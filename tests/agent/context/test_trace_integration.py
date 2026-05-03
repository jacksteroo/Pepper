"""Issue #33: assembler provenance flows into the trace builder.

Checks the wire-up between :class:`ContextAssembler`, the chat-turn logger
and :class:`TraceBuilder`. The full ``PepperCore.chat`` path is heavy to
exercise in unit tests; this test reproduces the relevant subset:

1. assemble a turn → get provenance
2. stamp it onto the chat-turn-logger trace dict (matches core.py)
3. snapshot the trace dict
4. feed it to ``TraceBuilder.set_assembled_context``
5. finalise the trace
6. assert the persisted ``Trace.assembled_context`` carries all five
   required keys with the expected types.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agent import chat_turn_logger
from agent.context import ContextAssembler, Turn
from agent.traces.emitter import TraceBuilder


class _StubConfig:
    OWNER_NAME = "Tester"
    LIFE_CONTEXT_PATH = ""
    TIMEZONE = "UTC"
    WEEKLY_REVIEW_DAY = 6
    WEEKLY_REVIEW_HOUR = 9
    MORNING_BRIEF_HOUR = 6
    MORNING_BRIEF_MINUTE = 30


class _StubMemory:
    def get_working_memory(self, *, limit: int) -> list[dict]:
        return []


def _life_context_path(tmp_path: Path) -> str:
    p = tmp_path / "life_context.md"
    p.write_text(
        "## Owner\nName: Tester\n\n## Children\nThree kids.\n",
        encoding="utf-8",
    )
    return str(p)


def test_provenance_flows_into_trace(tmp_path: Path) -> None:
    cfg = _StubConfig()
    cfg.LIFE_CONTEXT_PATH = _life_context_path(tmp_path)
    asm = ContextAssembler(
        life_context_path=cfg.LIFE_CONTEXT_PATH,
        config=cfg,
        capability_registry=None,
        memory_manager=_StubMemory(),
        skills_provider=lambda: [],
        timezone="UTC",
    )
    fixed_now = datetime(2026, 5, 2, 12, 0, tzinfo=ZoneInfo("UTC"))
    turn = Turn(
        user_message="what's up",
        memory_context="MEMORY_BLOCK",
        memory_records=[
            {"id": "11111111-1111-1111-1111-111111111111", "score": 0.7},
        ],
        now_override=fixed_now,
    )

    chat_turn_logger.start_turn()
    assembled = asm.assemble(turn)
    chat_turn_logger.record_assembled_context(assembled.provenance)

    trace_snapshot = dict(chat_turn_logger.get_trace() or {})
    assert trace_snapshot.get("assembled_context") is not None

    tb = TraceBuilder.start(input="what's up")
    tb.set_assembled_context(trace_snapshot.get("assembled_context"))
    trace = tb.finish(output="ok", latency_ms=10)

    ctx = trace.assembled_context
    # All five required fields present and typed.
    assert isinstance(ctx["life_context_sections_used"], list)
    assert ctx["life_context_sections_used"]  # non-empty for our fixture
    assert isinstance(ctx["last_n_turns"], int)
    assert isinstance(ctx["memory_ids"], list)
    assert ctx["memory_ids"] == [
        ["11111111-1111-1111-1111-111111111111", 0.7],
    ]
    # No skill match in this turn — must be JSON null, not absent.
    assert "skill_match" in ctx
    assert ctx["skill_match"] is None
    assert isinstance(ctx["capability_block_version"], str)
    assert len(ctx["capability_block_version"]) == 12


def test_set_assembled_context_tolerates_none() -> None:
    """A turn that bailed before assembly stamps ``None`` — must not crash."""
    tb = TraceBuilder.start(input="x")
    tb.set_assembled_context(None)
    trace = tb.finish(output="y", latency_ms=1)
    assert trace.assembled_context == {}
