"""Postgres connection factory for agent processes.

Lives in `_shared/` because every archetype talks to the same Postgres
instance Pepper Core uses (the trace store + per-archetype output
tables) and re-implementing the engine/session boilerplate inside each
archetype would risk subtle divergence (different pool sizes,
different `pool_pre_ping`, different encoding). The factory returns
fresh engine/session objects on each call; nothing is held at module
scope.

Note: this module deliberately does NOT import `agent.db` so the
`agents/` boundary remains clean (per ADR-0004 §"Isolation rule").
The shape is mirrored by hand and any divergence from `agent/db.py`
is a review concern.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(postgres_url: str) -> AsyncEngine:
    """Create a fresh async engine for an archetype process.

    `pool_pre_ping=True` matches `agent.db.init_db` — it's what guards
    against stale connections after Postgres restarts. `echo=False`
    keeps SQL out of agent logs by default; per-archetype debugging
    can wrap individual sessions if needed.
    """
    return create_async_engine(
        postgres_url,
        echo=False,
        pool_pre_ping=True,
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to `engine`.

    `expire_on_commit=False` matches `agent.db.init_db` — it lets the
    caller keep using attribute access on ORM rows after commit, which
    is the common shape for read-mostly archetype loops.
    """
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
