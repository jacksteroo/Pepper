"""Types and dataclasses for the context-assembly module.

The :class:`AssembledContext` is the contract between :class:`ContextAssembler`
and the rest of the system: it captures both the rendered prompt parts
(system prompt + history) AND a JSON-serializable provenance record explaining
**why** each selector picked what it did. Issue #33 will plumb provenance into
trace emission, so all provenance values must be plain dicts/lists of
primitives — no live objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# A "turn" is the input to the assembler. It bundles all the inputs needed by
# selectors so the assembler signature stays tight: ``assemble(turn) -> AC``.
#
# We use a dataclass with sensible defaults so callers (notably ``core.py``)
# only fill in the fields that actually exist for their code path. Heavy /
# light paths fill different subsets — that's fine, the assembler tolerates
# missing optional context.
@dataclass
class Turn:
    """Single-turn input bundle for context assembly.

    All fields are read-only inputs to selectors. The assembler does NOT
    fetch any of them itself — they are pre-computed by the caller (typically
    ``Pepper._chat_impl``) and handed in. This keeps the assembler synchronous
    and keeps the existing concurrent ``asyncio.gather`` proactive-fetch
    pattern in core unchanged.
    """

    user_message: str
    channel: str = ""
    isolated: bool = False
    history_limit: int = 20

    # Pre-fetched contexts (already strings — the caller fetched them in
    # parallel via gather()). Empty string means "skip / don't include".
    memory_context: str = ""
    web_context: str = ""
    routing_context: str = ""
    calendar_context: str = ""
    email_context: str = ""
    imessage_context: str = ""
    whatsapp_context: str = ""
    slack_context: str = ""

    # Whether to include the lazy-loaded skills index in the system prompt.
    # ANSWER_FROM_CONTEXT turns strip this — see core for the rationale.
    include_skills_index: bool = True

    # Optional caller-provided string appended *after* proactive contexts but
    # *before* the skills index. The heavy path uses this for the GROUNDING
    # RULES block — that block is per-turn behavioural intercept logic which
    # belongs in core (it depends on routing/intent state the assembler does
    # not see), but its placement in the rendered prompt must be preserved
    # byte-for-byte. This hook keeps the renderer authoritative without
    # duplicating the rule text into the assembler.
    extra_system_suffix: str = ""

    # Optional caller-supplied "now" for deterministic snapshot tests. When
    # None the assembler uses datetime.now(tz) at assemble time.
    now_override: Any | None = None


@dataclass
class SelectorRecord:
    """One selector's contribution: what it produced + why.

    ``content`` is the rendered string the selector adds to the prompt (or
    history list, for last_n_turns). ``provenance`` is a JSON-serializable
    dict explaining selection — issue #33 reads this for traces.
    """

    name: str
    content: Any
    provenance: dict[str, Any]


@dataclass
class AssembledContext:
    """Result of :meth:`ContextAssembler.assemble`.

    Holds the assembled prompt parts plus a per-selector provenance record.
    ``render_prompt()`` produces the final system-prompt string. ``to_messages()``
    produces the ``[{"role": "system", ...}, *history]`` list that's fed to
    the LLM client.
    """

    system_prompt: str
    history: list[dict[str, Any]] = field(default_factory=list)
    selectors: dict[str, SelectorRecord] = field(default_factory=dict)

    @property
    def provenance(self) -> dict[str, dict[str, Any]]:
        """JSON-serializable provenance map, keyed by selector name.

        Each value is the raw provenance dict the selector emitted. #33 will
        attach this to traces so we can answer "why did the model see X?"
        offline.
        """
        return {name: rec.provenance for name, rec in self.selectors.items()}

    def render_prompt(self) -> str:
        """Return the final system-prompt string.

        This is the byte-identical equivalent of the inline
        ``system = ... + memory_context + ...`` chain that previously lived
        in ``core._chat_impl``.
        """
        return self.system_prompt

    def to_messages(self) -> list[dict[str, Any]]:
        """Return ``[{"role": "system", "content": ...}] + history``."""
        return [{"role": "system", "content": self.system_prompt}, *self.history]
