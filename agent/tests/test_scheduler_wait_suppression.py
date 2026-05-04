"""Scheduler wait-suppression tests (#55).

When a scheduled brief turn ends in a `wait` tool call, the scheduler
must:
- still treat the run as success (no exception, returns the brief text)
- NOT call `_send` (no user-facing notification)
- NOT call `pepper.memory.save_to_recall` (no MORNING BRIEF recall row)
- emit `morning_brief_waited` to the audit log
- still update `_last_brief` (the daily slot is consumed)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.scheduler import PepperScheduler
from agent.wait_tool import Wait


def _make_config():
    config = MagicMock()
    config.TIMEZONE = "UTC"
    config.MORNING_BRIEF_HOUR = 8
    config.MORNING_BRIEF_MINUTE = 0
    config.WEEKLY_REVIEW_DAY = "sun"
    config.WEEKLY_REVIEW_HOUR = 18
    return config


def _make_pepper_with_wait(wait_obj):
    pepper = MagicMock()
    pepper.chat = AsyncMock(return_value="")
    pepper.memory = MagicMock()
    pepper.memory.save_to_recall = AsyncMock()
    pepper.memory.search_recall = AsyncMock(return_value=[])

    pepper.waits = MagicMock()
    pepper.waits.consume_latest = MagicMock(return_value=wait_obj)

    @asynccontextmanager
    async def _session_ctx():
        sess = MagicMock()
        sess.execute = AsyncMock(return_value=MagicMock())
        sess.commit = AsyncMock()
        sess.add = MagicMock()
        yield sess

    pepper.db_factory = _session_ctx
    return pepper


class TestMorningBriefWaitSuppression:
    @pytest.mark.asyncio
    async def test_wait_skips_send_and_recall(self) -> None:
        wait = Wait(reason="Jack already addressed this in last night's chat.")
        pepper = _make_pepper_with_wait(wait)
        sch = PepperScheduler(pepper, _make_config())
        # Replace _send so we can assert the no-send invariant.
        sch._send = AsyncMock()

        result = await sch.generate_morning_brief()

        # Brief returned successfully — wait is treated as success.
        assert result is not None
        # No send to the user.
        sch._send.assert_not_awaited()
        # No save_to_recall — the brief was held back, nothing to remember.
        pepper.memory.save_to_recall.assert_not_awaited()
        # The wait was consumed exactly once.
        assert pepper.waits.consume_latest.call_count == 1

    @pytest.mark.asyncio
    async def test_no_wait_keeps_existing_send_behaviour(self) -> None:
        # Default: consume_latest returns None — normal send path.
        pepper = _make_pepper_with_wait(None)
        pepper.chat = AsyncMock(return_value="hello, here is your brief")
        sch = PepperScheduler(pepper, _make_config())
        sch._send = AsyncMock()

        await sch.generate_morning_brief()

        sch._send.assert_awaited_once()
        pepper.memory.save_to_recall.assert_awaited_once()
