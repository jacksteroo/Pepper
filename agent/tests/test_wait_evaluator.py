"""Tests for `agents.reflector.wait_evaluator` (#56).

Covers:
- pure comparator helpers (jaccard, evaluate_relevance, evaluate_brought_up)
- WaitFeedback dataclass invariants
- evaluate_completed_waits is idempotent (re-runs do not duplicate)
- evaluate_brought_up_waits defers waits whose 7d window is still open
- summarize_feedback aggregates correctly
"""
from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from agents.reflector.wait_evaluator import (
    BROUGHT_UP_WINDOW,
    RELEVANCE_HALF_WINDOW,
    SIGNAL_BROUGHT_UP,
    SIGNAL_RELEVANCE,
    SIGNAL_THUMBS,
    TraceLite,
    WaitFeedback,
    WaitFeedbackRepository,
    evaluate_brought_up,
    evaluate_brought_up_waits,
    evaluate_completed_waits,
    evaluate_relevance,
    summarize_feedback,
)


def _ts(days_ago: int = 0, hours_ago: int = 0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)


def _wait(reason: str, *, until_iso=None, created_at=None, input_text="") -> TraceLite:
    return TraceLite(
        trace_id=uuid.uuid4(),
        created_at=created_at or _ts(),
        input=input_text,
        wait_reason=reason,
        until_iso=until_iso,
    )


def _trace(input_text: str, *, created_at=None, source="user") -> TraceLite:
    return TraceLite(
        trace_id=uuid.uuid4(),
        created_at=created_at or _ts(),
        input=input_text,
        trigger_source=source,
    )


# ── Dataclass invariants ─────────────────────────────────────────────────────


class TestFeedbackDataclass:
    def test_invalid_signal_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="signal_type"):
            WaitFeedback(
                wait_trace_id=uuid.uuid4(),
                signal_type="frobnicate",
                signal_value=1.0,
                confidence=1.0,
            )

    def test_signal_value_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="signal_value"):
            WaitFeedback(
                wait_trace_id=uuid.uuid4(),
                signal_type=SIGNAL_THUMBS,
                signal_value=1.5,
                confidence=1.0,
            )

    def test_confidence_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            WaitFeedback(
                wait_trace_id=uuid.uuid4(),
                signal_type=SIGNAL_THUMBS,
                signal_value=1.0,
                confidence=2.0,
            )


# ── evaluate_relevance (§2.1) ────────────────────────────────────────────────


class TestEvaluateRelevance:
    def test_match_returns_one_with_scaled_confidence(self) -> None:
        wait = _wait(
            "Jack mentioned the email triage but seemed tired",
            until_iso=_ts(),
            input_text="email triage",
        )
        nearby = [
            _trace("did the email triage land?", created_at=_ts(hours_ago=2)),
        ]
        fb = evaluate_relevance(wait_trace=wait, nearby_traces=nearby)
        assert fb.signal_type == SIGNAL_RELEVANCE
        assert fb.signal_value == 1.0
        # 1 match / 3.0 cap = ~0.33
        assert 0.3 < fb.confidence < 0.4

    def test_no_match_returns_zero_ambiguous(self) -> None:
        wait = _wait("hold the email triage", until_iso=_ts(), input_text="email triage")
        nearby = [_trace("calendar sync running", created_at=_ts(hours_ago=2))]
        fb = evaluate_relevance(wait_trace=wait, nearby_traces=nearby)
        assert fb.signal_value == 0.0
        # Ambiguity recorded as confidence=0.5 — neither correct nor incorrect.
        assert fb.confidence == 0.5

    def test_until_iso_required(self) -> None:
        wait = _wait("ok", until_iso=None)
        with pytest.raises(ValueError, match="until_iso"):
            evaluate_relevance(wait_trace=wait, nearby_traces=[])

    def test_three_or_more_matches_saturates_confidence(self) -> None:
        wait = _wait("hold email triage", until_iso=_ts(), input_text="email triage")
        nearby = [
            _trace("the email triage is done"),
            _trace("did email triage finish"),
            _trace("email triage status"),
            _trace("email triage update"),
        ]
        fb = evaluate_relevance(wait_trace=wait, nearby_traces=nearby)
        assert fb.confidence == 1.0


