"""FastAPI router for the wait-feedback endpoint — Issue #56.

Exposes:

  GET  /api/waits          — recent wait-traces (list, newest first)
  POST /api/wait-feedback  — record user thumbs-up/down on a wait

Privacy posture: wait-trace IDs are not sensitive; reasons ARE personal.
The GET list does not expose full trace content, only reason/until/timestamp
from the wait tool args. Same localhost-bind guard as /traces.
"""
from __future__ import annotations

import ipaddress
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from agent.auth import require_api_key
from agent.db import get_db
from agent.traces import TraceRepository

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["waits"])


# ── Localhost guard (mirrors traces/http.py) ──────────────────────────────────


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
        raise HTTPException(
            status_code=403,
            detail="/waits is bound to localhost.",
        )


# ── Response models ───────────────────────────────────────────────────────────


class WaitSummary(BaseModel):
    trace_id: str
    created_at: str
    reason: str
    until: Optional[str] = None
    # Aggregated user feedback if any exists.
    user_signal: Optional[str] = None  # "correct" | "incorrect" | None


class WaitListResponse(BaseModel):
    waits: list[WaitSummary]
    total: int


class WaitFeedbackRequest(BaseModel):
    wait_trace_id: str
    user_signal: str  # "correct" | "incorrect"
    notes: str = ""


class WaitFeedbackResponse(BaseModel):
    ok: bool
    wait_trace_id: str
    user_signal: str


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get(
    "/waits",
    response_model=WaitListResponse,
    dependencies=[Depends(require_api_key)],
)
async def list_waits(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
) -> WaitListResponse:
    """List recent wait-traces, newest first.

    Only exposes wait tool args (reason, until) plus the trace timestamp —
    not full trace content. The `is_wait` marker in assembled_context is
    the selector.
    """
    await _enforce_localhost_bind(request)

    repo = TraceRepository(session)
    # Query all recent traces, then filter by is_wait marker.
    # We need assembled_context for the is_wait flag; use with_payload=True
    # but cap the limit generously on the server side to avoid huge scans.
    candidates = await repo.query(limit=500, with_payload=True)

    wait_traces = [
        t for t in candidates
        if (t.assembled_context or {}).get("is_wait") is True
    ][:limit]

    # Load existing user feedback to annotate the list.
    try:
        from agents.reflector.wait_evaluator import load_wait_feedback
        all_feedback = load_wait_feedback()
        user_signals: dict[str, str] = {}
        for rec in all_feedback:
            if rec.get("signal_type") == "user_thumbs":
                tid = rec.get("wait_trace_id", "")
                # Last write wins.
                user_signals[tid] = "correct" if rec.get("signal_value") else "incorrect"
    except Exception:
        user_signals = {}

    summaries: list[WaitSummary] = []
    for t in wait_traces:
        # Extract wait args from tools_called.
        wait_args: dict = {}
        for call in (t.tools_called or []):
            if isinstance(call, dict) and call.get("name") == "wait":
                wait_args = call.get("args") or {}
                break
        summaries.append(
            WaitSummary(
                trace_id=t.trace_id,
                created_at=t.created_at.isoformat(),
                reason=wait_args.get("reason", ""),
                until=wait_args.get("until"),
                user_signal=user_signals.get(t.trace_id),
            )
        )

    return WaitListResponse(waits=summaries, total=len(summaries))


@router.post(
    "/wait-feedback",
    response_model=WaitFeedbackResponse,
    dependencies=[Depends(require_api_key)],
)
async def record_wait_feedback(
    body: WaitFeedbackRequest,
    request: Request,
) -> WaitFeedbackResponse:
    """Record explicit user thumbs-up/down on a wait-trace.

    `user_signal` must be "correct" or "incorrect". This is persisted to
    data/wait_feedback.json and read by the weekly reflector rollup.
    """
    await _enforce_localhost_bind(request)

    if body.user_signal not in ("correct", "incorrect"):
        raise HTTPException(
            status_code=400,
            detail="user_signal must be 'correct' or 'incorrect'",
        )

    try:
        from agents.reflector.wait_evaluator import record_user_thumbs

        record_user_thumbs(
            wait_trace_id=body.wait_trace_id,
            user_signal=body.user_signal,
            notes=body.notes,
        )
    except Exception as exc:
        logger.warning(
            "wait_feedback_record_failed",
            trace_id=body.wait_trace_id,
            error=str(exc)[:200],
        )
        raise HTTPException(status_code=500, detail="failed to record feedback") from exc

    return WaitFeedbackResponse(
        ok=True,
        wait_trace_id=body.wait_trace_id,
        user_signal=body.user_signal,
    )
