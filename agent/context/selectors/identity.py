"""IdentitySelector — wraps `agent.identity.load_identity` + render.

Loads `data/pepper_identity.md`, parses the two sections per ADR-0008
(`## Identity` + `## Questions Pepper is asking about herself`), and
emits a SelectorRecord whose content is the rendered identity block.
The block is appended into the system prompt by `ContextAssembler`
after the life-context block, on every turn.

Behaviour:
- Missing file → empty record (Pepper boots with no identity block).
- Parse error → empty record + warning log (no crash).
- Cached across turns; invalidated explicitly by `refresh()` after a
  diff is approved or the questions section is rewritten.

Optimizer carve-out: this selector is **not** subject to optimization
per ADR-0008. The exclusion is documented here AND in the optimizer's
selector list.
"""

from __future__ import annotations

from typing import Any

from agent.context.types import SelectorRecord
from agent.identity import (
    DEFAULT_IDENTITY_PATH,
    Identity,
    load_identity,
    render_identity_block,
)


class IdentitySelector:
    """Read-side selector for the PEPPER_IDENTITY surface."""

    name = "identity"

    def __init__(self, *, identity_path: str = DEFAULT_IDENTITY_PATH) -> None:
        self._path = identity_path
        self._cached: Identity | None = None

    def refresh(self) -> None:
        """Drop the cached identity so the next `select()` re-reads."""
        self._cached = None

    def _load(self) -> Identity:
        if self._cached is None:
            try:
                self._cached = load_identity(self._path)
            except Exception:
                # Defence in depth: load_identity is fail-soft already,
                # but if it raises we do NOT propagate the exception
                # into prompt assembly. An empty identity block is the
                # graceful degradation.
                self._cached = Identity(path=self._path)
        return self._cached

    def select(self) -> SelectorRecord:
        identity = self._load()
        content = render_identity_block(identity)
        provenance: dict[str, Any] = {
            "selector": self.name,
            "identity_path": identity.path,
            "identity_present": identity.identity_present,
            "questions_present": identity.questions_present,
            "identity_version": identity.identity_version,
            "questions_version": identity.questions_version,
            "identity_chars": len(identity.identity_text),
            "questions_chars": len(identity.questions_text),
        }
        return SelectorRecord(
            name=self.name,
            content=content,
            provenance=provenance,
        )
