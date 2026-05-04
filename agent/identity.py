"""Identity module — load, parse, and update data/pepper_identity.md.

Governance: propose-then-approve (ADR-0008, issue #52).

The file has two top-level sections:

  ## Identity               — committed self-model; approval-gated.
  ## Questions Pepper …     — observational; reflector-writable, no gate.

Public API
----------
- ``load_identity_doc()``       — parse the file into a dict; graceful on
                                  missing file (returns empty dict).
- ``get_identity_block()``      — rendered ## Identity text or "".
- ``get_questions_block()``     — rendered ## Questions … text or "".
- ``identity_version()``        — parse identity_version: N or None.
- ``propose_identity_diff()``   — queue a pending identity diff for approval.
- ``apply_identity_diff()``     — apply an approved diff (called by executor).
- ``update_identity_questions()``
                               — replace Questions section directly (no gate).
"""
from __future__ import annotations

import re
import structlog
from pathlib import Path
from typing import Any, Optional

logger = structlog.get_logger(__name__)

# Repo root = parent of the agent/ directory this file lives in.
_REPO_ROOT = Path(__file__).parent.parent
_IDENTITY_PATH = _REPO_ROOT / "data" / "pepper_identity.md"

# Section heading strings (matched case-sensitively after normalisation).
_IDENTITY_HEADING = "Identity"
_QUESTIONS_HEADING = "Questions Pepper is asking about herself"

# In-process cache: populated on first load, cleared on write.
_cached_identity: Optional[dict[str, str]] = None


# ── Loader ────────────────────────────────────────────────────────────────────


def _identity_path() -> Path:
    """Return the resolved path to data/pepper_identity.md."""
    return _IDENTITY_PATH


def load_identity_doc(*, _force_reload: bool = False) -> dict[str, str]:
    """Read and parse data/pepper_identity.md.

    Returns a dict keyed by section heading (without the leading '## ').
    Returns an empty dict if the file is missing — Pepper boots fine without
    an identity doc; the selector will return empty blocks.

    Caches the result in-process (``_cached_identity``) so repeated calls
    within a session do not hit disk. Call with ``_force_reload=True`` or
    clear ``_cached_identity = None`` externally to invalidate.
    """
    global _cached_identity
    if _cached_identity is not None and not _force_reload:
        return _cached_identity

    path = _identity_path()
    if not path.exists():
        logger.debug("identity.missing", path=str(path))
        _cached_identity = {}
        return _cached_identity

    text = path.read_text(encoding="utf-8")
    _cached_identity = _parse_sections(text)
    logger.debug(
        "identity.loaded",
        path=str(path),
        sections=list(_cached_identity.keys()),
    )
    return _cached_identity


def _parse_sections(text: str) -> dict[str, str]:
    """Split markdown ## headings into {heading_text: body_text} pairs.

    Only top-level '## ' headings are treated as section boundaries.
    Lines starting with '# ' (the file-level comment header) are ignored.
    """
    sections: dict[str, str] = {}
    current_heading: Optional[str] = None
    current_lines: list[str] = []

    for line in text.splitlines():
        if line.startswith("## "):
            if current_heading is not None:
                sections[current_heading] = "\n".join(current_lines).strip()
            current_heading = line[3:].strip()
            current_lines = []
        else:
            if current_heading is not None:
                current_lines.append(line)

    if current_heading is not None:
        sections[current_heading] = "\n".join(current_lines).strip()

    return sections


def _invalidate_cache() -> None:
    global _cached_identity
    _cached_identity = None


# ── Public accessors ──────────────────────────────────────────────────────────


def get_identity_block() -> str:
    """Return the '## Identity' section text, or empty string if absent."""
    doc = load_identity_doc()
    return doc.get(_IDENTITY_HEADING, "")


def get_questions_block() -> str:
    """Return the '## Questions Pepper is asking about herself' text, or ''."""
    doc = load_identity_doc()
    return doc.get(_QUESTIONS_HEADING, "")


def identity_version() -> Optional[int]:
    """Parse and return the identity_version: N value from the Identity section.

    Returns None if the section is missing or the line is absent/malformed.
    """
    block = get_identity_block()
    if not block:
        return None
    m = re.search(r"^identity_version:\s*(\d+)", block, re.MULTILINE)
    if m:
        return int(m.group(1))
    return None


# ── Write path: propose_identity_diff ────────────────────────────────────────


def propose_identity_diff(
    diff_text: str,
    *,
    pending_actions: Any,
    description: str = "",
) -> dict:
    """Queue a proposed identity diff for Jack's approval.

    Enqueues an ``apply_identity_diff`` action in the pending-actions queue.
    Jack approves, edits, or rejects it via the Pepper status panel.

    Args:
        diff_text:       The proposed new text for the '## Identity' section.
        pending_actions: A ``PendingActionsQueue`` instance (injected by core).
        description:     Optional model-supplied rationale shown as advisory.

    Returns the enqueued action as a dict.
    """
    if not diff_text.strip():
        return {"error": "propose_identity_diff: diff_text must be non-empty"}

    action = pending_actions.queue(
        tool_name="apply_identity_diff",
        args={"identity_section_text": diff_text},
        preview=description or f"Identity diff: {diff_text[:120]}…",
    )
    logger.info(
        "identity.diff_proposed",
        action_id=action.id,
        diff_preview=diff_text[:80],
    )
    return {
        "ok": True,
        "queued": True,
        "action_id": action.id,
        "message": (
            "Identity diff queued for Jack's review. "
            "Visible in the Pepper status panel."
        ),
    }


