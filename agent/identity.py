"""PEPPER_IDENTITY — load, parse, render the operator-managed identity doc.

`data/pepper_identity.md` holds Pepper's self-model: values, voice, the
things she finds boring, how she handles being wrong. It is gitignored
(RAW_PERSONAL) and locally authored. The file is split into two
top-level sections (per ADR-0008):

    ## Identity
    Committed self-model. Approval-gated. Bumps `identity_version` on
    change. Reflector writes proposed diffs to a pending queue
    (`pending_identity_diffs` table); operator approves / edits / rejects.

    ## Questions Pepper is asking about herself
    Observational. Reflector writes freely (append/replace). No approval.
    Bumps a separate `questions_version` counter.

Versions are persisted as HTML comments on the file's first lines:

    <!-- identity_version: 3 -->
    <!-- questions_version: 7 -->

Header-in-file is portable, human-readable, and survives the operator
manually editing the file. The loader is fail-soft: a missing file
boots the system with an empty identity block (no crash). A malformed
file (one section missing) loads the present section and warns about
the missing one.

This module deliberately exposes only read + atomic-write helpers. The
diff-and-approve mechanics live in `agent/identity_diffs.py`; nothing
in this module mutates the file in-place.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


_REPO_ROOT = Path(__file__).parent.parent
DEFAULT_IDENTITY_PATH = "data/pepper_identity.md"

SECTION_IDENTITY = "Identity"
SECTION_QUESTIONS = "Questions Pepper is asking about herself"

_VALID_SECTIONS: frozenset[str] = frozenset({SECTION_IDENTITY, SECTION_QUESTIONS})

# Header comment patterns. We compile here so the file-IO path is hot.
_RE_IDENTITY_VERSION = re.compile(r"<!--\s*identity_version:\s*(\d+)\s*-->")
_RE_QUESTIONS_VERSION = re.compile(r"<!--\s*questions_version:\s*(\d+)\s*-->")
_RE_SECTION_HEADER = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Identity:
    """Parsed view of the identity file.

    Both sections may be empty strings (the file exists but the section
    body is empty) or absent (the section header was missing). The
    distinction matters for diagnostics — a missing section warns; an
    empty body is a valid edited state.
    """

    identity_text: str = ""
    questions_text: str = ""
    identity_present: bool = False
    questions_present: bool = False
    identity_version: int = 0
    questions_version: int = 0
    path: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.identity_text.strip() or self.questions_text.strip())


def _resolve_path(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else _REPO_ROOT / p


def _parse_versions(content: str) -> tuple[int, int]:
    iv = _RE_IDENTITY_VERSION.search(content)
    qv = _RE_QUESTIONS_VERSION.search(content)
    iv_n = int(iv.group(1)) if iv else 0
    qv_n = int(qv.group(1)) if qv else 0
    return iv_n, qv_n


def _split_sections(content: str) -> dict[str, str]:
    """Return {section_name: body} for every `## Heading` in the file.

    The body of a section is everything up to (but not including) the
    next `## ` heading or end-of-file. Whitespace is preserved verbatim
    so the round-trip writer can reproduce the operator's formatting.
    """
    sections: dict[str, str] = {}
    current: Optional[str] = None
    current_lines: list[str] = []
    for line in content.splitlines():
        m = _RE_SECTION_HEADER.match(line)
        if m:
            if current is not None:
                sections[current] = "\n".join(current_lines).strip("\n")
            current = m.group(1).strip()
            current_lines = []
        elif current is not None:
            current_lines.append(line)
    if current is not None:
        sections[current] = "\n".join(current_lines).strip("\n")
    return sections


def load_identity(path: str = DEFAULT_IDENTITY_PATH) -> Identity:
    """Read the identity file and return a parsed `Identity`.

    Fail-soft on a missing file: returns an empty Identity with the
    default versions. Logs a warning so the operator can see the path.
    """
    file_path = _resolve_path(path)
    if not file_path.exists():
        logger.info("pepper_identity_not_found", path=str(file_path))
        return Identity(path=str(file_path))

    content = file_path.read_text(encoding="utf-8")
    iv, qv = _parse_versions(content)
    sections = _split_sections(content)

    identity_present = SECTION_IDENTITY in sections
    questions_present = SECTION_QUESTIONS in sections

    if not identity_present:
        logger.warning(
            "pepper_identity_missing_section",
            section=SECTION_IDENTITY,
            path=str(file_path),
        )
    if not questions_present:
        logger.warning(
            "pepper_identity_missing_section",
            section=SECTION_QUESTIONS,
            path=str(file_path),
        )

    return Identity(
        identity_text=sections.get(SECTION_IDENTITY, ""),
        questions_text=sections.get(SECTION_QUESTIONS, ""),
        identity_present=identity_present,
        questions_present=questions_present,
        identity_version=iv,
        questions_version=qv,
        path=str(file_path),
    )


def render_identity_block(identity: Identity) -> str:
    """Render the identity content for inclusion in the system prompt.

    The Questions section is framed as exploratory ("things you are
    still working out about yourself") so the model treats it as an
    open thread, not a committed self-model entry. An empty identity
    returns "" — the assembler skips empty selector content.
    """
    if identity.is_empty:
        return ""
    parts: list[str] = []
    if identity.identity_text.strip():
        parts.append("[Pepper identity]\n" + identity.identity_text.strip())
    if identity.questions_text.strip():
        parts.append(
            "[Things you are still working out about yourself]\n"
            + identity.questions_text.strip()
        )
    return "\n\n".join(parts)


def _build_file_text(identity: Identity) -> str:
    """Serialise an `Identity` back to file form, with version headers.

    The output round-trips cleanly: `load_identity(write_identity(x))`
    produces an Identity with identical content + versions to `x`.
    """
    lines: list[str] = []
    lines.append(f"<!-- identity_version: {identity.identity_version} -->")
    lines.append(f"<!-- questions_version: {identity.questions_version} -->")
    lines.append("")
    lines.append(f"## {SECTION_IDENTITY}")
    lines.append("")
    if identity.identity_text.strip():
        lines.append(identity.identity_text.strip())
    lines.append("")
    lines.append(f"## {SECTION_QUESTIONS}")
    lines.append("")
    if identity.questions_text.strip():
        lines.append(identity.questions_text.strip())
    lines.append("")
    return "\n".join(lines)


def write_identity_atomic(identity: Identity) -> None:
    """Write `identity` back to its `path` atomically.

    Uses a temp file in the same directory + os.replace so a partial
    write cannot leave the file half-formed. Diff application rides
    on this primitive — atomicity is the diff layer's correctness
    invariant.
    """
    file_path = Path(identity.path)
    if not file_path.is_absolute():
        file_path = _resolve_path(identity.path or DEFAULT_IDENTITY_PATH)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    text = _build_file_text(identity)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".pepper_identity.",
        suffix=".tmp",
        dir=str(file_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, file_path)
    except Exception:
        # Tempfile cleanup on any error; os.replace rename is atomic so
        # we cannot end up with both files except on the failure path.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def apply_identity_diff(
    *,
    proposed_identity_text: str,
    path: str = DEFAULT_IDENTITY_PATH,
) -> Identity:
    """Atomically replace the `## Identity` section and bump version.

    Returns the new `Identity`. Used by the diff-approval flow in
    `agent/identity_diffs.py:approve_diff`.
    """
    current = load_identity(path)
    new = Identity(
        identity_text=proposed_identity_text,
        questions_text=current.questions_text,
        identity_present=True,
        questions_present=current.questions_present or False,
        identity_version=current.identity_version + 1,
        questions_version=current.questions_version,
        path=current.path or str(_resolve_path(path)),
    )
    write_identity_atomic(new)
    logger.info(
        "pepper_identity_section_applied",
        section=SECTION_IDENTITY,
        new_version=new.identity_version,
        path=new.path,
    )
    return new


def write_questions_section(
    proposed_questions_text: str,
    *,
    path: str = DEFAULT_IDENTITY_PATH,
) -> Identity:
    """Replace the `## Questions ...` section without approval.

    The reflector is the intended caller. Bumps `questions_version`
    independently of `identity_version`.
    """
    current = load_identity(path)
    new = Identity(
        identity_text=current.identity_text,
        questions_text=proposed_questions_text,
        identity_present=current.identity_present or False,
        questions_present=True,
        identity_version=current.identity_version,
        questions_version=current.questions_version + 1,
        path=current.path or str(_resolve_path(path)),
    )
    write_identity_atomic(new)
    logger.info(
        "pepper_identity_section_applied",
        section=SECTION_QUESTIONS,
        new_version=new.questions_version,
        path=new.path,
    )
    return new
