"""FastAPI route for fetching a single memory row by id (#34).

Exposes:

- ``GET /api/memories/{memory_id}`` — returns the memory's full content +
  metadata, gated by ``require_api_key`` and (when the project setting is
  on) loopback-only enforcement.

**Privacy posture**

Memory rows can contain raw personal content. The route inherits the
same defence-in-depth as the traces endpoints:

1. **API-key required.**
2. **Localhost bind by default** (delegated to the same helper used by
   ``agent/traces/http.py``).
3. **Audit log on every read.** Records the memory id and action only —
   never the body.

Why this exists: the trace inspector (#34) lists per-turn memory IDs +
scores from provenance. Operators want to expand a row and read the
underlying content without dropping into the database directly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from agent.auth import require_api_key

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/memories", tags=["memories"])


class MemoryDetail(BaseModel):
    id: int
    type: str
    content: str
    summary: Optional[str] = None
    importance_score: float
    created_at: datetime
    accessed_at: Optional[datetime] = None
    has_embedding: bool


async def _audit_read(
    *,
    actor_key_hash: str,
    action: str,
    detail: dict[str, Any],
    request: Request,
) -> None:
    """Append a one-line audit record. Same shape as traces/http.py.

    NEVER logs the memory content — only metadata (id + action).
    """
    try:
        from agent.mcp_audit import audit_logger as audit

        audit.info(
            "memories_endpoint_read",
            actor=actor_key_hash[:12],
            action=action,
            client_host=(request.client.host if request.client else "<unknown>"),
            **detail,
        )
    except Exception:
        # Audit failure must never block a read — same posture as the
        # rest of the read paths. Swallow.
        pass


@router.get(
    "/{memory_id}",
    response_model=MemoryDetail,
    dependencies=[Depends(require_api_key)],
)
async def get_memory_detail(
    memory_id: int,
    request: Request,
) -> MemoryDetail:
    """Return the full memory row + metadata for ``memory_id``.

    Used by the trace inspector to expand a memory row and show its
    content. The response body is RAW_PERSONAL — the route inherits
    localhost-bind enforcement from the traces helper.
    """
    # Reuse the loopback enforcement helper from traces/http.py so the two
    # endpoints share a single policy implementation.
    from agent.traces.http import _enforce_localhost_bind

    await _enforce_localhost_bind(request)

    try:
        from agent.main import _get_pepper
    except Exception as exc:  # pragma: no cover - import guard
        raise HTTPException(
            status_code=503,
            detail=f"pepper not ready: {exc}",
        ) from exc

    pepper_obj = _get_pepper()
    if pepper_obj is None:
        raise HTTPException(status_code=503, detail="pepper not initialized")
    memory_manager = getattr(pepper_obj, "memory", None)
    if memory_manager is None:
        raise HTTPException(
            status_code=503,
            detail="memory manager not available on pepper instance",
        )

    if not hasattr(memory_manager, "get_by_id"):
        raise HTTPException(
            status_code=501,
            detail="memory manager does not implement get_by_id",
        )

    row = await memory_manager.get_by_id(memory_id)
    if row is None:
        raise HTTPException(status_code=404, detail="memory not found")

    api_key = request.headers.get("x-api-key", "")
    await _audit_read(
        actor_key_hash=api_key,
        action="get_memory_detail",
        # Privacy: log id only — never log the content body.
        detail={"memory_id": memory_id},
        request=request,
    )

    return MemoryDetail(
        id=row.id,
        type=row.type,
        content=row.content,
        summary=row.summary,
        importance_score=float(row.importance_score or 0.0),
        created_at=row.created_at,
        accessed_at=row.accessed_at,
        has_embedding=row.embedding is not None,
    )