# ── Write path: apply_identity_diff ──────────────────────────────────────────


def apply_identity_diff(args: dict) -> dict:
    """Apply an approved identity diff to the ## Identity section.

    Called by the pending-actions executor after Jack approves. Replaces the
    entire ## Identity section with the new text, bumps identity_version, and
    writes the file atomically.

    Args:
        args: dict with key ``"identity_section_text"`` — the new full body of
              the Identity section (not including the '## Identity' heading).

    Returns {"ok": True, "identity_version": N} on success.
    """
    new_body = args.get("identity_section_text", "")
    if not new_body.strip():
        return {"error": "apply_identity_diff: identity_section_text must be non-empty"}

    path = _identity_path()
    if not path.exists():
        return {"error": f"apply_identity_diff: {path} not found"}

    text = path.read_text(encoding="utf-8")

    # Bump identity_version in the new body.
    current_ver = _parse_version_in_block(new_body, "identity_version")
    new_ver = (current_ver or 0) + 1
    if re.search(r"^identity_version:\s*\d+", new_body, re.MULTILINE):
        new_body = re.sub(
            r"^(identity_version:\s*)\d+",
            rf"\g<1>{new_ver}",
            new_body,
            flags=re.MULTILINE,
        )
    else:
        new_body = new_body.rstrip() + f"\n\nidentity_version: {new_ver}"

    # Replace the ## Identity section in the full document.
    new_text = _replace_section(text, _IDENTITY_HEADING, new_body)
    path.write_text(new_text, encoding="utf-8")
    _invalidate_cache()

    logger.info("identity.diff_applied", identity_version=new_ver)
    return {"ok": True, "identity_version": new_ver}


# ── Write path: update_identity_questions ────────────────────────────────────


def update_identity_questions(args: dict) -> dict:
    """Replace the '## Questions Pepper is asking about herself' section.

    No approval gate — this is the reflector's low-stakes self-observation
    path (ADR-0008). Bumps questions_version.

    Args:
        args: dict with key ``"new_questions"`` — the new full body of the
              Questions section (not including the heading line).

    Returns {"ok": True, "questions_version": N}.
    """
    new_body = args.get("new_questions", "")
    if not new_body.strip():
        return {"error": "update_identity_questions: new_questions must be non-empty"}

    path = _identity_path()
    if not path.exists():
        return {"error": f"update_identity_questions: {path} not found"}

    text = path.read_text(encoding="utf-8")

    current_ver = _parse_version_in_block(new_body, "questions_version")
    # Also check current file if the caller didn't include a version line.
    if current_ver is None:
        existing_questions = _parse_sections(text).get(_QUESTIONS_HEADING, "")
        current_ver = _parse_version_in_block(existing_questions, "questions_version")

    new_ver = (current_ver or 0) + 1
    if re.search(r"^questions_version:\s*\d+", new_body, re.MULTILINE):
        new_body = re.sub(
            r"^(questions_version:\s*)\d+",
            rf"\g<1>{new_ver}",
            new_body,
            flags=re.MULTILINE,
        )
    else:
        new_body = new_body.rstrip() + f"\n\nquestions_version: {new_ver}"

    new_text = _replace_section(text, _QUESTIONS_HEADING, new_body)
    path.write_text(new_text, encoding="utf-8")
    _invalidate_cache()

    logger.info("identity.questions_updated", questions_version=new_ver)
    return {"ok": True, "questions_version": new_ver}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_version_in_block(block: str, key: str) -> Optional[int]:
    """Parse 'key: N' from a section body. Returns None if absent."""
    m = re.search(rf"^{re.escape(key)}:\s*(\d+)", block, re.MULTILINE)
    return int(m.group(1)) if m else None


def _replace_section(full_text: str, heading: str, new_body: str) -> str:
    """Replace the body of '## <heading>' in full_text with new_body.

    If the section is not found, appends it as a new section.
    """
    lines = full_text.splitlines(keepends=True)
    result: list[str] = []
    in_target = False
    replaced = False

    for line in lines:
        if line.rstrip() == f"## {heading}":
            in_target = True
            result.append(line)
            result.append(new_body.rstrip() + "\n")
            replaced = True
            continue
        if in_target and line.startswith("## "):
            in_target = False
        if not in_target:
            result.append(line)

    if not replaced:
        result.append(f"\n## {heading}\n\n{new_body.rstrip()}\n")

    return "".join(result)
