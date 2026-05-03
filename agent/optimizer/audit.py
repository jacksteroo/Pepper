"""Append-only audit log of optimizer runs.

One JSON record per ``optimize()`` call appended to
``data/optimizer/audit.jsonl``. Records carry enough fields to reproduce
a run (see ``schema.OptimizerRunRecord``).

The log is the single source of truth for "did this run actually
happen, against what data, with what seed". It is read by the eval gate
(#48) to refuse promotion of candidates whose run_id has no audit
record.

Format choice — JSONL, not a database:

- Append-only fits the use case (one writer, occasional reader).
- Plain text survives schema migrations without code changes.
- ``tail -f`` works, which matters for an operator-triggered tool.

Concurrency: a single process is the expected writer. If that
assumption changes, switch to ``fcntl.flock`` around the append.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterator

import structlog

from agent.optimizer.schema import OptimizerRunRecord

logger = structlog.get_logger(__name__)

DEFAULT_AUDIT_PATH = Path("data/optimizer/audit.jsonl")


def _record_to_json(r: OptimizerRunRecord) -> dict:
    d = asdict(r)
    for k in ("window_since", "window_until", "started_at", "finished_at"):
        v = d.get(k)
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


def _record_from_json(d: dict) -> OptimizerRunRecord:
    def _dt(v):
        return datetime.fromisoformat(v) if v else None
    return OptimizerRunRecord(
        run_id=d["run_id"],
        target=d["target"],
        archetype=d["archetype"],
        prompt_version_filter=d.get("prompt_version_filter", ""),
        window_since=_dt(d.get("window_since")),
        window_until=_dt(d.get("window_until")),
        dataset_size=int(d["dataset_size"]),
        dataset_hash=d["dataset_hash"],
        seed=int(d["seed"]),
        baseline_version=d.get("baseline_version", ""),
        runner_class=d["runner_class"],
        candidate_count=int(d["candidate_count"]),
        started_at=_dt(d["started_at"]),
        finished_at=_dt(d["finished_at"]),
        error=d.get("error", ""),
    )


class AuditLog:
    """Append-only JSONL log of optimizer runs."""

    def __init__(self, path: Path | str = DEFAULT_AUDIT_PATH) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: OptimizerRunRecord) -> None:
        line = json.dumps(_record_to_json(record), sort_keys=True)
        # Open in append mode + write a single newline-terminated line. POSIX
        # guarantees an O_APPEND write under PIPE_BUF (4096 bytes) is atomic
        # enough for our single-writer assumption.
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        logger.info(
            "optimizer.audit.append",
            run_id=record.run_id,
            target=record.target,
            candidate_count=record.candidate_count,
            error=record.error or None,
        )

    def iter_records(self) -> Iterator[OptimizerRunRecord]:
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield _record_from_json(json.loads(line))
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    logger.warning(
                        "optimizer.audit.bad_record",
                        line=line[:200],
                        error=str(e),
                    )
                    continue

    def find(self, run_id: str) -> OptimizerRunRecord | None:
        for r in self.iter_records():
            if r.run_id == run_id:
                return r
        return None
