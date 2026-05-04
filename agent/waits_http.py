"""FastAPI router for the wait panel (#55).

Exposes:

- `GET /api/waits` — recent waits with reasons, newest first.

Backed by the `traces` table: a wait turn is a row whose
`tools_called` JSONB array contains an entry with `name = "wait"`. The
GIN/jsonb_path_ops index from #20 makes the `@>` containment query
fast. There is no separate `pepper_waits` table — the trace itself is
the canonical record.

Privacy posture mirrors `agent/traces/http.py`:

1. **API-key required** via `require_api_key`.
2. **Localhost bind by default** when `PEPPER_BIND_LOCALHOST_ONLY` is
   true. Wait reasons commonly carry sensitive observation about the
   operator's affect ("Jack seemed off, don't pile on with the email
   triage"); the same access controls used for raw trace contents
   apply here.
3. **No mutation surface.** Read-only by construction — append-only
   trace store enforces this at the storage layer.
"""
from __future__ import annotations

import ipaddress
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text as _sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from agent.auth import require_api_key
from agent.db import get_db
from agent.wait_tool import _try_parse_iso

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/waits", tags=["waits"])


# ── Localhost guard (mirrors agents/reflector/http.py) ───────────────────────


def _client_is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if not host:
        return False
    if host in {"localhost"} or host.startswith("127."):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def _enforce_localhost_bind(request: Request) -> None:
    from agent.config import settings

    bind_localhost = getattr(settings, "PEPPER_BIND_LOCALHOST_ONLY", True)
    if bind_localhost and not _client_is_loopback(request):
        host = request.client.host if request.client else "<unknown>"
        logger.warning(
            "waits_endpoint_non_loopback_denied",
            client_host=host,
            path=str(request.url.path),
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "/waits is bound to localhost. Set "
                "PEPPER_BIND_LOCALHOST_ONLY=false AND wire session-level "
                "auth before exposing externally."
            ),
        )


# ── Response models ──────────────────────────────────────────────────────────


class WaitEntry(BaseModel):
    trace_id: str
    created_at: str
    reason: str
    until_raw: Optional[str] = None
    until_iso: Optional[str] = None
    trigger_source: str
    scheduler_job_name: Optional[str] = None


class WaitsListResponse(BaseModel):
    waits: list[WaitEntry]


# ── Routes ───────────────────────────────────────────────────────────────────


_DEFAULT_LIMIT: int = 50
_MAX_LIMIT: int = 200


def _extract_wait_args(tools_called: Any) -> Optional[dict[str, Any]]:
    """Return the args dict of the *last* wait-tool call in `tools_called`,
    or None if there is none.

    The GIN containment query has already filtered to rows that contain
    a wait call somewhere; this projection picks out the args payload
    so the panel does not have to walk the full tool-call list itself.

    Edge case: a turn that called `wait` more than once (e.g. the model
    retried after a tool result it found unsatisfying) records each call
    in order. The last entry is the model's final decision, so that's
    the one the panel surfaces.
    """
    if not isinstance(tools_called, list):
        return None
    last_args: Optional[dict[str, Any]] = None
    for call in tools_called:
        if not isinstance(call, dict):
            continue
        if call.get("name") == "wait":
            args = call.get("args")
            last_args = args if isinstance(args, dict) else {}
    return last_args


@router.get(
    "",
    response_model=WaitsListResponse,
    dependencies=[Depends(require_api_key), Depends(_enforce_localhost_bind)],
)
async def list_waits(
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    db: AsyncSession = Depends(get_db),
) -> WaitsListResponse:
    """Return the most recent wait traces, newest first."""
    # `@>` containment uses the GIN(jsonb_path_ops) index on
    # `traces.tools_called`. Cast the JSON literal explicitly so the
    # planner picks the index regardless of the column's storage type
    # at runtime.
    stmt = _sql_text(
        """
        SELECT
            trace_id,
            created_at,
            tools_called,
            trigger_source,
            scheduler_job_name
        FROM traces
        WHERE tools_called @> CAST(:probe AS jsonb)
        ORDER BY created_at DESC
        LIMIT :limit
        """
    )
    rows = (
        await db.execute(
            stmt,
            {"probe": '[{"name": "wait"}]', "limit": limit},
        )
    ).fetchall()

    out: list[WaitEntry] = []
    for r in rows:
        # SQLAlchemy returns Row tuples — index by position to avoid
        # dialect-specific row mappings. Order matches the SELECT.
        trace_id = str(r[0])
        created_at = r[1].isoformat() if r[1] is not None else ""
        wait_args = _extract_wait_args(r[2]) or {}
        reason = str(wait_args.get("reason") or "")
        until_raw = wait_args.get("until")
        until_raw = str(until_raw) if until_raw else None
        # Re-parse `until` server-side: the trace's tool_call entry
        # carries only the raw string the model passed (the wait
        # tool's parsed `until_iso` is in the LLM-facing tool result,
        # which is not persisted on the trace). Re-running the same
        # parser here keeps the panel's `until_iso` field load-bearing
        # without bloating the trace schema.
        parsed = _try_parse_iso(until_raw) if until_raw else None
        until_iso = parsed.isoformat() if parsed else None
        out.append(
            WaitEntry(
                trace_id=trace_id,
                created_at=created_at,
                reason=reason,
                until_raw=until_raw,
                until_iso=until_iso,
                trigger_source=str(r[3] or ""),
                scheduler_job_name=str(r[4]) if r[4] else None,
            )
        )

    logger.info("waits_list", count=len(out), limit=limit)
    return WaitsListResponse(waits=out)
