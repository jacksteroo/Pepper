"""Trace-grounded feedback loop for wait-actions (#56).

The reflector batch-evaluates every wait it can match a signal for and
appends `WaitFeedback` rows. Three signal types are documented in
`docs/wait-action-feedback.md`:

- `was_the_thing_still_relevant` (per #56 §2.1) — auto-computed when
  the wait's `until_iso` is in the past.
- `did_jack_later_bring_it_up` (per #56 §2.2) — auto-computed for
  every wait in the last 7 days; idempotent via list-by-wait check.
- `explicit_thumbs` (per #56 §2.3) — written by the HTTP route the
  Waits panel calls.

Implementation details:

- This is a feedback signal, not an automated learning loop. Module
  docstrings, table comments, and tests all assert this — adding any
  automated promotion of "always wait in similar situations" requires
  a new ADR.
- Similarity v0 = Jaccard token overlap. The same tokeniser
  (`agent.strategies_tools._tokenize`) is reused so the threshold
  semantics match the strategy ranker's.
- All three signals run locally over local traces. No external API.

This module is intentionally framework-agnostic: the public functions
take a "trace fetcher" callable (returning lists of `Trace`) and a
`WaitFeedbackRepository` and produce `WaitFeedback` rows. The
reflector wires real callables; tests inject stubs.
"""
from __future__ import annotations

import uuid as _uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

import structlog
from sqlalchemy import DateTime, Float, Index, String, Text, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from agent.db import Base
from agent.strategies_tools import _tokenize

logger = structlog.get_logger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


SIGNAL_RELEVANCE = "was_the_thing_still_relevant"
SIGNAL_BROUGHT_UP = "did_jack_later_bring_it_up"
SIGNAL_THUMBS = "explicit_thumbs"

_VALID_SIGNALS: frozenset[str] = frozenset({
    SIGNAL_RELEVANCE,
    SIGNAL_BROUGHT_UP,
    SIGNAL_THUMBS,
})

# Jaccard threshold for "same context." v0; phase 2 swaps for cosine
# over the trace embedding column.
SIMILARITY_THRESHOLD: float = 0.20

# Window around `until_iso` for the relevance signal.
RELEVANCE_HALF_WINDOW = timedelta(hours=24)

# Window after `created_at` for the brought-up signal.
BROUGHT_UP_WINDOW = timedelta(days=7)


# ── Data shape ───────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_feedback_id() -> _uuid.UUID:
    return _uuid.uuid4()


@dataclass
class WaitFeedback:
    """One feedback record about a wait trace.

    Frozen-shaped at the dataclass level (no setattrs after
    construction) so a constructed feedback row is the row that gets
    persisted; correctness invariants are checked once at __post_init__.
    """

    wait_trace_id: _uuid.UUID
    signal_type: str
    signal_value: float
    confidence: float
    feedback_id: _uuid.UUID = field(default_factory=_new_feedback_id)
    created_at: datetime = field(default_factory=_utcnow)
    notes: Optional[str] = None

    def __post_init__(self) -> None:
        if self.signal_type not in _VALID_SIGNALS:
            raise ValueError(
                f"signal_type must be one of {sorted(_VALID_SIGNALS)}, "
                f"got {self.signal_type!r}"
            )
        if not 0.0 <= self.signal_value <= 1.0:
            raise ValueError(
                f"signal_value must be in [0.0, 1.0], got {self.signal_value!r}"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {self.confidence!r}"
            )


class WaitFeedbackRow(Base):
    __tablename__ = "wait_feedback"

    feedback_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid.uuid4
    )
    wait_trace_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    signal_type: Mapped[str] = mapped_column(String(64), nullable=False)
    signal_value: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_wait_feedback_wait_trace_id", "wait_trace_id"),
        Index("idx_wait_feedback_signal_type", "signal_type"),
        Index("idx_wait_feedback_created_at", "created_at"),
    )


def _row_to_dataclass(row: WaitFeedbackRow) -> WaitFeedback:
    return WaitFeedback(
        wait_trace_id=row.wait_trace_id,
        signal_type=row.signal_type,
        signal_value=row.signal_value,
        confidence=row.confidence,
        feedback_id=row.feedback_id,
        created_at=row.created_at,
        notes=row.notes,
    )


