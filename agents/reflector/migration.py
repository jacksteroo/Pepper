"""Idempotent post-`create_all` SQL for the reflector's tables.

Mirrors the shape of `agent/traces/migration.py`: SQLAlchemy's
`create_all` cannot express partial HNSW indexes or a GIN index on a
uuid[] column, so they are applied here. Safe to re-run on every
startup.

This module is imported by `agents.reflector.main` and applied once
on reflector boot. It does NOT touch the `traces` table — that
remains the trace store's responsibility. The reflector's grants are
left to a follow-up (the operator-level note in the #38 PR also
applies here): until per-archetype Postgres roles land, the reflector
process connects with the same credentials as Pepper Core.

Tables covered:
- `reflections` (#39 / #40) — partial HNSW on embedding, GIN on
  parent_reflection_ids.
- `pattern_alerts` (#41) — GIN on trace_ids[] so the trace-inspector
  reverse lookup ("which alerts mention this trace?") is one
  indexed query.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def apply_reflections_migration(conn: AsyncConnection) -> None:
    """Apply post-create_all DDL for the reflector's own tables.

    Idempotent. Each statement uses `IF NOT EXISTS` so re-running on
    startup is a no-op after the first successful boot.
    """
    # Partial HNSW index: pgvector rejects nulls in HNSW. Reflections
    # may briefly land with NULL embedding (LLM produced text but the
    # embed call failed and we did not block on it). The partial
    # predicate keeps the index small and skips not-yet-embedded rows.
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_reflections_embedding
            ON reflections USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE embedding IS NOT NULL
            """
        )
    )
    # GIN on parent_reflection_ids — supports the "find rollups that
    # contain this daily reflection" query #40 needs. Cheap to add now;
    # avoids a follow-up migration when #40 lands.
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_reflections_parents
            ON reflections USING gin (parent_reflection_ids)
            """
        )
    )
    # #41: pattern_alerts.trace_ids is uuid[]. GIN supports the
    # @> containment operator the trace inspector will use to find
    # alerts that mention a given trace ("is this turn part of any
    # surfaced cluster?").
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_pattern_alerts_trace_ids
            ON pattern_alerts USING gin (trace_ids)
            """
        )
    )
