"""Persistence + flow for proposed strategy updates (#54).

Sibling to `agent/identity_diffs.py`. The `pending_strategy_diffs`
table holds reflector- (or model-) proposed updates to the strategy
hub. Each row is one proposed update with status `pending | approved
| rejected`. Approval applies the diff via `StrategyRepository`:

- `strategy_id` is null → new strategy lineage (`append`).
- `strategy_id` is set → new version of an existing strategy
  (`append_version`).

This module only exposes append + status flips; the actual repository
write is performed by `approve()` against an injected
`StrategyRepository`.
"""
from __future__ import annotations

import uuid as _uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import DateTime, Index, String, Text, select, update
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from agent.db import Base
from agent.strategies.repository import (
    Strategy,
    StrategyCreatedBy,
    StrategyRepository,
)

logger = structlog.get_logger(__name__)


class StrategyDiffStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

    ALL: frozenset[str] = frozenset({PENDING, APPROVED, REJECTED})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_diff_id() -> _uuid.UUID:
    return _uuid.uuid4()


@dataclass
class StrategyDiff:
    """One proposed strategy update.

    `target_strategy_id` is None for a brand-new strategy lineage; set
    to the parent's `strategy_id` for a new version of an existing one.
    """

    proposed_text: str
    rationale: str = ""
    target_strategy_id: Optional[_uuid.UUID] = None
    proposed_by: str = StrategyCreatedBy.REFLECTOR
    source_trace_ids: list[_uuid.UUID] = field(default_factory=list)
    status: str = StrategyDiffStatus.PENDING
    diff_id: _uuid.UUID = field(default_factory=_new_diff_id)
    created_at: datetime = field(default_factory=_utcnow)
    decided_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.proposed_text or not self.proposed_text.strip():
            raise ValueError("strategy diff proposed_text cannot be empty")
        if self.status not in StrategyDiffStatus.ALL:
            raise ValueError(
                f"strategy diff status must be one of "
                f"{sorted(StrategyDiffStatus.ALL)}, got {self.status!r}"
            )
        if self.proposed_by not in StrategyCreatedBy.ALL:
            raise ValueError(
                f"proposed_by must be one of {sorted(StrategyCreatedBy.ALL)}, "
                f"got {self.proposed_by!r}"
            )


class StrategyDiffRow(Base):
    __tablename__ = "pending_strategy_diffs"

    diff_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=_uuid.uuid4,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    target_strategy_id: Mapped[Optional[_uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    proposed_text: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    proposed_by: Mapped[str] = mapped_column(
        String(32), nullable=False, default=StrategyCreatedBy.REFLECTOR
    )
    source_trace_ids: Mapped[list[_uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=StrategyDiffStatus.PENDING
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("idx_pending_strategy_diffs_status", "status"),
        Index("idx_pending_strategy_diffs_created_at", "created_at"),
        Index("idx_pending_strategy_diffs_target", "target_strategy_id"),
    )


def _row_to_dataclass(row: StrategyDiffRow) -> StrategyDiff:
    return StrategyDiff(
        proposed_text=row.proposed_text,
        rationale=row.rationale,
        target_strategy_id=row.target_strategy_id,
        proposed_by=row.proposed_by,
        source_trace_ids=list(row.source_trace_ids or []),
        status=row.status,
        diff_id=row.diff_id,
        created_at=row.created_at,
        decided_at=row.decided_at,
    )


class StrategyDiffRepository:
    """Read/write surface for `pending_strategy_diffs`.

    Public methods: append, list_pending, get, reject, approve. There
    is no `update_text` and no `delete` — once a diff is recorded, the
    audit trail is permanent.

    `approve()` calls into `StrategyRepository.append` (for new
    lineages, target_strategy_id=None) or `StrategyRepository.append_version`
    (for new versions of existing strategies). Identity-diffs land
    file changes; strategy-diffs land DB rows. Otherwise the shape
    matches.
    """

    def __init__(
        self,
        session: AsyncSession,
        strategies_repo: StrategyRepository,
    ) -> None:
        self._session = session
        self._strategies = strategies_repo

    async def append(self, diff: StrategyDiff) -> StrategyDiff:
        if diff.status != StrategyDiffStatus.PENDING:
            raise ValueError(
                "append() only accepts PENDING diffs; "
                f"got {diff.status!r}"
            )
        row = StrategyDiffRow(
            diff_id=diff.diff_id,
            created_at=diff.created_at,
            target_strategy_id=diff.target_strategy_id,
            proposed_text=diff.proposed_text,
            rationale=diff.rationale,
            proposed_by=diff.proposed_by,
            source_trace_ids=list(diff.source_trace_ids or []),
            status=diff.status,
            decided_at=diff.decided_at,
        )
        self._session.add(row)
        await self._session.flush()
        return diff

    async def list_pending(self, *, limit: int = 50) -> list[StrategyDiff]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        stmt = (
            select(StrategyDiffRow)
            .where(StrategyDiffRow.status == StrategyDiffStatus.PENDING)
            .order_by(StrategyDiffRow.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_row_to_dataclass(r) for r in result.scalars().all()]

    async def get(self, diff_id) -> Optional[StrategyDiff]:
        sid = diff_id if isinstance(diff_id, _uuid.UUID) else _uuid.UUID(str(diff_id))
        result = await self._session.execute(
            select(StrategyDiffRow).where(StrategyDiffRow.diff_id == sid)
        )
        row = result.scalar_one_or_none()
        return _row_to_dataclass(row) if row is not None else None

    async def reject(self, diff_id) -> None:
        sid = diff_id if isinstance(diff_id, _uuid.UUID) else _uuid.UUID(str(diff_id))
        await self._session.execute(
            update(StrategyDiffRow)
            .where(
                StrategyDiffRow.diff_id == sid,
                StrategyDiffRow.status == StrategyDiffStatus.PENDING,
            )
            .values(status=StrategyDiffStatus.REJECTED, decided_at=_utcnow())
        )
        logger.info("strategy_diff_rejected", diff_id=str(sid))

    async def approve(self, diff_id) -> Strategy:
        diff = await self.get(diff_id)
        if diff is None:
            raise LookupError(f"strategy diff {diff_id} does not exist")
        if diff.status != StrategyDiffStatus.PENDING:
            raise ValueError(
                f"strategy diff {diff.diff_id} is in status "
                f"{diff.status!r}, expected pending"
            )
        await self._session.execute(
            update(StrategyDiffRow)
            .where(StrategyDiffRow.diff_id == diff.diff_id)
            .values(status=StrategyDiffStatus.APPROVED, decided_at=_utcnow())
        )
        if diff.target_strategy_id is None:
            new_strategy = await self._strategies.append(
                Strategy(
                    text=diff.proposed_text,
                    created_by=diff.proposed_by,
                    source_trace_ids=list(diff.source_trace_ids),
                )
            )
        else:
            parent = await self._strategies.get(diff.target_strategy_id)
            if parent is None:
                raise LookupError(
                    f"target strategy {diff.target_strategy_id} not found"
                )
            new_strategy = await self._strategies.append_version(
                parent=parent,
                new_text=diff.proposed_text,
                created_by=diff.proposed_by,
                source_trace_ids=list(diff.source_trace_ids),
            )
        logger.info(
            "strategy_diff_approved",
            diff_id=str(diff.diff_id),
            new_strategy_id=str(new_strategy.strategy_id),
            new_version=new_strategy.version,
        )
        return new_strategy
