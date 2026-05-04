"""SQLAlchemy ORM model for the ``strategies`` table.

Append-only at the application layer (enforced by
:class:`agent.strategies.repository.StrategyRepository`).
Version chains are modelled via ``parent_strategy_id``:
superseding a strategy creates a new row with
``parent_strategy_id`` pointing to the old one and
``status='superseded'`` set on the old row.
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, deferred, mapped_column

from agent.db import Base

# Must match agent/strategies/__init__.py STRATEGY_EMBEDDING_DIM
# Defined here (not imported) to avoid circular imports at model load time.
_STRATEGY_EMBEDDING_DIM: int = 768


class StrategyRow(Base):
    """One strategy version.  Read the column comments for semantics."""

    __tablename__ = "strategies"

    # ── Identity ──────────────────────────────────────────────────────
    strategy_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=_uuid.uuid4,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # ── Content ───────────────────────────────────────────────────────
    text: Mapped[str] = mapped_column(Text, nullable=False)

    # ── Versioning ────────────────────────────────────────────────────
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_strategy_id: Mapped[Optional[_uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )

    # ── Provenance ────────────────────────────────────────────────────
    # created_by: 'jack' | 'reflector' | 'bootstrap'
    created_by: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="jack",
    )
    # Which traces informed this strategy (may be empty).
    source_trace_ids: Mapped[Optional[list[_uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=True,
    )

    # ── Quality signals ───────────────────────────────────────────────
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_confirmed_correct: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # ── Lifecycle ─────────────────────────────────────────────────────
    # status: 'active' | 'superseded' | 'flagged'
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
    )

    # ── Embedding — deferred; 768-dim vector is ~3 KB per row ─────────
    embedding: Mapped[Optional[list[float]]] = deferred(
        mapped_column(Vector(_STRATEGY_EMBEDDING_DIM), nullable=True),
    )

    # ── Indexes ───────────────────────────────────────────────────────
    __table_args__ = (
        Index("idx_strategies_status", "status"),
        Index("idx_strategies_created_at", "created_at"),
    )
