"""Persistence + flow for proposed identity diffs (#52, ADR-0008).

The `pending_identity_diffs` table is the persistent surface for
reflector-proposed changes to the `## Identity` section of
`data/pepper_identity.md`. Each row is one proposed diff with a
status of `pending | approved | rejected`. Approval applies the
diff atomically (`agent.identity.apply_identity_diff`) and bumps
`identity_version`.

This is a sibling table to `strategies` (#53) — append-only at the
app layer, status-flippable, no in-place text edits.

Module surface:
- `IdentityDiff` — dataclass / ORM row.
- `IdentityDiffStatus` — closed enum of valid statuses.
- `IdentityDiffRepository` — append, list_pending, get, approve,
  reject. The approve helper is the only path that calls
  `apply_identity_diff`.
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
from agent.identity import (
    DEFAULT_IDENTITY_PATH,
    Identity,
    apply_identity_diff,
)

logger = structlog.get_logger(__name__)


class IdentityDiffStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

    ALL: frozenset[str] = frozenset({PENDING, APPROVED, REJECTED})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_diff_id() -> _uuid.UUID:
    return _uuid.uuid4()


@dataclass
class IdentityDiff:
    """One proposed diff to the `## Identity` section.

    `proposed_text` is the full new section body; the diff applies as a
    full-section replace on approval. We do NOT store unified-diff
    fragments — Pepper's identity is small enough that a full replace
    is simpler and avoids reconciliation hazards.
    """

    proposed_text: str
    rationale: str = ""
    source_trace_ids: list[_uuid.UUID] = field(default_factory=list)
    status: str = IdentityDiffStatus.PENDING
    diff_id: _uuid.UUID = field(default_factory=_new_diff_id)
    created_at: datetime = field(default_factory=_utcnow)
    decided_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.proposed_text or not self.proposed_text.strip():
            raise ValueError("identity diff proposed_text cannot be empty")
        if self.status not in IdentityDiffStatus.ALL:
            raise ValueError(
                f"identity diff status must be one of "
                f"{sorted(IdentityDiffStatus.ALL)}, got {self.status!r}"
            )


class IdentityDiffRow(Base):
    __tablename__ = "pending_identity_diffs"

    diff_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=_uuid.uuid4,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    proposed_text: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_trace_ids: Mapped[list[_uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=IdentityDiffStatus.PENDING
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("idx_pending_identity_diffs_status", "status"),
        Index("idx_pending_identity_diffs_created_at", "created_at"),
    )


def _row_to_dataclass(row: IdentityDiffRow) -> IdentityDiff:
    return IdentityDiff(
        proposed_text=row.proposed_text,
        rationale=row.rationale,
        source_trace_ids=list(row.source_trace_ids or []),
        status=row.status,
        diff_id=row.diff_id,
        created_at=row.created_at,
        decided_at=row.decided_at,
    )


class IdentityDiffRepository:
    """Read/write surface for `pending_identity_diffs`.

    Public methods: append, list_pending, get, reject, approve. There
    is no `update_text` and no `delete` — once a diff is recorded, the
    audit trail is permanent.

    The approve helper applies the diff via
    `agent.identity.apply_identity_diff`. Identity-write atomicity
    rides on the file-write primitive there.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, diff: IdentityDiff) -> IdentityDiff:
        if diff.status != IdentityDiffStatus.PENDING:
            raise ValueError(
                "append() only accepts PENDING diffs; "
                f"got {diff.status!r}"
            )
        row = IdentityDiffRow(
            diff_id=diff.diff_id,
            created_at=diff.created_at,
            proposed_text=diff.proposed_text,
            rationale=diff.rationale,
            source_trace_ids=list(diff.source_trace_ids or []),
            status=diff.status,
            decided_at=diff.decided_at,
        )
        self._session.add(row)
        await self._session.flush()
        return diff

    async def list_pending(self, *, limit: int = 50) -> list[IdentityDiff]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        stmt = (
            select(IdentityDiffRow)
            .where(IdentityDiffRow.status == IdentityDiffStatus.PENDING)
            .order_by(IdentityDiffRow.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [_row_to_dataclass(r) for r in result.scalars().all()]

    async def get(self, diff_id) -> Optional[IdentityDiff]:
        sid = diff_id if isinstance(diff_id, _uuid.UUID) else _uuid.UUID(str(diff_id))
        result = await self._session.execute(
            select(IdentityDiffRow).where(IdentityDiffRow.diff_id == sid)
        )
        row = result.scalar_one_or_none()
        return _row_to_dataclass(row) if row is not None else None

    async def reject(self, diff_id) -> None:
        sid = diff_id if isinstance(diff_id, _uuid.UUID) else _uuid.UUID(str(diff_id))
        await self._session.execute(
            update(IdentityDiffRow)
            .where(
                IdentityDiffRow.diff_id == sid,
                IdentityDiffRow.status == IdentityDiffStatus.PENDING,
            )
            .values(status=IdentityDiffStatus.REJECTED, decided_at=_utcnow())
        )
        logger.info("identity_diff_rejected", diff_id=str(sid))

    async def approve(
        self,
        diff_id,
        *,
        identity_path: str = DEFAULT_IDENTITY_PATH,
        on_applied=None,
    ) -> Identity:
        """Mark the diff approved AND apply it to the file.

        The status flip and the file write happen in this order
        deliberately:
        1. Flip status to APPROVED inside the current session (not yet
           committed by the caller).
        2. Apply the file write atomically (os.replace).
        3. (Optional) call `on_applied(new_identity)` so the caller can
           invalidate any in-process cache (e.g.
           ``ContextAssembler.refresh_identity()``) so the next turn
           picks up the new content without a process restart.
        4. Caller commits the session.

        If the file write fails, the SQLAlchemy session has not been
        committed by the caller yet — they roll back and the row stays
        PENDING. If the commit fails after a successful file write, the
        operator sees an apparent "diff applied but still pending"
        which is recoverable: re-approving is a no-op (file is already
        the new content), and the next status flip succeeds.
        """
        diff = await self.get(diff_id)
        if diff is None:
            raise LookupError(f"identity diff {diff_id} does not exist")
        if diff.status != IdentityDiffStatus.PENDING:
            raise ValueError(
                f"identity diff {diff.diff_id} is in status "
                f"{diff.status!r}, expected pending"
            )
        await self._session.execute(
            update(IdentityDiffRow)
            .where(IdentityDiffRow.diff_id == diff.diff_id)
            .values(status=IdentityDiffStatus.APPROVED, decided_at=_utcnow())
        )
        new_identity = apply_identity_diff(
            proposed_identity_text=diff.proposed_text,
            path=identity_path,
        )
        if on_applied is not None:
            try:
                on_applied(new_identity)
            except Exception:
                # Cache-invalidation failure must not unwind a successful
                # approval — log and let the operator restart the process
                # if needed.
                logger.warning(
                    "identity_diff_on_applied_failed",
                    diff_id=str(diff.diff_id),
                )
        logger.info(
            "identity_diff_approved",
            diff_id=str(diff.diff_id),
            new_identity_version=new_identity.identity_version,
        )
        return new_identity
