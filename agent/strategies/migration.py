"""Idempotent post-``create_all`` SQL for the ``strategies`` table.

Three things land here that SQLAlchemy's ``Base.metadata.create_all``
cannot express:

1. Specialised indexes — HNSW on ``embedding``, GIN on
   ``source_trace_ids``.
2. ``source_trace_ids`` column upgrade guard — older deployments may
   pre-date the column; the idempotent ADD COLUMN handles them.

``apply_strategies_migration(conn)`` is called from ``agent.db.init_db``
after the traces migration and is safe to re-run on every startup.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def _exec(conn: AsyncConnection, sql: str) -> None:
    await conn.execute(text(sql))


async def apply_strategies_migration(conn: AsyncConnection) -> None:
    """Apply strategies-specific schema additions and indexes.

    Idempotent. Safe to call on every ``init_db`` invocation.
    Does not raise on a fresh DB or on a re-run.
    """
    # ── HNSW index on embedding ──────────────────────────────────────
    # Partial index: pgvector rejects null values in HNSW indexes.
    await _exec(
        conn,
        """
        CREATE INDEX IF NOT EXISTS idx_strategies_embedding
        ON strategies USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        WHERE embedding IS NOT NULL
        """,
    )

    # ── GIN index on source_trace_ids (uuid array) ───────────────────
    # Supports the ``@>`` array-containment operator for "which strategies
    # were informed by trace X" queries.
    await _exec(
        conn,
        """
        CREATE INDEX IF NOT EXISTS idx_strategies_source_trace_ids
        ON strategies USING gin (source_trace_ids)
        WHERE source_trace_ids IS NOT NULL
        """,
    )
