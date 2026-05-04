"""Persistence layer for the `strategies` table.

Append-only at the app layer. A strategy is never edited in place; a
new version is appended with `parent_strategy_id` pointing at the row
it supersedes, and the old row's `status` is flipped to `superseded`.
This mirrors the `reflections` and `traces` discipline.

Schema (per #53):

| Column                     | Type                | Notes                                                        |
| -------------------------- | ------------------- | ------------------------------------------------------------ |
| `strategy_id`              | uuid (pk)           | New uuid per row, including new versions                     |
| `text`                     | text                | The strategy in natural language. Indexed via embedding      |
| `version`                  | integer             | Monotonic per lineage; v1 for new lineages                   |
| `parent_strategy_id`       | uuid (nullable)     | Points at the row this version supersedes                    |
| `created_at`               | timestamptz         | Insertion time                                               |
| `created_by`               | text (enum)         | `jack` | `reflector` | `bootstrap`                            |
| `source_trace_ids`         | uuid[]              | Traces that informed the strategy; may be empty for bootstrap |
| `confidence`               | real (0..1)         | Heuristic v0; revised once we see real strategies            |
| `usage_count`              | integer             | Bumped by #54's query path                                    |
| `last_confirmed_correct`   | timestamptz nullable | Bumped by #56's wait-feedback / explicit thumbs              |
| `status`                   | text (enum)         | `active` | `superseded` | `flagged`                          |
| `embedding`                | vector(1024)        | qwen3-embedding:0.6b — populated by #54's query path         |

Confidence v0 (documented):
    confidence = clamp01(0.5 + 0.05 * usage_count + recency_bonus)
where recency_bonus is +0.2 if `last_confirmed_correct` is within 30 days,
+0.1 within 90 days, 0 otherwise.

Conflict resolution:
    On insert, the repository's `append_with_contradiction_check` helper
    is offered a callback (`is_contradicting`) that the caller wires to
    an LLM-judge in #54. If the callback flags an existing active
    strategy as contradicted by the new one, the existing row's status
    is flipped to `flagged` (NOT `superseded` — that requires explicit
    operator approval). The new row is inserted with status=`active`.
    A pending-actions notification is the surface; this module emits a
    structured log line `strategy_contradiction_flagged` with both ids.
"""
from __future__ import annotations

import uuid as _uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

import structlog
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, deferred, mapped_column

from agent.db import Base

logger = structlog.get_logger(__name__)

# Mirrors `agent.traces.schema.EMBEDDING_DIM` and reflector's storage.
# Strategies are surfaced into the system prompt by #54 via similarity
# search, so they need to share the embedding space with router/recall.
STRATEGY_EMBEDDING_DIM: int = 1024
STRATEGY_EMBEDDING_MODEL_DEFAULT: str = "qwen3-embedding:0.6b"


class StrategyCreatedBy:
    """Closed enum of valid `created_by` values.

    Stored as text for forward-compat (no painful enum migration if a
    new origin is added) but constrained at the Python layer.
    """

    JACK = "jack"
    REFLECTOR = "reflector"
    BOOTSTRAP = "bootstrap"

    ALL: frozenset[str] = frozenset({JACK, REFLECTOR, BOOTSTRAP})


