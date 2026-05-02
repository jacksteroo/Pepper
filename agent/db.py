from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Base — all models inherit from this
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Lazy engine / session factory — initialised once at startup via init_db()
# ---------------------------------------------------------------------------

_engine = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


async def init_db(config=None) -> None:
    """Create tables and pgvector extension.  Call once at application startup."""
    global _engine, _session_factory

    if config is None:
        from agent.config import settings as config  # type: ignore[assignment]

    _engine = create_async_engine(
        config.POSTGRES_URL,
        echo=False,
        pool_pre_ping=True,
    )

    _session_factory = async_sessionmaker(
        _engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async with _engine.begin() as conn:
        # 1. Enable pgvector extension
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        # 1a. Ensure the traces SQLAlchemy model is imported before
        # `Base.metadata.create_all` runs — the model registers itself
        # with `Base.metadata` only at import time.
        import agent.traces.models  # noqa: F401  (side-effect import)

        # 2. Create all tables defined via Base
        await conn.run_sync(Base.metadata.create_all)

        # 2a. Phase 2 Task 0: routing_events.query_embedding migrated from
        # vector(768) (nomic-embed-text) to vector(1024) (qwen3-embedding:0.6b).
        # SQLAlchemy create_all does not alter existing columns, so we issue
        # an explicit, idempotent ALTER. Pre-existing rows have their
        # embedding nulled out — the companion re-embed script
        # (scripts/router_phase2_task0_reembed.py) backfills them.
        existing_dim = await conn.scalar(
            text(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = 'public.routing_events'::regclass "
                "AND attname = 'query_embedding'"
            )
        )
        if existing_dim is not None and existing_dim != 1024:
            await conn.execute(
                text("DROP INDEX IF EXISTS idx_routing_events_query_embedding")
            )
            await conn.execute(
                text(
                    "ALTER TABLE routing_events "
                    "ALTER COLUMN query_embedding TYPE vector(1024) USING NULL"
                )
            )

        # 3. HNSW indexes for embedding columns (idempotent)
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_memory_events_embedding "
                "ON memory_events USING hnsw (embedding vector_cosine_ops) "
                "WITH (m=16, ef_construction=64)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_conversations_embedding "
                "ON conversations USING hnsw (embedding vector_cosine_ops) "
                "WITH (m=16, ef_construction=64)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_routing_events_query_embedding "
                "ON routing_events USING hnsw (query_embedding vector_cosine_ops) "
                "WITH (m=16, ef_construction=64)"
            )
        )
        # Time-bounded queries on routing_events scan newest-first.
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_routing_events_timestamp_desc "
                "ON routing_events (timestamp DESC)"
            )
        )
        # Outbound channel message coordinates for reaction-based feedback
        # capture. Existing tables predate these columns; ADD COLUMN
        # IF NOT EXISTS keeps init_db idempotent across upgrades.
        await conn.execute(
            text(
                "ALTER TABLE routing_events "
                "ADD COLUMN IF NOT EXISTS outbound_chat_id BIGINT"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE routing_events "
                "ADD COLUMN IF NOT EXISTS outbound_message_id BIGINT"
            )
        )
        # Reaction lookups arrive keyed by (chat_id, message_id); index that
        # composite. Sparse — only the recent live-channel rows have values.
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_routing_events_outbound_msg "
                "ON routing_events (outbound_chat_id, outbound_message_id) "
                "WHERE outbound_message_id IS NOT NULL"
            )
        )

        # Phase 2 — router_exemplars: HNSW over the embedding column for
        # k-NN classification. Active rows only — archived rows are kept
        # for history but excluded from the live index.
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_router_exemplars_embedding "
                "ON router_exemplars USING hnsw (embedding vector_cosine_ops) "
                "WITH (m=16, ef_construction=64)"
            )
        )
        # Idempotency unique key for bootstrap loader: same query+intent+tier
        # is treated as a single exemplar (re-runs don't duplicate).
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_router_exemplars_dedup "
                "ON router_exemplars (query_text, intent_label, tier) "
                "WHERE archived_at IS NULL"
            )
        )

        # Epic 01 (#20) — traces table: specialised indexes, role
        # creation, and per-column UPDATE grants live in their own
        # module to keep `init_db` readable.
        from agent.traces.migration import apply_traces_migration

        await apply_traces_migration(conn)


def get_engine():
    """Return the initialised SQLAlchemy async engine.

    Raises RuntimeError if init_db() has not been called yet.
    """
    if _engine is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _engine


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Async generator providing a DB session — use as a FastAPI dependency."""
    if _session_factory is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    async with _session_factory() as session:
        yield session
