"""Tests for `agent.traces.emitter` — TraceBuilder accumulator and the
fail-soft `emit_trace` wrapper.

Covers the #22 acceptance criteria that don't require a live Postgres:

- TraceBuilder accumulates and finalizes correctly.
- `emit_trace` swallows persistence errors (fail-soft invariant).
- `emit_trace` does not log raw input/output text — structured metadata
  only (RAW_PERSONAL containment).
- `emit_trace` schedules the embedding worker only when the persist
  succeeds.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from structlog.testing import capture_logs

from agent.error_classifier import DataSensitivity
from agent.traces import (
    EMBEDDING_DIM,
    Trace,
    TraceTier,
    TriggerSource,
)
from agent.traces.emitter import TraceBuilder, _safe_error_message, emit_trace


class TestSafeErrorMessage:
    def test_redacts_sqlalchemy_parameters_block(self) -> None:
        # Simulate a SQLAlchemy-shaped error string with bound parameters
        # (which would carry RAW_PERSONAL substrings of `Trace.input`).
        class FakeStmtError(Exception):
            pass

        exc = FakeStmtError(
            "(psycopg.errors.UniqueViolation) duplicate key value\n"
            "[SQL: INSERT INTO traces (...) VALUES (...)]\n"
            "[parameters: ('secret-pii-string-from-input', ...)]",
        )
        msg = _safe_error_message(exc)
        assert "secret-pii-string-from-input" not in msg
        assert "[parameters" not in msg
        assert "FakeStmtError" in msg

    def test_keeps_short_diagnostic_for_plain_exceptions(self) -> None:
        msg = _safe_error_message(RuntimeError("DB unreachable"))
        assert "RuntimeError" in msg
        assert "DB unreachable" in msg

    def test_prefers_orig_attribute_when_present(self) -> None:
        # SQLAlchemy wraps the underlying DBAPI error in `.orig`.
        class WrappedExc(Exception):
            pass

        wrapped = WrappedExc("wrapper text")
        wrapped.orig = ConnectionError("postgres unreachable")
        msg = _safe_error_message(wrapped)
        assert "ConnectionError" in msg
        assert "postgres unreachable" in msg
        # The wrapper's own message (which might carry parameters) is not used.
        assert "wrapper text" not in msg


# ── TraceBuilder ──────────────────────────────────────────────────────────────


class TestTraceBuilder:
    def test_start_sets_provenance(self) -> None:
        tb = TraceBuilder.start(
            input="hi",
            trigger_source=TriggerSource.SCHEDULER,
            scheduler_job_name="morning_brief",
        )
        assert tb.input == "hi"
        assert tb.trigger_source is TriggerSource.SCHEDULER
        assert tb.scheduler_job_name == "morning_brief"

    def test_set_model_last_write_wins(self) -> None:
        tb = TraceBuilder.start(input="hi")
        tb.set_model("model-a", model_version="v1", prompt_version="p1")
        tb.set_model("model-b", model_version="v2", prompt_version="p2")
        t = tb.finish(output="ok", latency_ms=10)
        assert t.model_selected == "model-b"
        assert t.model_version == "v2"
        assert t.prompt_version == "p2"

    def test_add_tool_call_requires_name(self) -> None:
        tb = TraceBuilder.start(input="hi")
        with pytest.raises(ValueError, match="name is required"):
            tb.add_tool_call(name="")

    def test_add_tool_call_appends_in_order(self) -> None:
        tb = TraceBuilder.start(input="hi")
        tb.add_tool_call(name="search", success=True, latency_ms=12)
        tb.add_tool_call(name="send", success=False, error="rate_limited", latency_ms=42)
        t = tb.finish(output="done", latency_ms=100)
        assert [c["name"] for c in t.tools_called] == ["search", "send"]
        assert t.tools_called[1]["error"] == "rate_limited"
        assert t.tools_called[1]["success"] is False

    def test_finish_returns_frozen_trace(self) -> None:
        tb = TraceBuilder.start(input="hi")
        t = tb.finish(output="ok", latency_ms=5)
        assert isinstance(t, Trace)
        assert t.tier is TraceTier.WORKING
        # Frozen — verified by the dataclass tests, but smoke-check here.
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            t.input = "nope"  # type: ignore[misc]

    def test_finish_carries_data_sensitivity(self) -> None:
        tb = TraceBuilder.start(
            input="hi",
            data_sensitivity=DataSensitivity.SANITIZED,
        )
        t = tb.finish(output="ok", latency_ms=1)
        assert t.data_sensitivity is DataSensitivity.SANITIZED


# ── emit_trace ────────────────────────────────────────────────────────────────


def _stub_session_factory(
    *,
    flush_raises: Exception | None = None,
    refresh_returns_row=None,
):
    """Build a session_factory whose `async with` yields a mock AsyncSession.

    The session's `add` is a no-op, `flush` raises if requested, `refresh`
    populates the row's attributes from `refresh_returns_row`, `commit`
    no-ops, and the row passed to `add` is the same object returned via
    `refresh`.
    """
    @asynccontextmanager
    async def _session_ctx():
        sess = MagicMock()
        sess.add = MagicMock()
        sess.commit = AsyncMock()

        added_row = {}

        def _add(row):
            added_row["row"] = row

        sess.add = _add

        async def _flush():
            if flush_raises is not None:
                raise flush_raises

        async def _refresh(row):
            if refresh_returns_row is not None:
                # Stamp the row with the canonical "as persisted" attrs.
                for k, v in refresh_returns_row.items():
                    setattr(row, k, v)

        sess.flush = _flush
        sess.refresh = _refresh
        yield sess

    return _session_ctx


def _make_trace() -> Trace:
    return Trace(
        input="hello world",
        output="hi there",
        model_selected="hermes3-local",
        latency_ms=42,
    )


class TestEmitTraceFailSoft:
    @pytest.mark.asyncio
    async def test_persist_failure_returns_none_and_does_not_raise(self) -> None:
        trace = _make_trace()
        sf = _stub_session_factory(flush_raises=RuntimeError("DB down"))
        with capture_logs() as cap:
            result = await emit_trace(trace, session_factory=sf)
        assert result is None
        assert any(
            evt.get("event") == "trace_emit_failed" for evt in cap
        ), cap

    @pytest.mark.asyncio
    async def test_persist_failure_does_not_log_raw_payload(self) -> None:
        trace = _make_trace()
        sf = _stub_session_factory(flush_raises=RuntimeError("boom"))
        with capture_logs() as cap:
            await emit_trace(trace, session_factory=sf)
        # The fail-soft log line carries error metadata only — no raw
        # input/output should appear in the event payload.
        for evt in cap:
            for value in evt.values():
                if isinstance(value, str):
                    assert trace.input not in value, evt
                    assert trace.output not in value, evt


class TestEmitTraceMetadataOnly:
    @pytest.mark.asyncio
    async def test_success_log_contains_only_metadata(self) -> None:
        # Use TraceRepository's real append shape via a stub that accepts
        # the row and returns the row unchanged on refresh.
        with patch("agent.traces.emitter.TraceRepository") as repo_cls:
            repo = MagicMock()
            persisted = MagicMock()
            persisted.trace_id = "11111111-1111-1111-1111-111111111111"
            repo.append = AsyncMock(return_value=persisted)
            repo_cls.return_value = repo

            sf = _stub_session_factory()

            trace = _make_trace()
            with capture_logs() as cap:
                result = await emit_trace(trace, session_factory=sf)

        assert result == "11111111-1111-1111-1111-111111111111"
        ok_logs = [evt for evt in cap if evt.get("event") == "trace_emit_ok"]
        assert ok_logs, cap
        ok = ok_logs[0]
        # Allowed metadata.
        assert ok["trace_id"] == "11111111-1111-1111-1111-111111111111"
        assert ok["archetype"] == trace.archetype.value
        assert ok["latency_ms"] == trace.latency_ms
        # Disallowed RAW_PERSONAL — verify by exact-match scan over values.
        for value in ok.values():
            if isinstance(value, str):
                assert trace.input not in value
                assert trace.output not in value


class TestEmitTraceEmbedScheduling:
    @pytest.mark.asyncio
    async def test_embed_worker_skipped_when_persist_fails(self) -> None:
        trace = _make_trace()
        sf = _stub_session_factory(flush_raises=RuntimeError("boom"))
        embed_called = False

        async def _embed(text: str) -> list[float]:
            nonlocal embed_called
            embed_called = True
            return [0.0] * EMBEDDING_DIM

        await emit_trace(
            trace,
            session_factory=sf,
            embed_fn=_embed,
            embed_model_version="qwen3-embedding:0.6b",
        )
        # Persistence failed → embedding worker must not be scheduled.
        assert embed_called is False

    @pytest.mark.asyncio
    async def test_embed_worker_skipped_when_no_embed_fn(self) -> None:
        # No embed_fn → no worker. Just confirm we don't crash and we
        # return the trace_id from append.
        trace = _make_trace()
        with patch("agent.traces.emitter.TraceRepository") as repo_cls:
            repo = MagicMock()
            persisted = MagicMock()
            persisted.trace_id = "22222222-2222-2222-2222-222222222222"
            repo.append = AsyncMock(return_value=persisted)
            repo_cls.return_value = repo

            sf = _stub_session_factory()
            result = await emit_trace(trace, session_factory=sf)
        assert result == "22222222-2222-2222-2222-222222222222"


# Re-bind structlog to non-capture for any subsequent tests in the suite.
@pytest.fixture(autouse=True)
def _reset_structlog():
    yield
    structlog.reset_defaults()
