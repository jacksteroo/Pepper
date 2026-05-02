"""Per-concern selectors used by the :class:`ContextAssembler`.

Each selector is a small, isolated unit that picks a single piece of context
for the LLM and returns ``(content, provenance)``. Selectors must NOT import
from each other or from ``agent.core`` — the same subsystem-isolation rule
that governs the rest of the codebase applies here.
"""

from agent.context.selectors.capability_block import CapabilityBlockSelector
from agent.context.selectors.last_n_turns import LastNTurnsSelector
from agent.context.selectors.life_context import LifeContextSelector
from agent.context.selectors.retrieved_memory import RetrievedMemorySelector
from agent.context.selectors.skill_match import SkillMatchSelector

__all__ = [
    "CapabilityBlockSelector",
    "LastNTurnsSelector",
    "LifeContextSelector",
    "RetrievedMemorySelector",
    "SkillMatchSelector",
]
