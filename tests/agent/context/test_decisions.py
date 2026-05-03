"""Issue #33: ``agent.context.decisions.annotate`` produces a string per
selector explaining why each selection happened."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agent.context import ContextAssembler, Turn, annotate


class _StubConfig:
    OWNER_NAME = "Tester"
    LIFE_CONTEXT_PATH = ""
    TIMEZONE = "UTC"
    WEEKLY_REVIEW_DAY = 6
    WEEKLY_REVIEW_HOUR = 9
    MORNING_BRIEF_HOUR = 6
    MORNING_BRIEF_MINUTE = 30


class _StubMemory:
    def __init__(self, history: list[dict]) -> None:
        self._history = history

    def get_working_memory(self, *, limit: int) -> list[dict]:
        return list(self._history[-limit:])


def _ctx(tmp_path: Path, **turn_kwargs):
    p = tmp_path / "life_context.md"
    p.write_text(
        "## Owner\nName: Tester\n\n## Children\nThree kids.\n",
        encoding="utf-8",
    )
    cfg = _StubConfig()
    cfg.LIFE_CONTEXT_PATH = str(p)
    asm = ContextAssembler(
        life_context_path=str(p),
        config=cfg,
        capability_registry=None,
        memory_manager=_StubMemory(turn_kwargs.pop("history", [])),
        skills_provider=lambda: [],
        timezone="UTC",
    )
    fixed_now = datetime(2026, 5, 2, 12, 0, tzinfo=ZoneInfo("UTC"))
    turn = Turn(user_message="hi", now_override=fixed_now, **turn_kwargs)
    return asm.assemble(turn)


def test_annotate_returns_one_string_per_selector(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    notes = annotate(ctx)
    for selector_name in (
        "life_context",
        "capability_block",
        "retrieved_memory",
        "skill_match",
        "last_n_turns",
    ):
        assert selector_name in notes
        assert isinstance(notes[selector_name], str)
        assert notes[selector_name]  # non-empty


def test_annotate_mentions_section_count(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    notes = annotate(ctx)
    # Two ## headings in the test fixture → "2 section(s)".
    assert "2 section" in notes["life_context"]


def test_annotate_isolated_turn(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, isolated=True, history=[{"role": "user", "content": "x"}])
    notes = annotate(ctx)
    assert "isolated" in notes["last_n_turns"].lower()


def test_annotate_no_memory_hits(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, memory_context="", memory_records=[])
    notes = annotate(ctx)
    assert "no recall hits" in notes["retrieved_memory"]


def test_annotate_with_memory_hits(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        memory_context="MEMORY",
        memory_records=[
            {"id": "a", "score": 0.9},
            {"id": "b", "score": 0.5},
        ],
    )
    notes = annotate(ctx)
    assert "memory ID" in notes["retrieved_memory"]


def test_annotate_skills_index_suppressed(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, include_skills_index=False)
    notes = annotate(ctx)
    assert "suppressed" in notes["skill_match"]


def test_annotate_robust_to_garbage_provenance(tmp_path: Path) -> None:
    """Annotation is a debug aid — never raise on a bad provenance dict."""
    from agent.context.decisions import _explain
    from agent.context.types import SelectorRecord

    rec = SelectorRecord(name="life_context", content="", provenance={"x": object()})
    out = _explain("life_context", rec)
    assert isinstance(out, str)
