"""Context-assembly module.

Public surface:
- :class:`ContextAssembler` — composes the per-turn system prompt and history.
- :class:`AssembledContext` — the dataclass returned by ``assemble``.
- :class:`Turn` — bundle of inputs the caller hands the assembler.

See ``agent/context/assembler.py`` for the rendering contract and
``agent/context/selectors/`` for per-concern selectors.
"""

from agent.context.assembler import ContextAssembler
from agent.context.decisions import annotate
from agent.context.grounding_rules import render_grounding_rules
from agent.context.types import AssembledContext, SelectorRecord, Turn

__all__ = [
    "AssembledContext",
    "ContextAssembler",
    "SelectorRecord",
    "Turn",
    "annotate",
    "render_grounding_rules",
]
