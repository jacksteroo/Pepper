"""FastAPI router for the reflector's pattern alerts (#41).

Exposes:

- `GET  /api/reflector/alerts`               — list alerts (filter by status)
- `POST /api/reflector/alerts/{id}/dismiss`  — operator dismisses an alert
- `POST /api/reflector/alerts/{id}/file`     — operator marks an alert as filed

Privacy posture mirrors `agent/traces/http.py`:

1. **API-key required** via `require_api_key`.
2. **Localhost bind by default** when `PEPPER_BIND_LOCALHOST_ONLY` is
   true. Alerts surface trace_ids and a one-line detector-generated
   summary; they do NOT carry raw trace text. Even so, "which turns
   are on the failure-mode list" is information that should not leave
   the box without explicit operator opt-in.
3. **No DELETE.** Status flips are the only mutation; the audit
   trail (created_at + the existing structlog line on every status
   change) is the recoverable record.
"""
from __future__ import annotations

import ipaddress
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from agent.auth import require_api_key
from agent.db import get_db
from agents.reflector import alerts as ralerts

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/reflector", tags=["reflector"])


# ── Localhost guard ──────────────────────────────────────────────────────────


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
            "reflector_alerts_non_loopback_denied",
            client_host=host,
            path=str(request.url.path),
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "/api/reflector is bound to localhost. Set "
                "PEPPER_BIND_LOCALHOST_ONLY=false AND wire session-level "
                "auth before exposing externally."
            ),
        )


# ── Response models ──────────────────────────────────────────────────────────


class AlertOut(BaseModel):
    alert_id: str
    created_at: str
    window_start: str
    window_end: str
    trace_ids: list[str]
    cluster_size: int
    confidence: float
    summary: str
    suggested_action: str
    status: str
    metadata: dict = Field(default_factory=dict)


class AlertListResponse(BaseModel):
    alerts: list[AlertOut]


class StatusUpdateResponse(BaseModel):
    ok: bool
    alert_id: str
    status: str


def _to_out(alert: ralerts.PatternAlert) -> AlertOut:
    return AlertOut(
        alert_id=alert.alert_id,
        created_at=alert.created_at.isoformat(),
        window_start=alert.window_start.isoformat(),
        window_end=alert.window_end.isoformat(),
        trace_ids=alert.trace_ids,
        cluster_size=alert.cluster_size,
        confidence=alert.confidence,
        summary=alert.summary,
        suggested_action=alert.suggested_action,
        status=alert.status,
        metadata=alert.metadata_,
    )


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/alerts", response_model=AlertListResponse)
async def list_alerts(
    request: Request,
    status: Optional[str] = Query(
        default="open",
        description=(
            "Filter by status. One of `open`, `dismissed`, `filed`, or "
            "`all` (returns the most recent across statuses)."
        ),
    ),
    limit: int = Query(default=100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    _key_hash: str = Depends(require_api_key),
) -> AlertListResponse:
    await _enforce_localhost_bind(request)

    repo = ralerts.PatternAlertRepository(db)
    if status == "all":
        # Stitch a small union — list_open + list_by_status calls are
        # cheap and we cap at `limit` total. Newest-first across all
        # buckets.
        rows = []
        for s in (ralerts.STATUS_OPEN, ralerts.STATUS_FILED, ralerts.STATUS_DISMISSED):
            rows.extend(await repo.list_by_status(s, limit=limit))
        rows.sort(key=lambda a: a.created_at, reverse=True)
        rows = rows[:limit]
    elif status in {ralerts.STATUS_OPEN, ralerts.STATUS_DISMISSED, ralerts.STATUS_FILED}:
        rows = list(await repo.list_by_status(status, limit=limit))
    else:
        raise HTTPException(
            status_code=400,
            detail=f"unknown status filter {status!r}",
        )

    return AlertListResponse(alerts=[_to_out(r) for r in rows])


@router.post("/alerts/{alert_id}/dismiss", response_model=StatusUpdateResponse)
async def dismiss_alert(
    alert_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _key_hash: str = Depends(require_api_key),
) -> StatusUpdateResponse:
    await _enforce_localhost_bind(request)
    return await _set_status(alert_id, ralerts.STATUS_DISMISSED, db)


@router.post("/alerts/{alert_id}/file", response_model=StatusUpdateResponse)
async def file_alert(
    alert_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _key_hash: str = Depends(require_api_key),
) -> StatusUpdateResponse:
    await _enforce_localhost_bind(request)
    return await _set_status(alert_id, ralerts.STATUS_FILED, db)


async def _set_status(
    alert_id: str, status: str, db: AsyncSession
) -> StatusUpdateResponse:
    repo = ralerts.PatternAlertRepository(db)
    try:
        ok = await repo.set_status(alert_id, status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="alert not found")
    return StatusUpdateResponse(ok=True, alert_id=alert_id, status=status)
