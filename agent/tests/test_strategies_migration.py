"""Migration-SQL tests for the strategies table.

Verify the *content* of the SQL the migration emits without requiring a
live Postgres. Locks in:
- HNSW index on the embedding column is partial (`WHERE embedding IS NOT NULL`).
- GIN index on `source_trace_ids` exists.
- Statements are idempotent (`IF NOT EXISTS`).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.strategies.migration import apply_strategies_migration


@pytest.fixture
def captured_sql() -> tuple[AsyncMock, list[str]]:
    statements: list[str] = []

    async def _execute(stmt):
        statements.append(getattr(stmt, "text", str(stmt)))
        return MagicMock()

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=_execute)
    return conn, statements


class TestStrategiesMigration:
    @pytest.mark.asyncio
    async def test_hnsw_index_is_partial(self, captured_sql) -> None:
        conn, stmts = captured_sql
        await apply_strategies_migration(conn)
        joined = "\n".join(stmts).lower()
        # The partial predicate matters: pgvector rejects NULLs in HNSW.
        assert "idx_strategies_embedding" in joined
        assert "hnsw" in joined
        assert "embedding is not null" in joined

    @pytest.mark.asyncio
    async def test_gin_on_source_trace_ids(self, captured_sql) -> None:
        conn, stmts = captured_sql
        await apply_strategies_migration(conn)
        joined = "\n".join(stmts).lower()
        assert "idx_strategies_source_trace_ids" in joined
        assert "using gin" in joined
        assert "source_trace_ids" in joined

    @pytest.mark.asyncio
    async def test_statements_are_idempotent(self, captured_sql) -> None:
        conn, stmts = captured_sql
        await apply_strategies_migration(conn)
        for s in stmts:
            assert "if not exists" in s.lower(), (
                f"every CREATE INDEX must use IF NOT EXISTS — found:\n{s}"
            )
