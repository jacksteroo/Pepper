"""Tests for the Phase 4 skill system (agent/skills.py + agent/skill_reviewer.py)."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.skills import Skill, SkillMatcher, _parse_frontmatter, _load_skill, load_skills


# ── _parse_frontmatter ────────────────────────────────────────────────────────

def test_parse_frontmatter_extracts_dict_and_body():
    raw = dedent("""\
        ---
        name: test_skill
        description: A test skill
        triggers:
          - hello
          - hi there
        tools:
          - search_memory
        model: local
        version: 1
        ---

        ## Workflow

        1. Do something.
    """)
    fm, body = _parse_frontmatter(raw)
    assert fm["name"] == "test_skill"
    assert fm["triggers"] == ["hello", "hi there"]
    assert fm["tools"] == ["search_memory"]
    assert fm["version"] == 1
    assert "Do something" in body


def test_parse_frontmatter_no_frontmatter_returns_empty_dict():
    raw = "Just some content with no frontmatter."
    fm, body = _parse_frontmatter(raw)
    assert fm == {}
    assert body == raw


def test_parse_frontmatter_empty_yaml_returns_empty_dict():
    raw = "---\n---\nBody here."
    fm, body = _parse_frontmatter(raw)
    assert fm == {}
    assert "Body here" in body


# ── _load_skill ───────────────────────────────────────────────────────────────

def _write_skill(tmp_path: Path, content: str, filename: str = "my_skill.md") -> Path:
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    return path


def test_load_skill_valid_file(tmp_path):
    path = _write_skill(tmp_path, dedent("""\
        ---
        name: morning_brief
        description: Daily brief
        triggers:
          - morning brief
          - daily brief
        tools:
          - get_upcoming_events
        model: local
        version: 2
        ---

        ## Workflow

        1. Greet.
        2. Fetch calendar.
    """))
    skill = _load_skill(path)
    assert skill is not None
    assert skill.name == "morning_brief"
    assert skill.version == 2
    assert "morning brief" in skill.triggers
    assert "get_upcoming_events" in skill.tools
    assert "Workflow" in skill.content


def test_load_skill_missing_name_returns_none(tmp_path):
    path = _write_skill(tmp_path, dedent("""\
        ---
        description: No name here
        ---
        ## Workflow
        1. Step.
    """))
    assert _load_skill(path) is None


def test_load_skill_empty_body_returns_none(tmp_path):
    path = _write_skill(tmp_path, dedent("""\
        ---
        name: empty_skill
        ---
    """))
    assert _load_skill(path) is None


def test_load_skill_triggers_lowercased(tmp_path):
    path = _write_skill(tmp_path, dedent("""\
        ---
        name: demo
        triggers:
          - Morning Brief
          - DAILY BRIEF
        ---
        ## Workflow
        1. Run.
    """))
    skill = _load_skill(path)
    assert skill is not None
    assert all(t == t.lower() for t in skill.triggers)


# ── load_skills ───────────────────────────────────────────────────────────────

def test_load_skills_returns_empty_for_missing_dir(tmp_path):
    skills = load_skills(skills_dir=tmp_path / "nonexistent")
    assert skills == []


def test_load_skills_loads_all_md_files(tmp_path):
    for i in range(3):
        _write_skill(tmp_path, dedent(f"""\
            ---
            name: skill_{i}
            triggers:
              - trigger {i}
            ---
            ## Workflow
            Step {i}.
        """), filename=f"skill_{i}.md")

    skills = load_skills(skills_dir=tmp_path)
    assert len(skills) == 3
    names = {s.name for s in skills}
    assert names == {"skill_0", "skill_1", "skill_2"}


def test_load_skills_skips_invalid_files(tmp_path):
    _write_skill(tmp_path, dedent("""\
        ---
        name: good_skill
        triggers:
          - do the thing
        ---
        ## Workflow
        1. Do it.
    """), filename="good.md")

    # File with no name — should be skipped
    _write_skill(tmp_path, "---\ndescription: no name\n---\n## Workflow\n1. Nope.", filename="bad.md")

    skills = load_skills(skills_dir=tmp_path)
    assert len(skills) == 1
    assert skills[0].name == "good_skill"


# ── SkillMatcher.match ────────────────────────────────────────────────────────

def _make_skill(name: str, triggers: list[str]) -> Skill:
    return Skill(
        name=name,
        description="",
        triggers=[t.lower() for t in triggers],
        tools=[],
        model="local",
        version=1,
        content="## Workflow\n1. Run.",
        path=Path(f"/fake/{name}.md"),
    )


def test_matcher_returns_empty_when_no_skills():
    matcher = SkillMatcher([])
    assert matcher.match("anything") == []


def test_matcher_matches_by_trigger_phrase():
    skill = _make_skill("morning_brief", ["morning brief", "daily brief"])
    matcher = SkillMatcher([skill])
    result = matcher.match("Generate my morning brief for today.")
    assert len(result) == 1
    assert result[0].name == "morning_brief"


def test_matcher_returns_empty_when_no_triggers_match():
    skill = _make_skill("weekly_review", ["weekly review", "week in review"])
    matcher = SkillMatcher([skill])
    result = matcher.match("What time is it?")
    assert result == []


def test_matcher_ranks_by_trigger_hit_count():
    skill_a = _make_skill("skill_a", ["review", "weekly"])
    skill_b = _make_skill("skill_b", ["review"])
    matcher = SkillMatcher([skill_a, skill_b])
    # "weekly review" hits skill_a twice, skill_b once
    result = matcher.match("weekly review please", top_n=2)
    assert result[0].name == "skill_a"
    assert result[1].name == "skill_b"


def test_matcher_respects_top_n():
    skills = [_make_skill(f"skill_{i}", ["common phrase"]) for i in range(5)]
    matcher = SkillMatcher(skills)
    result = matcher.match("common phrase", top_n=2)
    assert len(result) == 2


def test_matcher_case_insensitive():
    skill = _make_skill("demo", ["meeting prep"])
    matcher = SkillMatcher([skill])
    assert len(matcher.match("MEETING PREP for tomorrow")) == 1


# ── SkillMatcher.inject_into_prompt ──────────────────────────────────────────

def test_inject_appends_skill_block():
    skill = _make_skill("morning_brief", ["morning brief"])
    skill.content = "## Workflow\n1. Greet."  # type: ignore[attr-defined]
    matcher = SkillMatcher([skill])

    result = matcher.inject_into_prompt("Base prompt.", "morning brief please")
    assert "Base prompt." in result
    assert '<skill name="morning_brief">' in result
    assert "## Workflow" in result
    assert "</skill>" in result


def test_inject_returns_prompt_unchanged_when_no_match():
    skill = _make_skill("morning_brief", ["morning brief"])
    matcher = SkillMatcher([skill])

    original = "My system prompt."
    result = matcher.inject_into_prompt(original, "what time is it?")
    assert result == original


def test_inject_multiple_skills_all_appear():
    skill_a = _make_skill("skill_a", ["alpha"])
    skill_b = _make_skill("skill_b", ["beta"])
    matcher = SkillMatcher([skill_a, skill_b])

    result = matcher.inject_into_prompt("Prompt.", "alpha beta query")
    assert '<skill name="skill_a">' in result
    assert '<skill name="skill_b">' in result


# ── Real skills directory ─────────────────────────────────────────────────────

def test_real_skills_directory_loads():
    """Verify the skills/ directory in the repo loads without errors."""
    skills = load_skills()
    # At minimum the 5 seeded skills should be present
    assert len(skills) >= 5
    names = {s.name for s in skills}
    expected = {
        "morning_brief",
        "weekly_review",
        "commitment_check",
        "draft_reply_to_contact",
        "prep_for_meeting",
    }
    assert expected.issubset(names)


def test_real_skills_have_triggers():
    skills = load_skills()
    for skill in skills:
        assert skill.triggers, f"Skill '{skill.name}' has no triggers"


def test_real_skills_have_workflow_content():
    skills = load_skills()
    for skill in skills:
        assert "Workflow" in skill.content or "workflow" in skill.content.lower(), \
            f"Skill '{skill.name}' has no Workflow section"


def test_morning_brief_skill_triggers_match_scheduler_message():
    """The phrase the scheduler sends must match the morning_brief skill."""
    skills = load_skills()
    matcher = SkillMatcher(skills)

    import datetime as dt
    today = dt.datetime.now().strftime("%A, %B %-d, %Y")
    result = matcher.match(f"Generate my morning brief for {today}.")
    names = [s.name for s in result]
    assert "morning_brief" in names, f"morning_brief not matched; got: {names}"


def test_weekly_review_skill_triggers_match_scheduler_message():
    """The phrase the scheduler sends must match the weekly_review skill."""
    skills = load_skills()
    matcher = SkillMatcher(skills)

    import datetime as dt
    week_label = dt.datetime.now().strftime("Week of %B %-d, %Y")
    result = matcher.match(f"Generate my weekly review for {week_label}.")
    names = [s.name for s in result]
    assert "weekly_review" in names, f"weekly_review not matched; got: {names}"


def test_commitment_check_skill_triggers_match_scheduler_message():
    """The phrase the scheduler sends must match the commitment_check skill."""
    skills = load_skills()
    matcher = SkillMatcher(skills)

    result = matcher.match(
        "Commitment check: list any open commitments older than 48 hours. "
        "Skip anything already resolved."
    )
    names = [s.name for s in result]
    assert "commitment_check" in names, f"commitment_check not matched; got: {names}"
