"""LifeContextSelector unit tests."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agent.context.selectors import LifeContextSelector


def _write_life_context(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "life_context.md"
    p.write_text(body, encoding="utf-8")
    return p


class _StubConfig:
    OWNER_NAME = "Tester"
    WEEKLY_REVIEW_DAY = 6
    WEEKLY_REVIEW_HOUR = 9
    MORNING_BRIEF_HOUR = 6
    MORNING_BRIEF_MINUTE = 30


def test_selects_system_prompt_with_provenance(tmp_path: Path) -> None:
    p = _write_life_context(
        tmp_path,
        "## Owner\nName: Tester\n\n## Children\nThree kids.\n",
    )
    sel = LifeContextSelector(
        life_context_path=str(p),
        config=_StubConfig(),
        capability_registry=None,
    )
    rec = sel.select()

    assert rec.name == "life_context"
    assert isinstance(rec.content, str)
    # The owner-name + life-context body should be in the rendered prompt.
    assert "Tester" in rec.content

    prov = rec.provenance
    assert prov["selector"] == "life_context"
    # Both sections we put in the file should appear in provenance.
    assert "Owner" in prov["sections_loaded"]
    assert "Children" in prov["sections_loaded"]
    assert prov["section_count"] == 2
    assert prov["system_prompt_chars"] == len(rec.content)


def test_caches_until_refresh(tmp_path: Path) -> None:
    p = _write_life_context(tmp_path, "## Owner\nName: A\n")
    sel = LifeContextSelector(
        life_context_path=str(p),
        config=_StubConfig(),
        capability_registry=None,
    )
    rec1 = sel.select()

    # Re-read the file with new content; cached prompt should still win.
    p.write_text("## Owner\nName: B\n", encoding="utf-8")
    rec2 = sel.select()
    assert rec1.content == rec2.content

    # Refresh forces a rebuild.
    sel.refresh()
    rec3 = sel.select()
    assert rec3.content != rec1.content


def test_provenance_is_json_serializable(tmp_path: Path) -> None:
    import json

    p = _write_life_context(tmp_path, "## Section\nbody\n")
    sel = LifeContextSelector(
        life_context_path=str(p),
        config=_StubConfig(),
        capability_registry=None,
    )
    rec = sel.select()
    # Issue #33 will rely on this — selector provenance MUST round-trip JSON.
    json.dumps(rec.provenance)


def test_handles_owner_name_failure(tmp_path: Path) -> None:
    p = _write_life_context(tmp_path, "## Owner\nbody\n")
    sel = LifeContextSelector(
        life_context_path=str(p),
        config=_StubConfig(),
        capability_registry=None,
    )
    with patch("agent.context.selectors.life_context.get_owner_name", side_effect=RuntimeError):
        rec = sel.select()
    assert rec.provenance["owner_name"] == ""