class WaitFeedbackRepository:
    """Append-only repository for wait_feedback rows.

    Public surface: append, list_by_wait, list_recent. There is no
    update or delete — the audit trail is the point.

    `explicit_thumbs` writes are allowed to land repeatedly for the
    same wait; the latest by `created_at` wins. The two automatic
    signals are gated by the comparator's `list_by_wait` idempotency
    check before writing.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, feedback: WaitFeedback) -> WaitFeedback:
        row = WaitFeedbackRow(
            feedback_id=feedback.feedback_id,
            wait_trace_id=feedback.wait_trace_id,
            signal_type=feedback.signal_type,
            signal_value=feedback.signal_value,
            confidence=feedback.confidence,
            created_at=feedback.created_at,
            notes=feedback.notes,
        )
        self._session.add(row)
        await self._session.flush()
        return feedback

    async def list_by_wait(
        self, wait_trace_id, *, signal_type: Optional[str] = None
    ) -> list[WaitFeedback]:
        sid = (
            wait_trace_id
            if isinstance(wait_trace_id, _uuid.UUID)
            else _uuid.UUID(str(wait_trace_id))
        )
        stmt = select(WaitFeedbackRow).where(
            WaitFeedbackRow.wait_trace_id == sid
        )
        if signal_type is not None:
            stmt = stmt.where(WaitFeedbackRow.signal_type == signal_type)
        stmt = stmt.order_by(WaitFeedbackRow.created_at.desc())
        result = await self._session.execute(stmt)
        return [_row_to_dataclass(r) for r in result.scalars().all()]

    async def list_recent(self, *, days: int = 7) -> list[WaitFeedback]:
        if days <= 0:
            raise ValueError("days must be positive")
        since = _utcnow() - timedelta(days=days)
        stmt = (
            select(WaitFeedbackRow)
            .where(WaitFeedbackRow.created_at >= since)
            .order_by(WaitFeedbackRow.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return [_row_to_dataclass(r) for r in result.scalars().all()]


# ── Pure comparator helpers (testable without DB) ────────────────────────────


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    if not inter:
        return 0.0
    union = a | b
    return len(inter) / len(union)


@dataclass
class TraceLite:
    """Minimum trace shape the comparator needs.

    Decoupled from `agent.traces.schema.Trace` so tests can construct
    these inline without dragging in the full ORM.
    """

    trace_id: _uuid.UUID
    created_at: datetime
    input: str
    trigger_source: str = "user"
    until_iso: Optional[datetime] = None
    wait_reason: Optional[str] = None


def _wait_tokens(wait_trace: TraceLite) -> set[str]:
    """Tokens that represent the wait's context.

    The wait's input is the user message that triggered the turn; the
    reason adds context the model identified explicitly. Both feed the
    comparator.
    """
    parts: list[str] = []
    if wait_trace.input:
        parts.append(wait_trace.input)
    if wait_trace.wait_reason:
        parts.append(wait_trace.wait_reason)
    return _tokenize(" ".join(parts))


def evaluate_relevance(
    *,
    wait_trace: TraceLite,
    nearby_traces: Sequence[TraceLite],
    threshold: float = SIMILARITY_THRESHOLD,
) -> WaitFeedback:
    """Compute the §2.1 was_the_thing_still_relevant signal.

    Caller must ensure `nearby_traces` is the set of traces in
    `[until_iso - 24h, until_iso + 24h]` excluding the wait trace itself.
    Idempotency is the caller's responsibility — see `evaluate_completed_waits`.
    """
    if wait_trace.until_iso is None:
        raise ValueError("relevance signal only applies when until_iso is set")
    wait_tokens = _wait_tokens(wait_trace)
    matches = 0
    for t in nearby_traces:
        if t.trace_id == wait_trace.trace_id:
            continue
        candidate_tokens = _tokenize(t.input or "")
        if _jaccard(wait_tokens, candidate_tokens) >= threshold:
            matches += 1
    if matches > 0:
        return WaitFeedback(
            wait_trace_id=wait_trace.trace_id,
            signal_type=SIGNAL_RELEVANCE,
            signal_value=1.0,
            confidence=min(1.0, matches / 3.0),
        )
    return WaitFeedback(
        wait_trace_id=wait_trace.trace_id,
        signal_type=SIGNAL_RELEVANCE,
        signal_value=0.0,
        # Ambiguity: 0.0 could mean the wait was correct OR Jack missed
        # something. Confidence=0.5 records the uncertainty so the weekly
        # rollup can flag it.
        confidence=0.5,
    )


def evaluate_brought_up(
    *,
    wait_trace: TraceLite,
    later_traces: Sequence[TraceLite],
    threshold: float = SIMILARITY_THRESHOLD,
) -> WaitFeedback:
    """Compute the §2.2 did_jack_later_bring_it_up signal.

    `later_traces` are traces in `(wait_trace.created_at,
    wait_trace.created_at + 7d]` from non-scheduler triggers.
    """
    wait_tokens = _wait_tokens(wait_trace)
    for t in later_traces:
        if t.trace_id == wait_trace.trace_id:
            continue
        if t.trigger_source == "scheduler":
            continue
        candidate_tokens = _tokenize(t.input or "")
        if _jaccard(wait_tokens, candidate_tokens) >= threshold:
            return WaitFeedback(
                wait_trace_id=wait_trace.trace_id,
                signal_type=SIGNAL_BROUGHT_UP,
                signal_value=1.0,
                confidence=1.0,
            )
    return WaitFeedback(
        wait_trace_id=wait_trace.trace_id,
        signal_type=SIGNAL_BROUGHT_UP,
        signal_value=0.0,
        confidence=0.5,
    )


# ── Comparator orchestration ─────────────────────────────────────────────────


WaitTraceFetcher = Callable[[datetime, datetime], Awaitable[list[TraceLite]]]
"""Fetch waits whose `until_iso` falls in [start, end). Used by §2.1.
Caller wires this against the real trace store; tests inject stubs."""


NearbyTraceFetcher = Callable[[datetime, datetime], Awaitable[list[TraceLite]]]
"""Fetch all traces in [start, end). Used to build the relevance window
and the brought-up window."""


WaitsInWindowFetcher = Callable[[datetime, datetime], Awaitable[list[TraceLite]]]
"""Fetch waits in [start, end) regardless of until_iso. Used by §2.2."""


async def evaluate_completed_waits(
    *,
    now: datetime,
    fetch_completed_waits: WaitTraceFetcher,
    fetch_traces_in_window: NearbyTraceFetcher,
    feedback_repo: WaitFeedbackRepository,
    half_window: timedelta = RELEVANCE_HALF_WINDOW,
    relevance_lookback: timedelta = timedelta(hours=25),
) -> int:
    """§2.1 — for each completed-window wait, write a relevance signal
    iff one is not already recorded for that wait.

    Returns the number of feedback rows appended.
    """
    waits = await fetch_completed_waits(now - relevance_lookback, now)
    appended = 0
    for wait in waits:
        if wait.until_iso is None:
            # Defence in depth: the fetcher should have filtered, but
            # we double-check so the dataclass invariant cannot fail.
            continue
        existing = await feedback_repo.list_by_wait(
            wait.trace_id, signal_type=SIGNAL_RELEVANCE
        )
        if existing:
            continue
        nearby = await fetch_traces_in_window(
            wait.until_iso - half_window,
            wait.until_iso + half_window,
        )
        feedback = evaluate_relevance(
            wait_trace=wait, nearby_traces=nearby
        )
        await feedback_repo.append(feedback)
        appended += 1
    logger.info("wait_evaluator_relevance_pass", appended=appended)
    return appended


async def evaluate_brought_up_waits(
    *,
    now: datetime,
    fetch_recent_waits: WaitsInWindowFetcher,
    fetch_traces_in_window: NearbyTraceFetcher,
    feedback_repo: WaitFeedbackRepository,
    window: timedelta = BROUGHT_UP_WINDOW,
) -> int:
    """§2.2 — for each wait in the last 7 days that has no
    `did_jack_later_bring_it_up` signal yet, evaluate and write.
    """
    waits = await fetch_recent_waits(now - window, now)
    appended = 0
    for wait in waits:
        existing = await feedback_repo.list_by_wait(
            wait.trace_id, signal_type=SIGNAL_BROUGHT_UP
        )
        if existing:
            continue
        # The "later" window is from the wait's created_at forward, capped
        # at min(now, created_at + window). We don't pre-judge waits whose
        # window is still open; if the window has not yet closed AND no
        # match has appeared, we hold off. (Matches that appear later
        # will land on the next pass.)
        later_end = min(now, wait.created_at + window)
        if later_end <= wait.created_at:
            continue
        later = await fetch_traces_in_window(wait.created_at, later_end)
        feedback = evaluate_brought_up(
            wait_trace=wait, later_traces=later
        )
        # If no match was found AND the window is still open, do not
        # write a 0.0 yet — defer to the next pass to give later traces
        # a chance to land.
        if (
            feedback.signal_value == 0.0
            and (now - wait.created_at) < window
        ):
            continue
        await feedback_repo.append(feedback)
        appended += 1
    logger.info("wait_evaluator_brought_up_pass", appended=appended)
    return appended


# ── Weekly rollup summary helper ─────────────────────────────────────────────


@dataclass
class WaitFeedbackSummary:
    total: int
    by_signal_value_mean: dict[str, float]
    ambiguous_count: int  # records where signal_value=0.0 with high confidence
    sample_size_per_signal: dict[str, int]


def summarize_feedback(records: Iterable[WaitFeedback]) -> WaitFeedbackSummary:
    """Aggregate feedback records into the shape the weekly rollup
    consumes. Pure function — testable without a DB."""
    by_type_sum: dict[str, float] = {}
    by_type_count: dict[str, int] = {}
    ambiguous = 0
    total = 0
    for r in records:
        total += 1
        by_type_sum[r.signal_type] = by_type_sum.get(r.signal_type, 0.0) + r.signal_value
        by_type_count[r.signal_type] = by_type_count.get(r.signal_type, 0) + 1
        # "Ambiguous with high confidence" — operator-input thumbs at 0.0
        # with confidence 1.0 is the strongest signal of an incorrect
        # wait. Auto-signals with confidence 0.5 are NOT counted as
        # ambiguous-high; they're the natural shape of "no match in
        # window."
        if r.signal_value == 0.0 and r.confidence >= 0.8:
            ambiguous += 1
    means = {
        st: by_type_sum[st] / by_type_count[st] for st in by_type_count
    }
    return WaitFeedbackSummary(
        total=total,
        by_signal_value_mean=means,
        ambiguous_count=ambiguous,
        sample_size_per_signal=dict(by_type_count),
    )
