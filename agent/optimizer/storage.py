"""Versioned-file store for ``CandidatePrompt``s.

Layout:

    <base_dir>/<target>/<version_hash>.json

One file per candidate. Flat directory per target — querying is fast
enough at the volumes this module sees (tens to low-hundreds of
candidates per target, total).

The store is intentionally framework-agnostic: it round-trips a
``CandidatePrompt`` dataclass via JSON. A future swap from GEPA to DSPy
(see ADR-0007) does not require migrating the store.

Default base directories (set in #45; overrideable for tests):

- ``data/optimizer/candidates/`` — local-only candidate prompts
  (gitignored under ``data/*``).
- ``agent/prompts/`` — accepted prompts (committed). Promotion is
  the eval gate's concern (#48); this module only writes accepted
  prompts when its caller passes them with ``status=ACCEPTED``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

from agent.optimizer.schema import CandidatePrompt, PromptStatus

logger = structlog.get_logger(__name__)

DEFAULT_CANDIDATE_DIR = Path("data/optimizer/candidates")
DEFAULT_ACCEPTED_DIR = Path("agent/prompts")

# Targets are file-path components — keep them ASCII-safe and refuse anything
# that could escape the base directory (``..`` or ``/`` traversal).
_TARGET_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION_HASH_RE = re.compile(r"^[a-f0-9]{8,64}$")


class StorageError(Exception):
    """Raised for invalid target names, version hashes, or path-escapes."""


def compute_version_hash(target: str, prompt_text: str) -> str:
    """Stable hex hash of a (target, prompt_text) pair.

    sha256 truncated to 16 hex chars (64 bits) — enough to avoid
    collisions across the lifetime of any individual target's prompt
    population (we expect O(100) candidates per target, not O(1e9)).
    """
    _validate_target(target)
    h = hashlib.sha256()
    h.update(target.encode("utf-8"))
    h.update(b"\x00")
    h.update(prompt_text.encode("utf-8"))
    return h.hexdigest()[:16]


def _validate_target(target: str) -> None:
    if not _TARGET_RE.match(target):
        raise StorageError(
            f"target must match {_TARGET_RE.pattern!r}, got {target!r}",
        )


def _validate_version_hash(version_hash: str) -> None:
    if not _VERSION_HASH_RE.match(version_hash):
        raise StorageError(
            f"version_hash must match {_VERSION_HASH_RE.pattern!r}, got {version_hash!r}",
        )


def _candidate_to_json(c: CandidatePrompt) -> dict:
    """Serialize for on-disk storage. ``datetime`` → ISO-8601, enum → value."""
    d = asdict(c)
    d["status"] = c.status.value
    d["created_at"] = c.created_at.isoformat()
    return d


def _candidate_from_json(d: dict) -> CandidatePrompt:
    return CandidatePrompt(
        target=d["target"],
        version_hash=d["version_hash"],
        parent_version=d.get("parent_version", ""),
        optimizer_run_id=d["optimizer_run_id"],
        prompt_text=d["prompt_text"],
        eval_score=float(d.get("eval_score", float("nan"))),
        status=PromptStatus(d.get("status", PromptStatus.CANDIDATE.value)),
        created_at=datetime.fromisoformat(d["created_at"]),
        sanitization=list(d.get("sanitization", [])),
    )


class PromptStore:
    """Filesystem-backed candidate-prompt store.

    Read/write is single-process — there's no expectation of concurrent
    writers (the optimizer is operator-triggered, single-threaded). If
    that changes, add a ``flock`` around ``put``.
    """

    def __init__(self, base_dir: Path | str = DEFAULT_CANDIDATE_DIR) -> None:
        self._base = Path(base_dir).resolve()
        self._base.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        return self._base

    def _path_for(self, target: str, version_hash: str) -> Path:
        _validate_target(target)
        _validate_version_hash(version_hash)
        target_dir = (self._base / target).resolve()
        # Path.resolve() collapses `..` — verify the resolved path stays
        # inside `_base`. Defense-in-depth against a hand-crafted target
        # that bypassed the regex (or against the regex itself getting
        # loosened later).
        if not str(target_dir).startswith(str(self._base) + os.sep) and target_dir != self._base:
            raise StorageError(f"target {target!r} resolved outside base_dir")
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / f"{version_hash}.json"

    def put(self, candidate: CandidatePrompt) -> Path:
        """Persist ``candidate``.

        Enforced invariants:

        - ``CANDIDATE → ACCEPTED`` and ``ACCEPTED → ROLLED_BACK`` are
          the only forward transitions allowed; the inverse is rejected.
          ``ROLLED_BACK → CANDIDATE`` is allowed (reconsidered after
          fix). Same-status overwrite is allowed (e.g. score update on
          a candidate).
        - A candidate with a non-empty ``sanitization`` list cannot be
          stored with ``status=ACCEPTED``. The PII gate is hard, not
          advisory: a candidate that mentions life-context tokens or
          email/phone-shaped strings must be cleaned (or paraphrased
          via #45 sanitizer follow-up) before promotion. The eval gate
          (#48) is a *second* line of defence; this one always runs.
        """
        if candidate.status == PromptStatus.ACCEPTED and candidate.sanitization:
            raise StorageError(
                "refusing to store ACCEPTED candidate with non-empty "
                "sanitization findings: "
                f"{candidate.sanitization!r} (target={candidate.target!r}, "
                f"version_hash={candidate.version_hash!r}). Sanitize the "
                "prompt and re-run before promotion.",
            )
        path = self._path_for(candidate.target, candidate.version_hash)
        if path.exists():
            existing = self.get(candidate.target, candidate.version_hash)
            if existing is not None and not _is_valid_transition(
                existing.status, candidate.status,
            ):
                raise StorageError(
                    f"invalid status transition for {candidate.version_hash}: "
                    f"{existing.status.value} → {candidate.status.value}",
                )
        # Atomic write via temp + rename, so a crash mid-write does not leave
        # a half-formed JSON file that breaks subsequent reads.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_candidate_to_json(candidate), indent=2))
        tmp.replace(path)
        logger.debug(
            "optimizer.storage.put",
            target=candidate.target,
            version_hash=candidate.version_hash,
            status=candidate.status.value,
            path=str(path),
        )
        return path

    def get(self, target: str, version_hash: str) -> Optional[CandidatePrompt]:
        path = self._path_for(target, version_hash)
        if not path.exists():
            return None
        return _candidate_from_json(json.loads(path.read_text()))

    def list(
        self,
        target: str,
        *,
        status: Optional[PromptStatus] = None,
    ) -> list[CandidatePrompt]:
        """Return all candidates for ``target`` (newest first by created_at).

        Optional ``status`` filters in-memory after load; expected
        candidate counts (O(100) per target) make a directory scan
        cheap enough to do unconditionally.
        """
        _validate_target(target)
        target_dir = (self._base / target).resolve()
        if not target_dir.exists():
            return []
        out: list[CandidatePrompt] = []
        for p in target_dir.glob("*.json"):
            try:
                c = _candidate_from_json(json.loads(p.read_text()))
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.warning("optimizer.storage.bad_record", path=str(p), error=str(e))
                continue
            if status is not None and c.status != status:
                continue
            out.append(c)
        out.sort(key=lambda c: c.created_at, reverse=True)
        return out

    def iter_targets(self) -> Iterator[str]:
        for p in sorted(self._base.iterdir()):
            if p.is_dir() and _TARGET_RE.match(p.name):
                yield p.name


def _is_valid_transition(prev: PromptStatus, new: PromptStatus) -> bool:
    """Allowed lifecycle transitions for prompts.

    - CANDIDATE → CANDIDATE  (idempotent re-write of metadata, e.g. score update)
    - CANDIDATE → ACCEPTED   (eval gate promotes — #48's job)
    - ACCEPTED  → ROLLED_BACK (production telemetry triggered rollback)
    - ROLLED_BACK → CANDIDATE (reconsidered after fix)

    Other transitions are rejected by ``put``.
    """
    allowed = {
        (PromptStatus.CANDIDATE, PromptStatus.CANDIDATE),
        (PromptStatus.CANDIDATE, PromptStatus.ACCEPTED),
        (PromptStatus.ACCEPTED, PromptStatus.ROLLED_BACK),
        (PromptStatus.ROLLED_BACK, PromptStatus.CANDIDATE),
    }
    return (prev, new) in allowed
