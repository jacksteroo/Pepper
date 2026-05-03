"""Append-only repository for the `traces` table.

Surface intentionally restricted: `append`, `get_by_id`, `query`,
`find_similar`, plus the narrow compactor entry points
(`set_embedding`, `advance_tier`, `set_user_reaction`). No `update_*`,
no `delete_*`. Tests assert via `hasattr` that the disallowed methods
do not exist (#20 acceptance criterion).

Database-layer enforcement (per-column UPDATE grants) is documented in
`agent/traces/migration.py` but is **advisory** until the application
is split onto per-role connections — see ADR-0005. This repository is
the operative privacy boundary today.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Optional

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from agent.error_classifier import DataSensitivity
from agent.traces.models import TraceRow
from agent.traces.schema import (
    EMBEDDING_DIM,
    Archetype,
    Trace,
    TraceTier,
    TriggerSource,
)

logger = structlog.get_logger(__name__)

# Defensive caps on the query surface. Single-tenant local-first means the
# DoS surface is the owner against themselves, but a runaway caller (e.g. a
# UI bug that passes `limit=10**9`) still hurts. These are budgets, not
# correctness bounds — bump them when there's a real reason.
MAX_QUERY_LIMIT: int = 1000
MAX_FILTER_TEXT_LEN: int = 1024

# Allowed keys on `user_reaction` per docs/trace-schema.md. Compactor entry
# point validates against this set so a malformed payload fails at the
# repository, not deep inside the JSONB column.
_USER_REACTION_KEYS = frozenset({"thumbs", "followup_correction", "source"})


# ── Mapping helpers ───────────────────────────────────────────────────────────


def _trace_to_row(t: Trace) -> TraceRow:
    return TraceRow(
        trace_id=uuid.UUID(t.trace_id),
        created_at=t.created_at,
        trigger_source=t.trigger_source.value,
        archetype=t.archetype.value,
        scheduler_job_name=t.scheduler_job_name,
        input=t.input,
        assembled_context=t.assembled_context,
        output=t.output,
        model_selected=t.model_selected,
        model_version=t.model_version,
        prompt_version=t.prompt_version,
        tools_called=t.tools_called,
        latency_ms=t.latency_ms,
        user_reaction=t.user_reaction,
        data_sensitivity=t.data_sensitivity.value,
        embedding=t.embedding,
        embedding_model_version=t.embedding_model_version,
        tier=t.tier.value,
    )


def _row_to_trace(r: TraceRow) -> Trace:
    """Materialize an ORM row back to a `Trace` dataclass.

    Caller MUST ensure deferred columns (`assembled_context`,
    `tools_called`, `embedding`) have been loaded — typically via
    `query()`'s `with_payload=True` flag or `get_by_id()` (which
    always undefers them). Calling this against a row that hasn't
    loaded its deferred columns will trigger an implicit lazy-load
    in async context which raises.
    """
    return Trace(
        trace_id=str(r.trace_id),
        created_at=r.created_at,
        trigger_source=TriggerSource(r.trigger_source),
        archetype=Archetype(r.archetype),
        scheduler_job_name=r.scheduler_job_name,
        input=r.input,
        assembled_context=r.assembled_context or {},
        output=r.output,
        model_selected=r.model_selected,
        model_version=r.model_version,
        prompt_version=r.prompt_version,
        tools_called=r.tools_called or [],
        latency_ms=r.latency_ms,
        user_reaction=r.user_reaction,
        data_sensitivity=DataSensitivity(r.data_sensitivity),
        embedding=r.embedding,
        embedding_model_version=r.embedding_model_version,
        tier=TraceTier(r.tier),
    )


# ── Repository ────────────────────────────────────────────────────────────────


class TraceRepository:
    """Append-only repository for traces.

    Methods on this class are the *only* sanctioned way to touch the
    traces table from application code. The class deliberately does not
    expose any method whose name implies mutation of the conversation
    payload (`update_*`, `replace_*`, `delete_*`, `purge_*`). The three
    compactor methods (`set_embedding`, `advance_tier`,
    `set_user_reaction`) are the documented carve-outs from
    append-only — see ADR-0005 §Mutability.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Append ────────────────────────────────────────────────────────────

    async def append(self, trace: Trace) -> Trace:
        """Insert a new trace. Returns the trace as persisted (with any
        server-side defaults — e.g. `created_at` — resolved).

        The caller (`agent/core.py` via #22) is responsible for
        fail-soft semantics — trace persistence failure must never
        break the user's turn.
        """
        row = _trace_to_row(trace)
        self._session.add(row)
        await self._session.flush()
        # Refresh so the returned Trace reflects what's actually in the
        # row — round-trip equality (#20 acceptance criterion) requires
        # this. Without it, server_default could clobber `created_at`
        # while the returned Trace still holds the Python-side value.
        await self._session.refresh(row)
        return _row_to_trace(row)

    # ── Read ──────────────────────────────────────────────────────────────

    async def get_by_id(self, trace_id: str) -> Optional[Trace]:
        """Fetch a trace by id with the heavy columns loaded."""
        stmt = (
            select(TraceRow)
            .where(TraceRow.trace_id == uuid.UUID(trace_id))
            .options(
                undefer(TraceRow.assembled_context),
                undefer(TraceRow.tools_called),
                undefer(TraceRow.embedding),
            )
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _row_to_trace(row) if row is not None else None

    async def query(
        self,
        *,
        archetype: Optional[Archetype] = None,
        trigger_source: Optional[TriggerSource] = None,
        model_selected: Optional[str] = None,
        data_sensitivity: Optional[DataSensitivity] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        contains_text: Optional[str] = None,
        tier: Optional[TraceTier] = None,
        limit: int = 100,
        cursor: Optional[tuple[datetime, str]] = None,
        with_payload: bool = False,
    ) -> Sequence[Trace]:
        """Return traces matching the given filters, newest first.

        Cursor is a `(created_at, trace_id)` tuple — composite to keep
        pagination stable when multiple rows share a `created_at`.

        `with_payload=False` (the default) leaves `assembled_context`,
        `tools_called`, and `embedding` deferred — list-view callers
        save the jsonb/vector load. Detail-view callers pass
        `with_payload=True` to get the full row.
        """
        # ── Defensive caps on free-string filters ──
        if model_selected is not None and len(model_selected) > MAX_FILTER_TEXT_LEN:
            raise ValueError(f"model_selected exceeds {MAX_FILTER_TEXT_LEN} chars")
        if contains_text is not None and len(contains_text) > MAX_FILTER_TEXT_LEN:
            raise ValueError(f"contains_text exceeds {MAX_FILTER_TEXT_LEN} chars")
        # `limit` is bounded — caller's pagination loop must respect cursor.
        limit = max(1, min(limit, MAX_QUERY_LIMIT))

        stmt = select(TraceRow).order_by(
            TraceRow.created_at.desc(),
            TraceRow.trace_id.desc(),
        ).limit(limit)
        if archetype is not None:
            stmt = stmt.where(TraceRow.archetype == archetype.value)
        if trigger_source is not None:
            stmt = stmt.where(TraceRow.trigger_source == trigger_source.value)
        if model_selected is not None:
            stmt = stmt.where(TraceRow.model_selected == model_selected)
        if data_sensitivity is not None:
            stmt = stmt.where(TraceRow.data_sensitivity == data_sensitivity.value)
        if tier is not None:
            stmt = stmt.where(TraceRow.tier == tier.value)
        if since is not None:
            stmt = stmt.where(TraceRow.created_at >= since)
        if until is not None:
            stmt = stmt.where(TraceRow.created_at <= until)
        if cursor is not None:
            cursor_at, cursor_id = cursor
            # Composite descending cursor: row pairs strictly less than
            # the cursor (lexicographic on the tuple). Stable across ties.
            stmt = stmt.where(
                or_(
                    TraceRow.created_at < cursor_at,
                    and_(
                        TraceRow.created_at == cursor_at,
                        TraceRow.trace_id < uuid.UUID(cursor_id),
                    ),
                ),
            )
        if contains_text is not None:
            # Until #20-followup adds tsvector, fall back to LIKE.
            # Caller-supplied text is parameterised by SQLAlchemy.
            pattern = f"%{contains_text}%"
            stmt = stmt.where(
                (TraceRow.input.ilike(pattern)) | (TraceRow.output.ilike(pattern)),
            )

        if with_payload:
            # Undefer ALL three deferred columns (assembled_context,
            # tools_called, embedding). The docstring promises "the
            # full row"; previously embedding was left deferred,
            # which made `_row_to_trace`'s `r.embedding` access
            # implicitly lazy-load and fail in async context.
            # Discovered via #41's pattern detector consuming the
            # vector outside the session block.
            stmt = stmt.options(
                undefer(TraceRow.assembled_context),
                undefer(TraceRow.tools_called),
                undefer(TraceRow.embedding),
            )
            result = await self._session.execute(stmt)
            rows = result.scalars().all()
            return [_row_to_trace(r) for r in rows]

        # Without payload, _row_to_trace would trigger lazy-load on the
        # deferred columns. Project to empty placeholders instead — list
        # callers don't read the heavy fields. We DO project a single
        # cheap JSONB scalar (#34): the capability_block_version key on
        # ``assembled_context``. The inspector uses it to find the prior
        # version without an N+1 detail-fetch loop.
        from sqlalchemy import literal_column

        cap_version_expr = literal_column(
            "assembled_context->>'capability_block_version'"
        ).label("capability_block_version")
        list_stmt = stmt.add_columns(cap_version_expr)
        result = await self._session.execute(list_stmt)
        out: list[Trace] = []
        for row_tuple in result.all():
            r = row_tuple[0]
            cap_version = row_tuple[1] if len(row_tuple) > 1 else None
            placeholder_ctx: dict[str, Any] = {}
            if cap_version:
                placeholder_ctx["capability_block_version"] = cap_version
            out.append(
                Trace(
                    trace_id=str(r.trace_id),
                    created_at=r.created_at,
                    trigger_source=TriggerSource(r.trigger_source),
                    archetype=Archetype(r.archetype),
                    scheduler_job_name=r.scheduler_job_name,
                    input=r.input,
                    assembled_context=placeholder_ctx,
                    output=r.output,
                    model_selected=r.model_selected,
                    model_version=r.model_version,
                    prompt_version=r.prompt_version,
                    tools_called=[],
                    latency_ms=r.latency_ms,
                    user_reaction=r.user_reaction,
                    data_sensitivity=DataSensitivity(r.data_sensitivity),
                    embedding=None,
                    embedding_model_version=r.embedding_model_version,
                    tier=TraceTier(r.tier),
                ),
            )
        return out

    async def find_similar(
        self,
        embedding: Sequence[float],
        *,
        limit: int = 10,
    ) -> Sequence[tuple[str, float]]:
        """Return (trace_id, distance) pairs for the nearest neighbors.

        Returns ids only — callers re-fetch full rows via `get_by_id`
        if they need them. Per Read patterns in `docs/trace-schema.md`.
        """
        if len(embedding) != EMBEDDING_DIM:
            raise ValueError(
                f"embedding dimension must be {EMBEDDING_DIM}, got {len(embedding)}",
            )
        limit = max(1, min(limit, MAX_QUERY_LIMIT))
        # `<=>` is pgvector cosine distance; the partial HNSW index
        # `WHERE embedding IS NOT NULL` covers this exact predicate.
        stmt = (
            select(
                TraceRow.trace_id,
                TraceRow.embedding.cosine_distance(list(embedding)).label("dist"),
            )
            .where(TraceRow.embedding.isnot(None))
            .order_by("dist")
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [(str(tid), float(dist)) for tid, dist in result.all()]

    # ── Compactor surface (narrow UPDATE carve-outs) ──────────────────────
    #
    # NOTE: Forward-only / shape invariants enforced here are
    # application-layer guards. Database-layer enforcement (per ADR-0005)
    # is advisory today because the application connects as the table
    # owner. A future PR splitting onto per-role connections promotes
    # these guards to defense-in-depth rather than sole enforcement.

    async def set_embedding(
        self,
        trace_id: str,
        embedding: Sequence[float],
        embedding_model_version: str,
    ) -> None:
        """Backfill `embedding` + `embedding_model_version` on a row.

        Carve-out from append-only per ADR-0005 §Mutability — only the
        embedding worker uses this path.
        """
        if len(embedding) != EMBEDDING_DIM:
            raise ValueError(
                f"embedding dimension must be {EMBEDDING_DIM}, got {len(embedding)}",
            )
        if not embedding_model_version:
            raise ValueError("embedding_model_version is required")
        row = await self._session.get(TraceRow, uuid.UUID(trace_id))
        if row is None:
            raise LookupError(f"trace {trace_id} not found")
        row.embedding = list(embedding)
        row.embedding_model_version = embedding_model_version
        await self._session.flush()

    async def advance_tier(self, trace_id: str, new_tier: TraceTier) -> None:
        """Advance a row's compression tier.

        Forward-only: same-tier is an idempotent no-op (the nightly job
        from #21 may re-run on a partially-completed batch); explicit
        backwards transitions raise.
        """
        row = await self._session.get(TraceRow, uuid.UUID(trace_id))
        if row is None:
            raise LookupError(f"trace {trace_id} not found")
        order = {
            TraceTier.WORKING.value: 0,
            TraceTier.RECALL.value: 1,
            TraceTier.ARCHIVAL.value: 2,
        }
        cur = order[row.tier]
        nxt = order[new_tier.value]
        if nxt < cur:
            raise ValueError(
                f"cannot transition tier {row.tier} → {new_tier.value} (forward-only)",
            )
        if nxt == cur:
            return  # idempotent no-op
        row.tier = new_tier.value
        await self._session.flush()

    async def set_user_reaction(
        self,
        trace_id: str,
        reaction: dict[str, Any],
    ) -> None:
        """Record a user reaction against a previously-persisted trace.

        Validates the payload shape against the schema in
        `docs/trace-schema.md` so a malformed reaction (typo, extra
        fields) fails at the repository rather than corrupting JSONB.
        """
        if not isinstance(reaction, dict):
            raise TypeError("reaction must be a dict")
        unknown = reaction.keys() - _USER_REACTION_KEYS
        if unknown:
            raise ValueError(
                f"unknown user_reaction keys: {sorted(unknown)} "
                f"(allowed: {sorted(_USER_REACTION_KEYS)})",
            )
        row = await self._session.get(TraceRow, uuid.UUID(trace_id))
        if row is None:
            raise LookupError(f"trace {trace_id} not found")
        row.user_reaction = reaction
        await self._session.flush()
