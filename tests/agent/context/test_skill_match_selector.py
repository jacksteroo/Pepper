"""SkillMatchSelector unit tests."""
from __future__ import annotations

import json
from dataclasses import dataclass

from agent.context.selectors import SkillMatchSelector


@dataclass
class _StubSkill:
    name: str
    description: str = "stub"
    triggers: tuple[str, ...] = ()
    body: str = ""


def test_include_false_returns_empty_string() -> None:
    sel = SkillMatchSelector(skills_provider=lambda: [_StubSkill("a"), _StubSkill("b")])
    rec = sel.select(include=False)
    assert rec.content == ""
    assert rec.provenance["included"] is False
    assert rec.provenance["skill_names"] == []
    assert rec.provenance["n_skills"] == 0


def test_include_true_renders_index() -> None:
    skills = [_StubSkill(name="alpha"), _StubSkill(name="beta")]
    sel = SkillMatchSelector(skills_provider=lambda: skills)
    rec = sel.select(include=True)

    assert rec.name == "skill_match"
    # Index from skills.build_index should mention both names.
    assert "alpha" in rec.content
    assert "beta" in rec.content
    assert rec.provenance["n_skills"] == 2
    assert rec.provenance["skill_names"] == ["alpha", "beta"]
    assert rec.provenance["index_chars"] == len(rec.content)


def test_provider_called_each_select() -> None:
    calls = {"n": 0}

    def _provider() -> list[_StubSkill]:
        calls["n"] += 1
        return [_StubSkill("only")]

    sel = SkillMatchSelector(skills_provider=_provider)
    sel.select(include=True)
    sel.select(include=True)
    # Provider re-checked each call so reload_skills() takes effect.
    assert calls["n"] == 2


def test_provenance_is_json_serializable() -> None:
    sel = SkillMatchSelector(skills_provider=lambda: [])
    rec = sel.select(include=True)
    json.dumps(rec.provenance)
