"""Tests for #23 — scheduler-side trace emission and the
`reflector_trigger` Postgres NOTIFY signal.

Each scheduled job that calls `pepper.chat()` must pass
`trigger_source=TriggerSource.SCHEDULER` and the corresponding
`scheduler_job_name`. The `reflector_trigger` job fires a
NOTIFY on the documented channel with a date-only payload.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.scheduler import REFLECTOR_TRIGGER_CHANNEL, PepperScheduler
from agent.traces import TriggerSource


def _make_config():
    config = MagicMock()
    config.TIMEZONE = "UTC"
    config.MORNING_BRIEF_HOUR = 8
    config.MORNING_BRIEF_MINUTE = 0
    config.WEEKLY_REVIEW_DAY = "sun"
    config.WEEKLY_REVIEW_HOUR = 18
    return config


def _make_pepper(*, with_db: bool = True):
    pepper = MagicMock()
    pepper.chat = AsyncMock(return_value="response text")
    pepper.memory = MagicMock()
    pepper.memory.save_to_recall = AsyncMock()
    pepper.memory.search_recall = AsyncMock(return_value=[])

    if with_db:
        executed_sql: list[str] = []

        @asynccontextmanager
        async def _session_ctx():
            sess = MagicMock()

            async def _execute(stmt):
                executed_sql.append(getattr(stmt, "text", str(stmt)))
                return MagicMock()

            sess.execute = AsyncMock(side_effect=_execute)
            sess.commit = AsyncMock()
            sess.add = MagicMock()
            yield sess

        pepper.db_factory = _session_ctx
        pepper._executed_sql = executed_sql
    else:
        pepper.db_factory = None

    return pepper


class TestSchedulerJobsCarryTriggerSource:
    @pytest.mark.asyncio
    async def test_morning_brief_emits_scheduler_trace(self) -> None:
        pepper = _make_pepper()
        sch = PepperScheduler(pepper, _make_config())
        await sch.generate_morning_brief()
        pepper.chat.assert_awaited_once()
        kwargs = pepper.chat.await_args.kwargs
        assert kwargs["trigger_source"] is TriggerSource.SCHEDULER
        assert kwargs["scheduler_job_name"] == "morning_brief"
        assert kwargs["isolated"] is True

    @pytest.mark.asyncio
    async def test_weekly_review_emits_scheduler_trace(self) -> None:
        pepper = _make_pepper()
        sch = PepperScheduler(pepper, _make_config())
        await sch.generate_weekly_review()
        pepper.chat.assert_awaited_once()
        kwargs = pepper.chat.await_args.kwargs
        assert kwargs["trigger_source"] is TriggerSource.SCHEDULER
        assert kwargs["scheduler_job_name"] == "weekly_review"

    @pytest.mark.asyncio
    async def test_commitment_check_emits_scheduler_trace_when_items_open(
        self,
    ) -> None:
        pepper = _make_pepper()
        # Force at least one open commitment so the chat path runs.
        pepper.memory.search_recall = AsyncMock(
            return_value=[
                {
                    "content": "COMMITMENT: ship the trace substrate",
                    "created_at": "2025-01-01T00:00:00+00:00",
                },
            ],
        )
        sch = PepperScheduler(pepper, _make_config())
        await sch.check_commitments()
        pepper.chat.assert_awaited_once()
        kwargs = pepper.chat.await_args.kwargs
        assert kwargs["trigger_source"] is TriggerSource.SCHEDULER
        assert kwargs["scheduler_job_name"] == "commitment_check"

    @pytest.mark.asyncio
    async def test_commitment_check_skips_chat_when_no_open_items(
        self,
    ) -> None:
        # The no-open-items short-circuit predates Epic 01 — verify it
        # still doesn't emit a (rogue) trace by calling chat at all.
        pepper = _make_pepper()
        pepper.memory.search_recall = AsyncMock(return_value=[])
        sch = PepperScheduler(pepper, _make_config())
        await sch.check_commitments()
        pepper.chat.assert_not_awaited()


class TestReflectorTrigger:
    @pytest.mark.asyncio
    async def test_fires_notify_on_documented_channel(self) -> None:
        pepper = _make_pepper(with_db=True)
        sch = PepperScheduler(pepper, _make_config())
        ok = await sch.fire_reflector_trigger()
        assert ok is True
        # Exactly one SQL statement was executed: the NOTIFY.
        assert len(pepper._executed_sql) == 1
        sql = pepper._executed_sql[0]
        assert sql.startswith(f"NOTIFY {REFLECTOR_TRIGGER_CHANNEL}")
        # Payload is signal-only (a date string), never trace contents.
        # Just confirm there's no jsonb / SELECT / trace text in there.
        assert "trace" not in sql.lower()
        assert "SELECT" not in sql

    @pytest.mark.asyncio
    async def test_skipped_when_db_factory_missing(self) -> None:
        pepper = _make_pepper(with_db=False)
        sch = PepperScheduler(pepper, _make_config())
        ok = await sch.fire_reflector_trigger()
        assert ok is False  # graceful skip, no raise

    @pytest.mark.asyncio
    async def test_failure_does_not_raise(self) -> None:
        pepper = _make_pepper(with_db=True)
        # Replace db_factory with one whose session.execute raises.
        @asynccontextmanager
        async def _broken():
            sess = MagicMock()
            sess.execute = AsyncMock(side_effect=RuntimeError("DB down"))
            sess.commit = AsyncMock()
            sess.add = MagicMock()
            yield sess

        pepper.db_factory = _broken
        sch = PepperScheduler(pepper, _make_config())
        ok = await sch.fire_reflector_trigger()
        assert ok is False  # fail-soft

    def test_channel_name_is_documented_constant(self) -> None:
        # ADR-0005 / #23 spec name. If anyone renames the channel they
        # must update both the scheduler and the reflector LISTEN side.
        assert REFLECTOR_TRIGGER_CHANNEL == "reflector_trigger"