# ── evaluate_brought_up (§2.2) ───────────────────────────────────────────────


class TestEvaluateBroughtUp:
    def test_user_brings_up_returns_one(self) -> None:
        wait = _wait("hold email triage", input_text="email triage")
        later = [
            _trace(
                "did the email triage land",
                created_at=_ts(days_ago=2),
                source="user",
            ),
        ]
        fb = evaluate_brought_up(wait_trace=wait, later_traces=later)
        assert fb.signal_type == SIGNAL_BROUGHT_UP
        assert fb.signal_value == 1.0
        assert fb.confidence == 1.0

    def test_scheduler_traces_dont_count(self) -> None:
        """Scheduler-fired briefs that surface the topic don't prove
        Jack brought it up — only user-driven traces do."""
        wait = _wait("hold email triage", input_text="email triage")
        later = [
            _trace(
                "email triage sweep",
                created_at=_ts(days_ago=2),
                source="scheduler",
            ),
        ]
        fb = evaluate_brought_up(wait_trace=wait, later_traces=later)
        assert fb.signal_value == 0.0


# ── Orchestrator — idempotency and deferral ─────────────────────────────────


class _StubFeedbackRepo:
    """Records appends + serves list_by_wait queries from the in-memory store."""

    def __init__(self) -> None:
        self.appended: list[WaitFeedback] = []

    async def append(self, fb: WaitFeedback) -> WaitFeedback:
        self.appended.append(fb)
        return fb

    async def list_by_wait(self, wait_trace_id, *, signal_type=None):
        sid = wait_trace_id if isinstance(wait_trace_id, uuid.UUID) else uuid.UUID(str(wait_trace_id))
        return [
            f
            for f in self.appended
            if f.wait_trace_id == sid
            and (signal_type is None or f.signal_type == signal_type)
        ]

    async def list_recent(self, *, days: int = 7):
        return list(self.appended)


class TestEvaluateCompletedWaits:
    @pytest.mark.asyncio
    async def test_appends_one_per_wait_idempotent(self) -> None:
        wait = _wait(
            "hold email triage",
            until_iso=_ts(hours_ago=1),
            input_text="email triage",
        )
        nearby = [_trace("email triage done", created_at=_ts(hours_ago=1))]
        repo = _StubFeedbackRepo()

        async def fetch_completed_waits(start, end):
            return [wait]

        async def fetch_traces_in_window(start, end):
            return nearby

        n1 = await evaluate_completed_waits(
            now=_ts(),
            fetch_completed_waits=fetch_completed_waits,
            fetch_traces_in_window=fetch_traces_in_window,
            feedback_repo=repo,
        )
        assert n1 == 1
        # Second pass must not re-write the relevance signal for the
        # same wait — the comparator's idempotency check catches it.
        n2 = await evaluate_completed_waits(
            now=_ts(),
            fetch_completed_waits=fetch_completed_waits,
            fetch_traces_in_window=fetch_traces_in_window,
            feedback_repo=repo,
        )
        assert n2 == 0
        # Total appends still 1.
        assert sum(1 for f in repo.appended if f.signal_type == SIGNAL_RELEVANCE) == 1


