"""SQLAlchemy ORM mapping for the `traces` table.

Materializes the contract in `agent/traces/schema.py` (the canonical
dataclass) and `docs/trace-schema.md`. Heavy columns (`embedding`,
`assembled_context`, `tools_called`) are `deferred()` so the default
load excludes them, per the Read patterns mandate.

Append-only at the application layer is enforced by the repository
(`agent/traces/repository.py`) — this module only describes the row
shape. Database-layer enforcement (per-column UPDATE grants on
`pepper_traces_compactor`, INSERT-only on `pepper_traces_writer`) is
applied by `agent/traces/migration.py` and called from `init_db`.
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime
from typing import Any, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, deferred, mapped_column

from agent.db import Base
from agent.traces.schema import EMBEDDING_DIM


class TraceRow(Base):
    """One persisted agent turn. See `agent/traces/schema.py::Trace` for
    the field semantics — this class is the storage projection.
    """

    __tablename__ = "traces"

    # Identity & timing — always loaded.
    trace_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=_uuid.uuid4,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Provenance — always loaded.
    trigger_source: Mapped[str] = mapped_column(String(20), nullable=False)
    archetype: Mapped[str] = mapped_column(String(20), nullable=False)
    scheduler_job_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Conversation payload (RAW_PERSONAL).
    # `input` and `output` are loaded by default — list view callers must
    # explicitly project the columns they want (see Read patterns in
    # docs/trace-schema.md). They are NOT deferred because the existing
    # callers in this codebase load full rows, and the SQLAlchemy default
    # load is the safer choice for correctness; #24's list-view query
    # explicitly projects to avoid them.
    input: Mapped[str] = mapped_column(Text, nullable=False, default="")
    output: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Heavy jsonb columns — deferred() to keep default loads bounded.
    assembled_context: Mapped[dict[str, Any]] = deferred(
        mapped_column(JSONB, nullable=False, default=dict),
    )
    tools_called: Mapped[list[dict[str, Any]]] = deferred(
        mapped_column(JSONB, nullable=False, default=list),
    )

    # Model & prompt — always loaded.
    model_selected: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model_version: Mapped[str] = mapped_column(Text, nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="unversioned",
    )

    # Outcome — always loaded.
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    user_reaction: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
    )
    data_sensitivity: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="local_only",
    )

    # Embedding — deferred(); a 1024-dim vector at ~4 KB per row is the
    # single largest column and most consumers don't need it.
    embedding: Mapped[Optional[list[float]]] = deferred(
        mapped_column(Vector(EMBEDDING_DIM), nullable=True),
    )
    embedding_model_version: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    # Compression tier.
    tier: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="working",
    )

    # Composite indexes that don't fit on a column-level `index=True`.
    __table_args__ = (
        Index("idx_traces_created_at", "created_at"),
        Index("idx_traces_archetype_created_at", "archetype", "created_at"),
        Index("idx_traces_model_created_at", "model_selected", "created_at"),
    )