class StrategyStatus:
    """Closed enum of valid `status` values."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FLAGGED = "flagged"

    ALL: frozenset[str] = frozenset({ACTIVE, SUPERSEDED, FLAGGED})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_strategy_id() -> _uuid.UUID:
    return _uuid.uuid4()


# ── Dataclass contract ───────────────────────────────────────────────────────


@dataclass
class Strategy:
    """One persisted strategy.

    Mutability of the dataclass mirrors the mutable column set the
    repository exposes write paths for (`usage_count`,
    `last_confirmed_correct`, `status`). The `text`, `version`,
    `parent_strategy_id`, and `created_*` fields are write-once.
    """

    text: str
    created_by: str = StrategyCreatedBy.JACK
    version: int = 1
    parent_strategy_id: Optional[_uuid.UUID] = None
    source_trace_ids: list[_uuid.UUID] = field(default_factory=list)
    confidence: float = 0.5
    usage_count: int = 0
    last_confirmed_correct: Optional[datetime] = None
    status: str = StrategyStatus.ACTIVE
    strategy_id: _uuid.UUID = field(default_factory=_new_strategy_id)
    created_at: datetime = field(default_factory=_utcnow)
    embedding: Optional[list[float]] = None

    def __post_init__(self) -> None:
        if not self.text or not self.text.strip():
            raise ValueError("strategy text cannot be empty")
        if self.created_by not in StrategyCreatedBy.ALL:
            raise ValueError(
                f"strategy created_by must be one of {sorted(StrategyCreatedBy.ALL)}, "
                f"got {self.created_by!r}"
            )
        if self.status not in StrategyStatus.ALL:
            raise ValueError(
                f"strategy status must be one of {sorted(StrategyStatus.ALL)}, "
                f"got {self.status!r}"
            )
        if self.version < 1:
            raise ValueError("strategy version must be >= 1")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"strategy confidence must be in [0.0, 1.0], got {self.confidence!r}"
            )
        if self.embedding is not None and len(self.embedding) != STRATEGY_EMBEDDING_DIM:
            raise ValueError(
                f"strategy embedding must have dim {STRATEGY_EMBEDDING_DIM}, "
                f"got {len(self.embedding)}"
            )


# ── ORM mapping ──────────────────────────────────────────────────────────────


class StrategyRow(Base):
    """Storage projection for `Strategy`. Append-only at the app layer."""

    __tablename__ = "strategies"

    strategy_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=_uuid.uuid4,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_strategy_id: Mapped[Optional[_uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    source_trace_ids: Mapped[list[_uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
        default=list,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_confirmed_correct: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=StrategyStatus.ACTIVE,
    )

    embedding: Mapped[Optional[list[float]]] = deferred(
        mapped_column(Vector(STRATEGY_EMBEDDING_DIM), nullable=True),
    )

    __table_args__ = (
        Index("idx_strategies_status", "status"),
        Index("idx_strategies_created_at", "created_at"),
        Index("idx_strategies_parent", "parent_strategy_id"),
    )


# ── Repository ───────────────────────────────────────────────────────────────


def _row_to_dataclass(row: StrategyRow) -> Strategy:
    return Strategy(
        text=row.text,
        version=row.version,
        parent_strategy_id=row.parent_strategy_id,
        source_trace_ids=list(row.source_trace_ids or []),
        confidence=row.confidence,
        usage_count=row.usage_count,
        last_confirmed_correct=row.last_confirmed_correct,
        status=row.status,
        strategy_id=row.strategy_id,
        created_at=row.created_at,
        created_by=row.created_by,
        embedding=None,  # deferred — callers ask explicitly via `with_embeddings`
    )


def _coerce_uuid(value) -> _uuid.UUID:
    if isinstance(value, _uuid.UUID):
        return value
    return _uuid.UUID(str(value))


# Optional callback the caller wires to an LLM-judge (lands in #54). If
# the callback returns True for `(existing_text, new_text)`, the
# existing row is `flagged`. If None, no contradiction check is run.
ContradictionJudge = Callable[[str, str], Awaitable[bool]]


def compute_confidence_v0(
    *, usage_count: int, last_confirmed_correct: Optional[datetime], now: Optional[datetime] = None
) -> float:
    """Heuristic v0 confidence score.

    Documented in module docstring. Pure function so it's trivially
    testable and the eval flywheel can swap it without DB churn.
    """
    if now is None:
        now = _utcnow()
    base = 0.5 + 0.05 * max(0, usage_count)
    recency_bonus = 0.0
    if last_confirmed_correct is not None:
        delta = now - last_confirmed_correct
        if delta < timedelta(days=30):
            recency_bonus = 0.2
        elif delta < timedelta(days=90):
            recency_bonus = 0.1
    return max(0.0, min(1.0, base + recency_bonus))


class StrategyRepository:
    """Read/write surface for the `strategies` table.

    The repository exposes only:
      - `append`: insert a new strategy (new lineage, version=1)
      - `append_version`: insert a new version that supersedes an old one
      - `query_active`: list active strategies
      - `get`: fetch by id
      - `bump_usage`: increment usage_count + recompute confidence
      - `confirm_correct`: stamp last_confirmed_correct=now + recompute confidence
      - `set_status`: flip status (used for `flagged` + `superseded`)
      - `count_active`: cheap "is the table empty?" check for bootstrap

    There is no `update_text`, no `delete`. Editing means appending a
    new version; revoking means flipping status to `superseded` via
    an explicit `set_status` call. This is enforced by *not exposing*
    those methods — there is no DELETE / UPDATE TEXT path through this
    module.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        strategy: Strategy,
        *,
        is_contradicting: Optional[ContradictionJudge] = None,
    ) -> Strategy:
        """Insert a new lineage (version 1).

        If `is_contradicting` is provided, every currently-active
        strategy is checked against the new one. The first existing
        strategy that the judge flags as contradicted is set to
        `flagged`. The new strategy is still inserted as `active`.
        """
        if strategy.version != 1:
            raise ValueError(
                "append() inserts a new lineage; use append_version() for "
                f"version > 1 (got {strategy.version})"
            )
        if strategy.parent_strategy_id is not None:
            raise ValueError(
                "append() inserts a new lineage; parent_strategy_id must be None"
            )
        await self._maybe_flag_contradictions(strategy, is_contradicting)
        return await self._insert(strategy)

    async def append_version(
        self,
        *,
        parent: Strategy,
        new_text: str,
        created_by: str,
        source_trace_ids: Optional[Sequence[_uuid.UUID]] = None,
        embedding: Optional[list[float]] = None,
    ) -> Strategy:
        """Append a new version that supersedes `parent`.

        Atomic at the SQLAlchemy session level: the parent's status flips
        to `superseded` and the new row is inserted in the same flush.
        """
        if parent.status == StrategyStatus.SUPERSEDED:
            raise ValueError(
                f"strategy {parent.strategy_id} is already superseded; "
                f"cannot append a new version on top of a dead lineage"
            )
        new_row = Strategy(
            text=new_text,
            version=parent.version + 1,
            parent_strategy_id=parent.strategy_id,
            source_trace_ids=list(source_trace_ids or []),
            created_by=created_by,
            embedding=embedding,
            confidence=compute_confidence_v0(
                usage_count=0, last_confirmed_correct=None
            ),
        )
        await self._session.execute(
            update(StrategyRow)
            .where(StrategyRow.strategy_id == parent.strategy_id)
            .values(status=StrategyStatus.SUPERSEDED)
        )
        return await self._insert(new_row)

    async def query_active(self, *, limit: int = 100) -> list[Strategy]:
        """List active strategies, newest first."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        stmt = (
            select(StrategyRow)
            .where(StrategyRow.status == StrategyStatus.ACTIVE)
            .order_by(StrategyRow.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_row_to_dataclass(row) for row in result.scalars().all()]

    async def get(self, strategy_id) -> Optional[Strategy]:
        sid = _coerce_uuid(strategy_id)
        result = await self._session.execute(
            select(StrategyRow).where(StrategyRow.strategy_id == sid)
        )
        row = result.scalar_one_or_none()
        return _row_to_dataclass(row) if row is not None else None

    async def bump_usage(self, strategy_id) -> None:
        """Increment usage_count and recompute confidence_v0."""
        sid = _coerce_uuid(strategy_id)
        result = await self._session.execute(
            select(StrategyRow).where(StrategyRow.strategy_id == sid)
        )
        row = result.scalar_one_or_none()
        if row is None:
            logger.warning("strategy_bump_usage_missing", strategy_id=str(sid))
            return
        row.usage_count = (row.usage_count or 0) + 1
        row.confidence = compute_confidence_v0(
            usage_count=row.usage_count,
            last_confirmed_correct=row.last_confirmed_correct,
        )

    async def confirm_correct(self, strategy_id, *, at: Optional[datetime] = None) -> None:
        """Stamp `last_confirmed_correct` and recompute confidence_v0."""
        sid = _coerce_uuid(strategy_id)
        when = at or _utcnow()
        result = await self._session.execute(
            select(StrategyRow).where(StrategyRow.strategy_id == sid)
        )
        row = result.scalar_one_or_none()
        if row is None:
            logger.warning("strategy_confirm_correct_missing", strategy_id=str(sid))
            return
        row.last_confirmed_correct = when
        row.confidence = compute_confidence_v0(
            usage_count=row.usage_count,
            last_confirmed_correct=row.last_confirmed_correct,
            now=when,
        )

    async def set_status(self, strategy_id, status: str) -> None:
        if status not in StrategyStatus.ALL:
            raise ValueError(
                f"status must be one of {sorted(StrategyStatus.ALL)}, got {status!r}"
            )
        sid = _coerce_uuid(strategy_id)
        await self._session.execute(
            update(StrategyRow)
            .where(StrategyRow.strategy_id == sid)
            .values(status=status)
        )

    async def count_active(self) -> int:
        """Cheap empty-check for the bootstrap loader."""
        result = await self._session.execute(
            select(StrategyRow.strategy_id).where(
                StrategyRow.status == StrategyStatus.ACTIVE
            )
        )
        return len(result.scalars().all())

    # ── internals ────────────────────────────────────────────────────────────

    async def _insert(self, strategy: Strategy) -> Strategy:
        row = StrategyRow(
            strategy_id=strategy.strategy_id,
            text=strategy.text,
            version=strategy.version,
            parent_strategy_id=strategy.parent_strategy_id,
            created_at=strategy.created_at,
            created_by=strategy.created_by,
            source_trace_ids=list(strategy.source_trace_ids or []),
            confidence=strategy.confidence,
            usage_count=strategy.usage_count,
            last_confirmed_correct=strategy.last_confirmed_correct,
            status=strategy.status,
            embedding=strategy.embedding,
        )
        self._session.add(row)
        await self._session.flush()
        return strategy

    async def _maybe_flag_contradictions(
        self,
        new_strategy: Strategy,
        is_contradicting: Optional[ContradictionJudge],
    ) -> None:
        if is_contradicting is None:
            return
        # Only check active strategies. Contradicting a superseded
        # strategy is meaningless — it's already off the active list.
        active = await self.query_active(limit=1000)
        for existing in active:
            try:
                contradicts = await is_contradicting(existing.text, new_strategy.text)
            except Exception:
                # Judge failures must not block strategy writes —
                # log and continue. The reflector's productive load
                # outweighs perfect contradiction-detection.
                logger.warning(
                    "strategy_contradiction_judge_error",
                    existing_id=str(existing.strategy_id),
                )
                continue
            if contradicts:
                await self.set_status(existing.strategy_id, StrategyStatus.FLAGGED)
                logger.info(
                    "strategy_contradiction_flagged",
                    existing_id=str(existing.strategy_id),
                    new_id=str(new_strategy.strategy_id),
                )
                # Flag the FIRST match only. A new strategy that
                # contradicts multiple existing ones is a signal Jack
                # should review, not an excuse to mass-flag.
                return
