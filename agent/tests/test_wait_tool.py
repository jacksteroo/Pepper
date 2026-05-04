"""Unit tests for `agent.wait_tool` (#55).

Covers the issue's acceptance contract:
- wait tool produces a well-formed result with `reason` populated
- wait without `reason` fails validation (the field is required)
- ISO `until` is parsed; non-ISO `until` is preserved as-is and not parsed
- WaitsRegistry consume_latest is one-shot
- WaitsRegistry per-session capacity is bounded (no leak)
- WAIT_TOOLS schema declares `reason` required and is JSON-schema-shaped
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.wait_tool import (
    MAX_REASON_LEN,
    MAX_UNTIL_LEN,
    WAIT_TOOLS,
    Wait,
    WaitsRegistry,
    execute_wait,
)


# ── Schema contract ──────────────────────────────────────────────────────────


class TestWaitToolSchema:
    def test_single_tool_named_wait(self) -> None:
        assert len(WAIT_TOOLS) == 1
        assert WAIT_TOOLS[0]["function"]["name"] == "wait"

    def test_reason_is_required(self) -> None:
        params = WAIT_TOOLS[0]["function"]["parameters"]
        assert "reason" in params["required"]
        assert "until" not in params.get("required", [])

    def test_no_side_effects_flag(self) -> None:
        # Wait is a non-action: no external side effects.
        assert WAIT_TOOLS[0]["side_effects"] is False


# ── Validation ───────────────────────────────────────────────────────────────


class TestExecuteWaitValidation:
    @pytest.mark.asyncio
    async def test_missing_reason_returns_error(self) -> None:
        registry = WaitsRegistry()
        result = await execute_wait({}, registry=registry, session_id="s1")
        assert "error" in result
        assert "reason" in result["error"]
        # Nothing recorded.
        assert registry.peek_latest("s1") is None

    @pytest.mark.asyncio
    async def test_empty_reason_returns_error(self) -> None:
        registry = WaitsRegistry()
        result = await execute_wait(
            {"reason": "   "}, registry=registry, session_id="s1"
        )
        assert "error" in result
        assert registry.peek_latest("s1") is None

    @pytest.mark.asyncio
    async def test_non_string_reason_returns_error(self) -> None:
        registry = WaitsRegistry()
        result = await execute_wait(
            {"reason": 42}, registry=registry, session_id="s1"
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_reason_length_bounded(self) -> None:
        registry = WaitsRegistry()
        too_long = "a" * (MAX_REASON_LEN + 1)
        result = await execute_wait(
            {"reason": too_long}, registry=registry, session_id="s1"
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_until_length_bounded(self) -> None:
        registry = WaitsRegistry()
        too_long = "x" * (MAX_UNTIL_LEN + 1)
        result = await execute_wait(
            {"reason": "ok", "until": too_long},
            registry=registry,
            session_id="s1",
        )
        assert "error" in result


# ── Happy path ───────────────────────────────────────────────────────────────


class TestExecuteWaitHappyPath:
    @pytest.mark.asyncio
    async def test_minimal_reason_recorded(self) -> None:
        registry = WaitsRegistry()
        result = await execute_wait(
            {"reason": "Jack already addressed this in the morning brief."},
            registry=registry,
            session_id="s1",
        )
        assert result == {
            "ok": True,
            "waited": True,
            "reason": "Jack already addressed this in the morning brief.",
            "until": None,
            "until_iso": None,
        }
        wait = registry.peek_latest("s1")
        assert wait is not None
        assert wait.reason.startswith("Jack already")
        assert wait.session_id == "s1"

    @pytest.mark.asyncio
    async def test_iso_until_parsed_with_z(self) -> None:
        registry = WaitsRegistry()
        result = await execute_wait(
            {"reason": "after meeting", "until": "2026-05-04T17:00:00Z"},
            registry=registry,
            session_id="s1",
        )
        assert result["ok"] is True
        assert result["until"] == "2026-05-04T17:00:00Z"
        assert result["until_iso"] is not None
        wait = registry.peek_latest("s1")
        assert wait is not None
        assert wait.until_iso is not None
        assert wait.until_iso.tzinfo is not None
        assert wait.until_iso == datetime(2026, 5, 4, 17, 0, 0, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_natural_language_until_kept_as_raw(self) -> None:
        registry = WaitsRegistry()
        result = await execute_wait(
            {"reason": "ok", "until": "after the meeting"},
            registry=registry,
            session_id="s1",
        )
        assert result["until"] == "after the meeting"
        # Cannot parse — until_iso must be None, not a guess.
        assert result["until_iso"] is None
        wait = registry.peek_latest("s1")
        assert wait is not None
        assert wait.until_raw == "after the meeting"
        assert wait.until_iso is None

    @pytest.mark.asyncio
    async def test_iso_without_zone_treated_as_utc(self) -> None:
        registry = WaitsRegistry()
        result = await execute_wait(
            {"reason": "ok", "until": "2026-05-04T17:00:00"},
            registry=registry,
            session_id="s1",
        )
        assert result["until_iso"] is not None
        wait = registry.peek_latest("s1")
        assert wait is not None
        assert wait.until_iso is not None and wait.until_iso.tzinfo is not None


# ── Registry ─────────────────────────────────────────────────────────────────


class TestWaitsRegistry:
    def test_consume_latest_is_one_shot(self) -> None:
        registry = WaitsRegistry()
        registry.record(Wait(reason="r", session_id="s1"))
        first = registry.consume_latest("s1")
        assert first is not None
        # Second consume returns None — the bucket was popped.
        second = registry.consume_latest("s1")
        assert second is None

    def test_consume_returns_most_recent(self) -> None:
        registry = WaitsRegistry()
        registry.record(Wait(reason="older", session_id="s1"))
        registry.record(Wait(reason="newer", session_id="s1"))
        latest = registry.consume_latest("s1")
        assert latest is not None
        assert latest.reason == "newer"

    def test_per_session_capacity_bounds_memory(self) -> None:
        registry = WaitsRegistry(per_session_capacity=2)
        for i in range(5):
            registry.record(Wait(reason=f"r{i}", session_id="s1"))
        # After 5 records, the deque holds the last 2.
        latest = registry.consume_latest("s1")
        assert latest is not None
        assert latest.reason == "r4"
        latest2 = registry.consume_latest("s1")
        assert latest2 is not None
        assert latest2.reason == "r3"

    def test_sessions_are_isolated(self) -> None:
        registry = WaitsRegistry()
        registry.record(Wait(reason="for s1", session_id="s1"))
        # Other session sees nothing.
        assert registry.consume_latest("s2") is None
        # Original session still has its wait.
        latest = registry.consume_latest("s1")
        assert latest is not None
        assert latest.reason == "for s1"

    def test_unknown_session_returns_none(self) -> None:
        registry = WaitsRegistry()
        assert registry.consume_latest("never-seen") is None
        assert registry.peek_latest("never-seen") is None
