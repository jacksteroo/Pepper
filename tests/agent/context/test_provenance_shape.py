"""Issue #33: assert AssembledContext.provenance carries the five required keys.

The reflector (#39) and optimizer (#45) both consume this map. The contract
they rely on:

- ``life_context_sections_used: list[str]``
- ``last_n_turns: int``
- ``memory_ids: list[[uuid_str, score_float]]``
- ``skill_match: dict | None``
- ``capability_block_version: str``

Empty selectors (no skill match, no memory hits) must still surface their
key — as ``[]``, ``0``, ``None``, etc. — so JSONB queries can rely on the
field's presence.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from agent.context import ContextAssembler, Turn


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


def _life_context_path(tmp_path: Path) -> str:
    p = tmp_path / "life_context.md"
    p.write_text(
        "## Owner\nName: Tester\n\n## Children\nThree kids.\n",
        encoding="utf-8",
    )
    return str(p)


def _assemble(
    tmp_path: Path,
    *,
    history: list[dict] | None = None,
    memory_context: str = "",
    memory_records: list[dict] | None = None,
    isolated: bool = False,
    include_skills_index: bool = False,
) -> dict:
    cfg = _StubConfig()
    cfg.LIFE_CONTEXT_PATH = _life_context_path(tmp_path)
    asm = ContextAssembler(
        life_context_path=cfg.LIFE_CONTEXT_PATH,
        config=cfg,
        capability_registry=None,
        memory_manager=_StubMemory(history or []),
        skills_provider=lambda: [],
        timezone="UTC",
    )
    fixed_now = datetime(2026, 5, 2, 12, 0, tzinfo=ZoneInfo("UTC"))
    turn = Turn(
        user_message="hi",
        memory_context=memory_context,
        memory_records=memory_records or [],
        isolated=isolated,
        include_skills_index=include_skills_index,
        now_override=fixed_now,
    )
    return asm.assemble(turn).provenance


def test_provenance_has_five_required_keys(tmp_path: Path) -> None:
    prov = _assemble(tmp_path)
    for k in (
        "life_context_sections_used",
        "last_n_turns",
        "memory_ids",
        "skill_match",
        "capability_block_version",
    ):
        assert k in prov, f"missing required provenance key: {k}"


def test_required_keys_have_expected_types(tmp_path: Path) -> None:
    prov = _assemble(tmp_path)
    assert isinstance(prov["life_context_sections_used"], list)
    assert all(isinstance(s, str) for s in prov["life_context_sections_used"])
    assert isinstance(prov["last_n_turns"], int)
    assert isinstance(prov["memory_ids"], list)
    # skill_match may be dict OR None (empty selector → None)
    assert prov["skill_match"] is None or isinstance(prov["skill_match"], dict)
    assert isinstance(prov["capability_block_version"], str)


def test_empty_skill_match_serialises_as_null_not_missing(tmp_path: Path) -> None:
    """Empty selector → JSON null, not absent. JSONB queries depend on this."""
    prov = _assemble(tmp_path, include_skills_index=False)
    assert "skill_match" in prov
    assert prov["skill_match"] is None
    # And JSON-serialised null is JSON null.
    j = json.loads(json.dumps(prov))
    assert j["skill_match"] is None


def test_empty_memory_ids_is_empty_list(tmp_path: Path) -> None:
    prov = _assemble(tmp_path, memory_context="", memory_records=[])
    assert prov["memory_ids"] == []


def test_memory_ids_threaded_through(tmp_path: Path) -> None:
    rows = [
        {"id": "11111111-1111-1111-1111-111111111111", "score": 0.91},
        {"id": "22222222-2222-2222-2222-222222222222", "sim": 0.42},
    ]
    prov = _assemble(
        tmp_path,
        memory_context="MEMORY",
        memory_records=rows,
    )
    assert len(prov["memory_ids"]) == 2
    for pair in prov["memory_ids"]:
        assert isinstance(pair, list)
        assert len(pair) == 2
        uid, score = pair
        assert isinstance(uid, str)
        assert isinstance(score, float)


def test_isolated_turn_reports_zero_turns(tmp_path: Path) -> None:
    prov = _assemble(
        tmp_path,
        history=[{"role": "user", "content": "x"}],
        isolated=True,
    )
    assert prov["last_n_turns"] == 0


def test_last_n_turns_counts_pairs(tmp_path: Path) -> None:
    history = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": "d"},
    ]
    prov = _assemble(tmp_path, history=history)
    assert prov["last_n_turns"] == 2


def test_provenance_is_fully_json_serialisable(tmp_path: Path) -> None:
    prov = _assemble(
        tmp_path,
        memory_context="ctx",
        memory_records=[
            {"id": "11111111-1111-1111-1111-111111111111", "score": 0.5},
        ],
    )
    # Must round-trip cleanly — the trace JSONB column is the consumer.
    json.dumps(prov)


def test_selectors_view_still_present(tmp_path: Path) -> None:
    """Top-level keys are the new contract; per-selector detail stays under
    ``selectors``."""
    prov = _assemble(tmp_path)
    assert "selectors" in prov
    for name in (
        "life_context",
        "capability_block",
        "retrieved_memory",
        "skill_match",
        "last_n_turns",
    ):
        assert name in prov["selectors"]