class TestEvaluateBroughtUpWaits:
    @pytest.mark.asyncio
    async def test_open_window_with_no_match_defers(self) -> None:
        # Wait fired 1 day ago — window still has 6 days to run.
        wait = _wait(
            "hold email triage",
            created_at=_ts(days_ago=1),
            input_text="email triage",
        )
        repo = _StubFeedbackRepo()

        async def fetch_recent_waits(start, end):
            return [wait]

        async def fetch_traces_in_window(start, end):
            return []  # No matching traces yet.

        n = await evaluate_brought_up_waits(
            now=_ts(),
            fetch_recent_waits=fetch_recent_waits,
            fetch_traces_in_window=fetch_traces_in_window,
            feedback_repo=repo,
        )
        # Deferred — no row written.
        assert n == 0
        assert repo.appended == []

    @pytest.mark.asyncio
    async def test_closed_window_with_no_match_writes_zero(self) -> None:
        # Wait fired 8 days ago — window closed yesterday.
        wait = _wait(
            "hold email triage",
            created_at=_ts(days_ago=8),
            input_text="email triage",
        )
        repo = _StubFeedbackRepo()

        async def fetch_recent_waits(start, end):
            return [wait]

        async def fetch_traces_in_window(start, end):
            return []

        n = await evaluate_brought_up_waits(
            now=_ts(),
            fetch_recent_waits=fetch_recent_waits,
            fetch_traces_in_window=fetch_traces_in_window,
            feedback_repo=repo,
        )
        # Window closed; no match found — 0.0 row is written.
        assert n == 1
        assert repo.appended[0].signal_value == 0.0

    @pytest.mark.asyncio
    async def test_match_found_writes_one(self) -> None:
        wait = _wait(
            "hold email triage",
            created_at=_ts(days_ago=2),
            input_text="email triage",
        )
        repo = _StubFeedbackRepo()

        async def fetch_recent_waits(start, end):
            return [wait]

        async def fetch_traces_in_window(start, end):
            return [
                _trace("email triage status", created_at=_ts(days_ago=1), source="user")
            ]

        n = await evaluate_brought_up_waits(
            now=_ts(),
            fetch_recent_waits=fetch_recent_waits,
            fetch_traces_in_window=fetch_traces_in_window,
            feedback_repo=repo,
        )
        assert n == 1
        assert repo.appended[0].signal_value == 1.0


# ── summarize_feedback ───────────────────────────────────────────────────────


class TestSummarizeFeedback:
    def test_means_per_signal_type(self) -> None:
        records = [
            WaitFeedback(uuid.uuid4(), SIGNAL_THUMBS, 1.0, 1.0),
            WaitFeedback(uuid.uuid4(), SIGNAL_THUMBS, 0.0, 1.0),
            WaitFeedback(uuid.uuid4(), SIGNAL_BROUGHT_UP, 1.0, 1.0),
        ]
        summary = summarize_feedback(records)
        assert summary.total == 3
        assert summary.by_signal_value_mean[SIGNAL_THUMBS] == 0.5
        assert summary.by_signal_value_mean[SIGNAL_BROUGHT_UP] == 1.0
        # 1 thumbs-down at confidence 1.0 = ambiguous-high.
        assert summary.ambiguous_count == 1

    def test_ambiguous_count_only_for_high_confidence_zeros(self) -> None:
        # Auto-signals with confidence 0.5 are NOT counted as ambiguous-high.
        records = [
            WaitFeedback(uuid.uuid4(), SIGNAL_RELEVANCE, 0.0, 0.5),
            WaitFeedback(uuid.uuid4(), SIGNAL_BROUGHT_UP, 0.0, 0.5),
        ]
        summary = summarize_feedback(records)
        assert summary.ambiguous_count == 0


# ── Repository surface lock ──────────────────────────────────────────────────


class TestRepositorySurface:
    expected_public: frozenset[str] = frozenset({
        "append",
        "list_by_wait",
        "list_recent",
    })

    def test_public_method_set_is_exhaustive(self) -> None:
        names = {
            n
            for n, _ in inspect.getmembers(WaitFeedbackRepository, predicate=inspect.isfunction)
            if not n.startswith("_")
        }
        assert names == self.expected_public

    def test_no_destructive_paths(self) -> None:
        """Per #56: 'No automated learning loop — feedback is read-only signal.'
        The repository must not expose update/delete paths."""
        forbidden = ("update", "edit", "rewrite", "delete", "purge", "drop")
        names = {n for n, _ in inspect.getmembers(WaitFeedbackRepository)}
        bad = [n for n in names if any(n.startswith(p) for p in forbidden)]
        assert bad == []
