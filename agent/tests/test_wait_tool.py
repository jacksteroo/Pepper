"""Tests for Issue #55/#56 — wait-action tool and trace integration.

Covers:
  - test_wait_tool_reason_required — wait without reason fails validation
  - test_wait_tool_produces_null_output_trace — wait tool produces a well-formed
    trace with output="" and reason populated (via trace builder)
  - test_scheduler_treats_wait_as_success — scheduler metrics treat
    wait-resolved jobs as success (wait is not an error result)
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.wait_tool import WAIT_TOOL_SCHEMA, execute_wait
from agent.traces.emitter import TraceBuilder
from agent.traces.schema import Trace, TriggerSource


# ── Unit: execute_wait ────────────────────────────────────────────────────────


class TestExecuteWait:
    @pytest.mark.asyncio
    async def test_wait_tool_reason_required(self) -> None:
        result = await execute_wait({})
        assert "error" in result
        assert "reason" in result["error"]

    @pytest.mark.asyncio
    async def test_wait_tool_reason_required_empty_string(self) -> None:
        result = await execute_wait({"reason": ""})
        # Empty string is falsy, so treated as missing.
        assert "error" in result

    @pytest.mark.asyncio
    async def test_wait_tool_returns_wait_status(self) -> None:
        result = await execute_wait({"reason": "Timing is wrong"})
        assert result["status"] == "wait"
        assert result["reason"] == "Timing is wrong"
        assert "until" not in result  # not set → not in output

    @pytest.mark.asyncio
    async def test_wait_tool_includes_until_when_provided(self) -> None:
        result = await execute_wait({
            "reason": "Jack is traveling",
            "until": "2026-05-10T09:00:00",
        })
        assert result["status"] == "wait"
        assert result["until"] == "2026-05-10T09:00:00"

    def test_wait_tool_schema_has_required_reason(self) -> None:
        fn = WAIT_TOOL_SCHEMA["function"]
        assert fn["name"] == "wait"
        params = fn["parameters"]
        assert "reason" in params["required"]
        assert "reason" in params["properties"]
        assert "until" in params["properties"]


# ── Unit: trace builder produces well-formed wait trace ──────────────────────


class TestWaitToolProducesTrace:
    def test_wait_tool_produces_null_output_trace(self) -> None:
        """A TraceBuilder.finish() with output='' and a wait tool call produces
        a well-formed Trace that can be distinguished as a wait trace.

        The trace has:
          - output == ""  (no response was surfaced)
          - tools_called contains the wait entry
          - assembled_context["is_wait"] is True (set by the wiring in core.py)
        """
        tb = TraceBuilder.start(input="morning brief context")
        tb.set_model("hermes3-local")
        tb.add_tool_call(
            name="wait",
            args={"reason": "Jack is still sleeping", "until": "08:30"},
            result_summary="status=wait",
        )
        # Simulate the is_wait marker that core.py adds.
        tb.assembled_context = {"is_wait": True}

        trace = tb.finish(output="", latency_ms=120)

        assert isinstance(trace, Trace)
        # output is empty string — no response was surfaced
        assert trace.output == ""
        # tools_called contains the wait
        assert len(trace.tools_called) == 1
        assert trace.tools_called[0]["name"] == "wait"
        assert trace.tools_called[0]["args"]["reason"] == "Jack is still sleeping"
        # is_wait marker is present
        assert trace.assembled_context.get("is_wait") is True

    def test_wait_trace_is_well_formed(self) -> None:
        """A wait trace passes all Trace.__post_init__ invariants."""
        tb = TraceBuilder.start(input="proactive check")
        tb.set_model("")
        tb.add_tool_call(
            name="wait",
            args={"reason": "nothing to surface"},
        )
        tb.assembled_context = {"is_wait": True}
        trace = tb.finish(output="", latency_ms=0)
        # Trace.__post_init__ runs; if it raises the test fails.
        assert trace.trace_id  # UUID is set


# ── Unit: scheduler treats wait as success ────────────────────────────────────


class TestSchedulerTreatsWaitAsSuccess:
    """The `wait` tool result is not an error; it is a valid non-action.

    This test verifies that a scheduler job whose pepper.chat() response
    included a wait tool call does NOT raise and does NOT look like a failure
    from the scheduler's perspective.

    We check this at the execute_wait level: the return value has
    status="wait", not "error". The scheduler's _audit call and _send path
    only fire on the response text — an empty response is fine (no exception).
    """

    @pytest.mark.asyncio
    async def test_wait_result_is_not_an_error(self) -> None:
        result = await execute_wait({"reason": "timing is wrong"})
        assert "error" not in result
        assert result.get("status") == "wait"

    @pytest.mark.asyncio
    async def test_scheduler_morning_brief_with_wait_does_not_raise(self) -> None:
        """Simulate a morning_brief job where Pepper called wait.

        The scheduler generates a brief via pepper.chat(). If the LLM chose
        to call wait, the response_text is "". The scheduler saves an empty
        brief and sends it. None of this should raise.
        """
        from agent.scheduler import PepperScheduler

        config = MagicMock()
        config.TIMEZONE = "UTC"
        config.MORNING_BRIEF_HOUR = 8
        config.MORNING_BRIEF_MINUTE = 0
        config.WEEKLY_REVIEW_DAY = "sun"
        config.WEEKLY_REVIEW_HOUR = 18

        pepper = MagicMock()
        # Pepper.chat returns empty string (the wait action produced no output).
        pepper.chat = AsyncMock(return_value="")
        pepper.memory = MagicMock()
        pepper.memory.save_to_recall = AsyncMock()
        pepper.memory.search_recall = AsyncMock(return_value=[])

        @asynccontextmanager
        async def _session_ctx():
            sess = MagicMock()
            sess.execute = AsyncMock(return_value=MagicMock())
            sess.commit = AsyncMock()
            sess.add = MagicMock()
            yield sess

        pepper.db_factory = _session_ctx

        sch = PepperScheduler(pepper, config)
        # Should not raise even with empty response text.
        result = await sch.generate_morning_brief()
        assert result == "" or result is not None  # no exception is the key assertion

    @pytest.mark.asyncio
    async def test_wait_tool_schema_available_on_all_turns(self) -> None:
        """WAIT_TOOL_SCHEMA is importable and has the expected structure.

        This is a smoke-test that the schema is registered correctly (the
        full registration is tested by inspecting the actual tool list built
        in core.py, but that requires the full stack to be wired).
        """
        assert WAIT_TOOL_SCHEMA["function"]["name"] == "wait"
        assert WAIT_TOOL_SCHEMA["type"] == "function"
