"""IdentitySelector — injects Pepper's self-model into the system prompt.

Reads from data/pepper_identity.md via :mod:`agent.identity`. Two sections
are surfaced:

  ## Identity — the committed self-model (values, voice, how Pepper handles
    being wrong). Approval-gated via propose_identity_diff (ADR-0008).

  ## Questions Pepper is asking about herself — observational, reflector-
    writable. Framed in the prompt as "things you are still working out about
    yourself" so the model treats it as exploratory rather than settled.

Graceful degradation: if the identity file is missing the selector returns
empty content — Pepper boots and operates normally without one. Callers MUST
NOT treat an empty identity block as an error.

NOTE: The optimizer (agent/optimizer/) is explicitly forbidden from optimizing
or tuning either section produced by this selector. Identity is a sacred
artifact, not a tunable prompt. See ADR-0008 §Decision and issue #52.
Enforcement is in agent/optimizer/runners.py — the identity target name is
included in EXCLUDED_TARGETS.
"""
from __future__ import annotations

from agent.context.types import SelectorRecord
from agent.identity import get_identity_block, get_questions_block, identity_version


class IdentitySelector:
    """Inject Pepper's identity blocks into the assembled system prompt.

    The selector is stateless across turns — it calls the module-level cache
    in agent.identity on every turn rather than holding its own. The identity
    cache is invalidated by apply_identity_diff / update_identity_questions,
    so changes become visible on the next turn after the write.
    """

    name = "identity"

    def select(self) -> SelectorRecord:
        identity_block = get_identity_block()
        questions_block = get_questions_block()
        ver = identity_version()

        parts: list[str] = []
        if identity_block:
            parts.append("## Pepper's Identity\n\n" + identity_block)
        if questions_block:
            parts.append(
                "## Things Pepper is still working out about herself\n\n"
                + questions_block
            )

        content = "\n\n".join(parts) if parts else ""

        provenance: dict = {
            "selector": self.name,
            "identity_present": bool(identity_block),
            "questions_present": bool(questions_block),
            "identity_version": ver,
            "content_chars": len(content),
        }
        return SelectorRecord(
            name=self.name,
            content=content,
            provenance=provenance,
        )
