"""Persistence layer for `pattern_alerts` (#41).

Stores cluster summaries the pattern detector surfaces after each
nightly reflection. Each alert is a small piece of *operator-
reviewable* output: trace_ids that triggered the cluster, the size
and confidence, an optional LLM-or-detector-generated summary, and
the operator's chosen status.

Append-only at the application layer. The carve-out is a status
update — operators can mark an alert as `dismissed` or `filed`
(they took action on it). Update is the only post-insert mutation
the repository exposes; everything else is write-once.

The status update path mirrors the trace store's `set_user_reaction`
carve-out from ADR-0005 — narrow, named, no generic update method.
"""
from __future__ import annotations

import uuid as _uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import Float, Index, Integer, String, Text, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from agent.db import Base

logger = structlog.get_logger(__name__)

STATUS_OPEN: str = "open"
STATUS_DISMISSED: str = "dismissed"
STATUS_FILED: str = "filed"

_VALID_STATUSES: frozenset[str] = frozenset({STATUS_OPEN, STATUS_DISMISSED, STATUS_FILED})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_alert_id() -> str:
    return str(_uuid.uuid4())


# ── Dataclass contract ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class PatternAlert:
    """One persisted alert. Frozen — once constructed, only the
    `status` column may change via the repository's status carve-out."""

    trace_ids: list[str]
    cluster_size: int
    window_start: datetime
    window_end: datetime
    alert_id: str = field(default_factory=_new_alert_id)
    created_at: datetime = field(default_factory=_utcnow)
    confidence: float = 0.0
    summary: str = ""
    suggested_action: str = ""
    status: str = STATUS_OPEN
    metadata_: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(
                f"alert status must be one of {sorted(_VALID_STATUSES)}, "
                f"got {self.status!r}"
            )
        if self.cluster_size != len(self.trace_ids):
            raise ValueError(
                f"cluster_size {self.cluster_size} does not match "
                f"len(trace_ids) {len(self.trace_ids)}"
            )
        if self.cluster_size < 1:
            raise ValueError("cluster_size must be >= 1")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {self.confidence!r}"
            )
        if self.window_end < self.window_start:
            raise ValueError("window_end is before window_start")


# ── ORM mapping ──────────────────────────────────────────────────────────────


class PatternAlertRow(Base):
    """Storage projection for `PatternAlert`."""

    __tablename__ = "pattern_alerts"

    alert_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=_uuid.uuid4,
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False,
    )
    window_start: Mapped[datetime] = mapped_column(nullable=False)
    window_end: Mapped[datetime] = mapped_column(nullable=False)
    trace_ids: Mapped[list[_uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
    )
    cluster_size: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    suggested_action: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=STATUS_OPEN,
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
    )

    __table_args__ = (
        Index("idx_pattern_alerts_created_at", "created_at"),
        Index("idx_pattern_alerts_status", "status"),
        Index("idx_pattern_alerts_window", "window_start", "window_end"),
    )


# ── Mapping helpers ──────────────────────────────────────────────────────────


def _to_row(a: PatternAlert) -> PatternAlertRow:
    return PatternAlertRow(
        alert_id=_uuid.UUID(a.alert_id),
        created_at=a.created_at,
        window_start=a.window_start,
        window_end=a.window_end,
        trace_ids=[_uuid.UUID(t) for t in a.trace_ids],
        cluster_size=a.cluster_size,
        confidence=a.confidence,
        summary=a.summary,
        suggested_action=a.suggested_action,
        status=a.status,
        metadata_=dict(a.metadata_),
    )


def _from_row(row: PatternAlertRow) -> PatternAlert:
    return PatternAlert(
        alert_id=str(row.alert_id),
        created_at=row.created_at,
        window_start=row.window_start,
        window_end=row.window_end,
        trace_ids=[str(t) for t in row.trace_ids],
        cluster_size=row.cluster_size,
        confidence=row.confidence,
        summary=row.summary,
        suggested_action=row.suggested_action,
        status=row.status,
        metadata_=dict(row.metadata_ or {}),
    )


# ── Repository ───────────────────────────────────────────────────────────────


class PatternAlertRepository:
    """Append-only with one carve-out: `set_status` flips an alert
    between `open / dismissed / filed`. No generic `update_*`,
    no `delete_*`. The lint test asserts the public surface."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, alert: PatternAlert) -> PatternAlert:
        row = _to_row(alert)
        self._session.add(row)
        await self._session.flush()
        await self._session.commit()
        logger.info(
            "pattern_alert_appended",
            alert_id=alert.alert_id,
            cluster_size=alert.cluster_size,
        )
        return alert

    async def get_by_id(self, alert_id: str) -> Optional[PatternAlert]:
        stmt = select(PatternAlertRow).where(
            PatternAlertRow.alert_id == _uuid.UUID(alert_id),
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _from_row(row) if row is not None else None

    async def list_open(self, limit: int = 100) -> Sequence[PatternAlert]:
        """Return open alerts, newest first. UI default view."""
        return await self._list_with_status(STATUS_OPEN, limit=limit)

    async def list_by_status(
        self, status: str, limit: int = 100
    ) -> Sequence[PatternAlert]:
        if status not in _VALID_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        return await self._list_with_status(status, limit=limit)

    async def _list_with_status(
        self, status: str, *, limit: int
    ) -> Sequence[PatternAlert]:
        limit = max(1, min(limit, 1000))
        stmt = (
            select(PatternAlertRow)
            .where(PatternAlertRow.status == status)
            .order_by(PatternAlertRow.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        rows = result.scalars().all()
        return [_from_row(r) for r in rows]

    async def set_status(self, alert_id: str, status: str) -> bool:
        """Flip an alert's status. Returns True if an alert moved.

        The single named-mutation surface — there is no generic
        `update`. New statuses are added by extending `_VALID_STATUSES`
        + this method, not by exposing arbitrary writes.
        """
        if status not in _VALID_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        stmt = select(PatternAlertRow).where(
            PatternAlertRow.alert_id == _uuid.UUID(alert_id),
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return False
        row.status = status
        await self._session.commit()
        logger.info("pattern_alert_status_changed", alert_id=alert_id, status=status)
        return True
