"""Integration test for #22 — verify `chat()` schedules a trace emission
in its `finally` block.

We mock the LLM/memory/router stack the same way `test_core.py` does and
patch `agent.traces.emitter.emit_trace` to capture the Trace passed in.
The test asserts:

- Exactly one trace per chat turn.
- The trace's `input` and `output` reflect the user message and response.
- The trace's `latency_ms` is positive.
- The trace's `trigger_source` defaults to USER and overrides to SCHEDULER
  when the new kwarg is passed.
- A failure inside the trace emitter does not propagate (fail-soft).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.error_classifier import DataSensitivity
from agent.traces import Trace, TriggerSource


def make_mock_config():
    """Mirror the helper from agent/tests/test_core.py."""
    config = MagicMock()
    config.ALWAYS_HEAVY = False
    config.HEAVY_MAX_FALLBACK_RATIO = 0.0
    config.DEFAULT_LOCAL_MODEL = "hermes-test:latest"
    config.DEFAULT_FRONTIER_MODEL = "claude-opus-4-7"
    config.MODEL_CONTEXT_TOKENS = 8000
    config.LIFE_CONTEXT_PATH = "/dev/null"
    config.TIMEZONE = "UTC"
    config.OWNER_NAME = "Tester"
    return config


def make_mock_llm():
    llm = MagicMock()
    llm.chat = AsyncMock(return_value={"content": "general chat reply", "tool_calls": []})
    llm.embed_router = AsyncMock(return_value=[0.1] * 1024)
    llm.config = make_mock_config()
    return llm


@pytest.mark.asyncio
async def test_chat_emits_one_trace_per_turn() -> None:
    from agent.core import PepperCore

    config = make_mock_config()

    captured: list[Trace] = []

    async def _capture_emit(trace, **kwargs):
        captured.append(trace)
        return trace.trace_id

    with patch("agent.core.ModelClient") as MockLLM, \
         patch("agent.core.MemoryManager") as MockMem, \
         patch("agent.core.ToolRouter") as MockRouter, \
         patch("agent.core.build_system_prompt", return_value="system"), \
         patch("agent.core.CommitmentExtractor") as MockExtractor, \
         patch("agent.traces.emitter.emit_trace", side_effect=_capture_emit) as mock_emit, \
         patch("agent.db._session_factory", new=MagicMock()):
        mock_llm = make_mock_llm()
        MockLLM.return_value = mock_llm

        mock_memory = MagicMock()
        mock_memory.add_to_working_memory = MagicMock()
        mock_memory.get_working_memory = MagicMock(return_value=[])
        mock_memory.build_context_for_query = AsyncMock(return_value="")
        mock_memory.save_to_recall = AsyncMock()
        MockMem.return_value = mock_memory

        MockRouter.return_value.check_health = AsyncMock(return_value={})
        MockRouter.return_value.is_mcp_tool = MagicMock(return_value=False)
        MockRouter.return_value.is_mcp_read_only_tool = MagicMock(return_value=False)
        MockRouter.return_value.list_available_tools = AsyncMock(return_value=[])
        MockRouter.return_value.get_status.return_value = {}

        MockExtractor.return_value.has_commitment_language = MagicMock(return_value=False)

        pepper = PepperCore(config)
        pepper._initialized = True
        pepper._system_prompt = "system"

        response = await pepper.chat("hello pepper", "test-session")

        # Wait for the background trace task to complete. The chat()
        # finally block schedules emit_trace as a background task that
        # we patched — the patched coroutine awaits immediately so a
        # tiny yield is enough.
        await asyncio.sleep(0.05)

    assert isinstance(response, str)
    assert mock_emit.call_count == 1, "trace should be emitted exactly once per turn"
    assert len(captured) == 1
    t = captured[0]
    assert t.input == "hello pepper"
    assert t.output == response
    assert t.latency_ms >= 0
    assert t.trigger_source is TriggerSource.USER
    assert t.data_sensitivity is DataSensitivity.LOCAL_ONLY


@pytest.mark.asyncio
async def test_chat_passes_scheduler_trigger_source() -> None:
    from agent.core import PepperCore

    config = make_mock_config()

    captured: list[Trace] = []

    async def _capture_emit(trace, **kwargs):
        captured.append(trace)
        return trace.trace_id

    with patch("agent.core.ModelClient") as MockLLM, \
         patch("agent.core.MemoryManager") as MockMem, \
         patch("agent.core.ToolRouter") as MockRouter, \
         patch("agent.core.build_system_prompt", return_value="system"), \
         patch("agent.core.CommitmentExtractor") as MockExtractor, \
         patch("agent.traces.emitter.emit_trace", side_effect=_capture_emit), \
         patch("agent.db._session_factory", new=MagicMock()):
        MockLLM.return_value = make_mock_llm()
        mock_memory = MagicMock()
        mock_memory.add_to_working_memory = MagicMock()
        mock_memory.get_working_memory = MagicMock(return_value=[])
        mock_memory.build_context_for_query = AsyncMock(return_value="")
        mock_memory.save_to_recall = AsyncMock()
        MockMem.return_value = mock_memory
        MockRouter.return_value.check_health = AsyncMock(return_value={})
        MockRouter.return_value.is_mcp_tool = MagicMock(return_value=False)
        MockRouter.return_value.is_mcp_read_only_tool = MagicMock(return_value=False)
        MockRouter.return_value.list_available_tools = AsyncMock(return_value=[])
        MockRouter.return_value.get_status.return_value = {}
        MockExtractor.return_value.has_commitment_language = MagicMock(return_value=False)

        pepper = PepperCore(config)
        pepper._initialized = True
        pepper._system_prompt = "system"

        await pepper.chat(
            "morning brief please",
            "scheduler-session",
            trigger_source=TriggerSource.SCHEDULER,
            scheduler_job_name="morning_brief",
        )
        await asyncio.sleep(0.05)

    assert len(captured) == 1
    t = captured[0]
    assert t.trigger_source is TriggerSource.SCHEDULER
    assert t.scheduler_job_name == "morning_brief"


@pytest.mark.asyncio
async def test_emitter_failure_does_not_break_chat_response() -> None:
    """Fail-soft invariant: emit_trace raising must not propagate to chat()."""
    from agent.core import PepperCore

    config = make_mock_config()

    async def _boom(trace, **kwargs):
        raise RuntimeError("emitter exploded")

    with patch("agent.core.ModelClient") as MockLLM, \
         patch("agent.core.MemoryManager") as MockMem, \
         patch("agent.core.ToolRouter") as MockRouter, \
         patch("agent.core.build_system_prompt", return_value="system"), \
         patch("agent.core.CommitmentExtractor") as MockExtractor, \
         patch("agent.traces.emitter.emit_trace", side_effect=_boom), \
         patch("agent.db._session_factory", new=MagicMock()):
        MockLLM.return_value = make_mock_llm()
        mock_memory = MagicMock()
        mock_memory.add_to_working_memory = MagicMock()
        mock_memory.get_working_memory = MagicMock(return_value=[])
        mock_memory.build_context_for_query = AsyncMock(return_value="")
        mock_memory.save_to_recall = AsyncMock()
        MockMem.return_value = mock_memory
        MockRouter.return_value.check_health = AsyncMock(return_value={})
        MockRouter.return_value.is_mcp_tool = MagicMock(return_value=False)
        MockRouter.return_value.is_mcp_read_only_tool = MagicMock(return_value=False)
        MockRouter.return_value.list_available_tools = AsyncMock(return_value=[])
        MockRouter.return_value.get_status.return_value = {}
        MockExtractor.return_value.has_commitment_language = MagicMock(return_value=False)

        pepper = PepperCore(config)
        pepper._initialized = True
        pepper._system_prompt = "system"

        # The chat call must succeed despite the emitter error.
        response = await pepper.chat("hi", "test-session")

    # The emitter is scheduled as a background task — its raised exception
    # would surface as a Task exception, not propagate. The user sees the
    # response unchanged.
    assert isinstance(response, str)
