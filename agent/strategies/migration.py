"""Idempotent post-`create_all` SQL for the `strategies` table.

`create_all` produces the table and the simple b-tree indexes declared
on `StrategyRow`. The HNSW index over the embedding column cannot be
expressed in SQLAlchemy and lands here. Same shape as
`agents/reflector/migration.py`.

Called from `agent.db.init_db()` after `Base.metadata.create_all`.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def apply_strategies_migration(conn: AsyncConnection) -> None:
    """Apply post-create_all DDL for the `strategies` table.

    Idempotent. Uses `IF NOT EXISTS` so re-running on every startup is
    a no-op after the first successful boot.
    """
    # Partial HNSW index on the embedding column. Strategies may briefly
    # land with NULL embedding (the bootstrap loader writes them text-
    # only and a follow-up backfill populates the column), and pgvector
    # rejects nulls inside an HNSW index. Same shape as the reflector's
    # partial index.
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_strategies_embedding
            ON strategies USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE embedding IS NOT NULL
            """
        )
    )
    # GIN over source_trace_ids — the trace inspector ("which strategies
    # cite this trace?") is a single GIN-indexed lookup.
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_strategies_source_trace_ids
            ON strategies USING gin (source_trace_ids)
            """
        )
    )
