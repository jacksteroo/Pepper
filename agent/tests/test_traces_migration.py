"""Tests for the traces table migration SQL.

These tests verify the *content* of the SQL the migration emits without
requiring a live Postgres. They lock in the ADR-0005 mandate that:

- Three roles are created with NOLOGIN (writer / compactor / reader).
- Writer gets INSERT only.
- Compactor gets SELECT plus per-column UPDATE on exactly the documented
  carve-out columns — never global UPDATE.
- Reader gets SELECT only.
- No grant in this migration includes DELETE on `traces`.
- The HNSW embedding index is partial (`WHERE embedding IS NOT NULL`).
- The tools_called GIN index uses `jsonb_path_ops`.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.traces.migration import (
    COMPACTOR_UPDATE_COLUMNS,
    ROLE_COMPACTOR,
    ROLE_READER,
    ROLE_WRITER,
    apply_traces_migration,
)


@pytest.fixture
def captured_sql() -> tuple[AsyncMock, list[str]]:
    """Return a (conn_mock, statements) pair where every text(...) executed
    against `conn_mock` is captured into the `statements` list."""
    statements: list[str] = []

    async def _execute(stmt):
        # SQLAlchemy text() objects expose their SQL via .text
        statements.append(getattr(stmt, "text", str(stmt)))
        return MagicMock()

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=_execute)
    return conn, statements


class TestRoleCreation:
    @pytest.mark.asyncio
    async def test_creates_three_roles(self, captured_sql) -> None:
        conn, statements = captured_sql
        await apply_traces_migration(conn)
        joined = "\n".join(statements)
        assert ROLE_WRITER in joined
        assert ROLE_COMPACTOR in joined
        assert ROLE_READER in joined

    @pytest.mark.asyncio
    async def test_roles_are_nologin(self, captured_sql) -> None:
        conn, statements = captured_sql
        await apply_traces_migration(conn)
        # Each role-creation block must declare NOLOGIN — the roles are
        # group roles for inheritance, not login identities.
        for role in (ROLE_WRITER, ROLE_COMPACTOR, ROLE_READER):
            create_stmt = next(
                (s for s in statements if f"CREATE ROLE {role}" in s),
                None,
            )
            assert create_stmt is not None, f"no CREATE ROLE for {role}"
            assert "NOLOGIN" in create_stmt

    @pytest.mark.asyncio
    async def test_role_creation_is_idempotent(self, captured_sql) -> None:
        conn, statements = captured_sql
        await apply_traces_migration(conn)
        # Idempotent guard for each role creation — the DO/IF NOT EXISTS
        # pattern is the only PG-portable form.
        for role in (ROLE_WRITER, ROLE_COMPACTOR, ROLE_READER):
            block = next(s for s in statements if f"CREATE ROLE {role}" in s)
            assert "IF NOT EXISTS" in block
            assert "pg_catalog.pg_roles" in block


class TestGrants:
    @pytest.mark.asyncio
    async def test_writer_gets_insert_only(self, captured_sql) -> None:
        conn, statements = captured_sql
        await apply_traces_migration(conn)
        writer_grants = [
            s for s in statements if f"TO {ROLE_WRITER}" in s and s.strip().startswith("GRANT")
        ]
        # Exactly one GRANT statement — INSERT.
        assert len(writer_grants) == 1
        assert "GRANT INSERT ON traces" in writer_grants[0]
        # No UPDATE / DELETE / TRUNCATE for the writer role.
        for stmt in writer_grants:
            assert "UPDATE" not in stmt
            assert "DELETE" not in stmt
            assert "TRUNCATE" not in stmt

    @pytest.mark.asyncio
    async def test_compactor_gets_select_plus_per_column_update(
        self,
        captured_sql,
    ) -> None:
        conn, statements = captured_sql
        await apply_traces_migration(conn)
        compactor_stmts = [s for s in statements if f"TO {ROLE_COMPACTOR}" in s]
        # SELECT grant present.
        assert any("GRANT SELECT ON traces" in s for s in compactor_stmts)
        # Per-column UPDATE grant present and lists ONLY the carve-out columns.
        update_stmts = [s for s in compactor_stmts if "GRANT UPDATE" in s]
        assert len(update_stmts) == 1
        update = update_stmts[0]
        for col in COMPACTOR_UPDATE_COLUMNS:
            assert col in update
        # No global UPDATE grant — the parenthesised column list IS the
        # mandate, and adding a bare `GRANT UPDATE ON traces` would
        # nullify it. Check by ensuring "GRANT UPDATE" only appears with
        # an open parenthesis after it.
        for stmt in update_stmts:
            assert "GRANT UPDATE (" in stmt

    @pytest.mark.asyncio
    async def test_reader_gets_select_only(self, captured_sql) -> None:
        conn, statements = captured_sql
        await apply_traces_migration(conn)
        reader_stmts = [
            s for s in statements if f"TO {ROLE_READER}" in s and s.strip().startswith("GRANT")
        ]
        assert len(reader_stmts) == 1
        assert "GRANT SELECT ON traces" in reader_stmts[0]
        for stmt in reader_stmts:
            assert "UPDATE" not in stmt
            assert "INSERT" not in stmt
            assert "DELETE" not in stmt

    @pytest.mark.asyncio
    async def test_no_grant_anywhere_includes_delete(self, captured_sql) -> None:
        conn, statements = captured_sql
        await apply_traces_migration(conn)
        for stmt in statements:
            if stmt.strip().startswith("GRANT"):
                assert "DELETE" not in stmt, f"DELETE leaked into a GRANT: {stmt}"

    @pytest.mark.asyncio
    async def test_revoke_runs_before_grants(self, captured_sql) -> None:
        # Re-runs of init_db must converge on the documented grant set
        # even after the spec narrows. REVOKE ALL precedes any GRANT.
        conn, statements = captured_sql
        await apply_traces_migration(conn)
        for role in (ROLE_WRITER, ROLE_COMPACTOR, ROLE_READER):
            revoke_idx = next(
                i
                for i, s in enumerate(statements)
                if f"REVOKE ALL ON traces FROM {role}" in s
            )
            grant_idx = next(
                (
                    i
                    for i, s in enumerate(statements)
                    if s.strip().startswith("GRANT") and f"TO {role}" in s
                ),
                None,
            )
            if grant_idx is not None:
                assert revoke_idx < grant_idx

    @pytest.mark.asyncio
    async def test_current_user_inherits_writer_and_compactor(
        self,
        captured_sql,
    ) -> None:
        # The application's existing connection inherits writer +
        # compactor so the path through `agent/core.py` keeps working
        # without a per-role connection split.
        conn, statements = captured_sql
        await apply_traces_migration(conn)
        joined = "\n".join(statements)
        assert f"GRANT {ROLE_WRITER} TO CURRENT_USER" in joined
        assert f"GRANT {ROLE_COMPACTOR} TO CURRENT_USER" in joined
        # Reader is NOT inherited — it's reserved for future per-role
        # connections (E4 reflector, E5 optimizer, #24 HTTP route).
        assert f"GRANT {ROLE_READER} TO CURRENT_USER" not in joined


class TestIndexes:
    @pytest.mark.asyncio
    async def test_hnsw_index_is_partial_on_non_null_embedding(
        self,
        captured_sql,
    ) -> None:
        conn, statements = captured_sql
        await apply_traces_migration(conn)
        hnsw = next(s for s in statements if "USING hnsw" in s and "traces" in s)
        assert "WHERE embedding IS NOT NULL" in hnsw
        assert "vector_cosine_ops" in hnsw

    @pytest.mark.asyncio
    async def test_tools_called_gin_uses_jsonb_path_ops(
        self,
        captured_sql,
    ) -> None:
        conn, statements = captured_sql
        await apply_traces_migration(conn)
        gin = next(s for s in statements if "USING gin" in s and "tools_called" in s)
        assert "jsonb_path_ops" in gin
