"""SkillMatchSelector — appends the lazy-loaded skills index to the prompt.

The skills system uses progressive disclosure: the model sees a one-line
index every turn and calls ``skill_view`` to load full bodies on demand.
This selector wraps :func:`agent.skills.build_index` and reports which
skill names were exposed for the turn.
"""

from __future__ import annotations

from typing import Any

from agent.context.types import SelectorRecord
from agent.skills import build_index


class SkillMatchSelector:
    name = "skill_match"

    def __init__(self, skills_provider: Any) -> None:
        # ``skills_provider`` is a zero-arg callable returning the current
        # list of skills. Using a callable instead of holding the list
        # directly lets ``Pepper.reload_skills()`` swap the underlying
        # collection without invalidating the assembler.
        self._skills_provider = skills_provider

    def select(self, *, include: bool) -> SelectorRecord:
        if not include:
            provenance = {
                "selector": self.name,
                "included": False,
                "n_skills": 0,
                "skill_names": [],
            }
            return SelectorRecord(
                name=self.name,
                content="",
                provenance=provenance,
            )

        skills = list(self._skills_provider() or [])
        index = build_index(skills) or ""

        names: list[str] = []
        for s in skills:
            n = getattr(s, "name", None)
            if isinstance(n, str):
                names.append(n)

        provenance = {
            "selector": self.name,
            "included": True,
            "n_skills": len(skills),
            "skill_names": sorted(names),
            "index_chars": len(index),
        }
        return SelectorRecord(
            name=self.name,
            content=index,
            provenance=provenance,
        )
