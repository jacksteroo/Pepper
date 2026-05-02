"""Idempotent post-`create_all` SQL for the `traces` table.

Three things land here that SQLAlchemy's `Base.metadata.create_all`
cannot express:

1. Specialised indexes — partial HNSW on `embedding`, GIN with
   `jsonb_path_ops` on `tools_called`.
2. Per-column `UPDATE` grants that materialise ADR-0005's
   "Postgres roles & grants" mandate.
3. Role creation (writer / compactor / reader) for callers that have
   not yet been split onto per-role connections.

`apply_traces_migration(conn)` is called from `agent.db.init_db` after
`create_all` and is safe to re-run on every startup.

Limitation, deliberately documented: the application's existing
`pepper` Postgres user remains the table owner because the SQLAlchemy
engine connects as that user and `create_all` makes the connecting
user the owner. Owners bypass `GRANT` checks at the database layer,
so the role-level enforcement these grants describe is **advisory**
until the application is split onto per-role connections (filed as a
follow-up — see ADR-0005 §"Postgres roles & grants"). The repository
layer (`agent/traces/repository.py`) is the operative enforcement
today and exposes no UPDATE/DELETE surface.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Roles defined in ADR-0005. Callers that want to run with reduced
# privilege connect with a role that inherits ONE of these.
ROLE_WRITER = "pepper_traces_writer"
ROLE_COMPACTOR = "pepper_traces_compactor"
ROLE_READER = "pepper_traces_reader"

# Columns the compactor role may UPDATE. Anything outside this set is
# write-once at INSERT.
COMPACTOR_UPDATE_COLUMNS: tuple[str, ...] = (
    "embedding",
    "embedding_model_version",
    "tier",
    "user_reaction",
)


def _create_role_sql(role: str) -> str:
    # Postgres has no `CREATE ROLE IF NOT EXISTS` even on modern versions.
    # The DO/EXISTS pattern below is the documented idempotent form.
    return f"""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{role}') THEN
            CREATE ROLE {role} NOLOGIN;
        END IF;
    END
    $$;
    """


async def _exec(conn: AsyncConnection, sql: str) -> None:
    await conn.execute(text(sql))


async def apply_traces_migration(conn: AsyncConnection) -> None:
    """Apply traces-specific schema, indexes, roles, and grants.

    Idempotent. Safe to call on every `init_db` invocation. Does not
    raise on a fresh DB or on a re-run.
    """
    # ── Indexes ──
    # Partial HNSW: pgvector rejects nulls in HNSW, and recall-tier
    # rows have NULL embedding by design (see #21). Filtering keeps
    # the index bounded by the working window.
    await _exec(
        conn,
        """
        CREATE INDEX IF NOT EXISTS idx_traces_embedding
        ON traces USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        WHERE embedding IS NOT NULL
        """,
    )
    # GIN with jsonb_path_ops — supports the `@>` containment operator
    # the "which traces called X" pattern uses, smaller than full GIN.
    await _exec(
        conn,
        """
        CREATE INDEX IF NOT EXISTS idx_traces_tools_called
        ON traces USING gin (tools_called jsonb_path_ops)
        """,
    )

    # ── Roles ──
    await _exec(conn, _create_role_sql(ROLE_WRITER))
    await _exec(conn, _create_role_sql(ROLE_COMPACTOR))
    await _exec(conn, _create_role_sql(ROLE_READER))

    # ── Grants ──
    # Wipe any prior grants on these roles so re-runs converge on the
    # documented set even after we narrow it. REVOKE is idempotent.
    for role in (ROLE_WRITER, ROLE_COMPACTOR, ROLE_READER):
        await _exec(conn, f"REVOKE ALL ON traces FROM {role}")

    # Writer: INSERT only.
    await _exec(conn, f"GRANT INSERT ON traces TO {ROLE_WRITER}")

    # Compactor: SELECT plus per-column UPDATE on the carve-out columns.
    await _exec(conn, f"GRANT SELECT ON traces TO {ROLE_COMPACTOR}")
    cols = ", ".join(COMPACTOR_UPDATE_COLUMNS)
    await _exec(
        conn,
        f"GRANT UPDATE ({cols}) ON traces TO {ROLE_COMPACTOR}",
    )

    # Reader: SELECT only.
    await _exec(conn, f"GRANT SELECT ON traces TO {ROLE_READER}")

    # Make the application's existing connection inherit writer + compactor.
    # The reader role stays unattached — it's for E4/E5 future per-role
    # connections (and for the #24 HTTP route once role-based pooling lands).
    # CURRENT_USER avoids hardcoding `pepper` so this still works in dev
    # environments that connect as a different user.
    await _exec(
        conn,
        f"GRANT {ROLE_WRITER} TO CURRENT_USER",
    )
    await _exec(
        conn,
        f"GRANT {ROLE_COMPACTOR} TO CURRENT_USER",
    )
