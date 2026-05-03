"""FastAPI router for the trace inspection endpoint (#24).

Exposes:

- `GET  /api/traces`                       — paginated list view (filters + cursor)
- `GET  /api/traces/{id}`                  — full trace including assembled_context
- `POST /api/traces/{id}/find_similar`     — embedding-nearest neighbours
- `POST /api/traces/{id}/rerender-prompt`  — re-runs the assembler against this
  trace's input (#34) so a maintainer can verify that fixes to the assembler
  change the right thing. The re-render result is NEVER logged or persisted —
  it lives in the response body only, returned to the in-browser inspector.

**Privacy posture**

This endpoint surfaces the most sensitive HTTP route in the system —
every trace row contains the full input and output of an agent turn,
the assembled context, and tool-call args. Three layers of defence:

1. **API-key required.** Inherits the existing `require_api_key`
   header check used by every other authenticated endpoint.
2. **Localhost bind by default.** When `PEPPER_BIND_LOCALHOST_ONLY`
   is true (the default), every request whose `client.host` is not
   loopback is rejected with 403, even if it carries a valid key.
   The web UI talks to FastAPI over loopback; turning this off is an
   explicit, documented opt-in for non-localhost deployments and
   requires session-level auth (deferred — see GUARDRAILS.md).
3. **Audit log on every read.** Every call records to a `mcp_audit`-
   shaped audit entry so the operator can answer "who looked at
   what, when" without stepping through structlog. Logged on success
   AND on permission-denied.

This module is wired into `agent/main.py` via `include_router`. It
intentionally does NOT expose a `/traces/{id}` `DELETE` route —
mirrors ADR-0005's append-only invariant at the API layer.
"""
from __future__ import annotations

import hashlib
import ipaddress
from datetime import datetime
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from agent.auth import require_api_key
from agent.db import get_db
from agent.error_classifier import DataSensitivity
from agent.traces import (
    EMBEDDING_DIM,
    Archetype,
    Trace,
    TraceRepository,
    TraceTier,
    TriggerSource,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/traces", tags=["traces"])


# ── Localhost guard ───────────────────────────────────────────────────────────


def _client_is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if not host:
        return False
    # ``localhost`` is a hostname, not an IP — keep the string fallback
    # so dev hosts that report it stay on the allow-list. The existing
    # ``127.x`` branch is also kept because some clients return that
    # without a fully-formed IP literal.
    if host in {"localhost"} or host.startswith("127."):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def _enforce_localhost_bind(request: Request) -> None:
    """Reject non-loopback requests when PEPPER_BIND_LOCALHOST_ONLY is on."""
    from agent.config import settings

    bind_localhost = getattr(settings, "PEPPER_BIND_LOCALHOST_ONLY", True)
    if bind_localhost and not _client_is_loopback(request):
        host = request.client.host if request.client else "<unknown>"
        logger.warning(
            "traces_endpoint_non_loopback_denied",
            client_host=host,
            path=str(request.url.path),
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "/traces is bound to localhost. Set PEPPER_BIND_LOCALHOST_ONLY=false "
                "AND wire session-level auth before exposing externally."
            ),
        )


# ── Audit log ─────────────────────────────────────────────────────────────────


async def _audit_read(
    *,
    actor_key_hash: str,
    action: str,
    detail: dict[str, Any],
    request: Request,
) -> None:
    """Append a one-line audit record for every /traces read.

    Reuses `agent.mcp_audit.log_mcp_call` shape because that's the
    existing "who-touched-what" audit pipeline. We log structured
    metadata only — never the row contents.
    """
    try:
        from agent.mcp_audit import audit_logger as audit

        audit.info(
            "traces_endpoint_read",
            actor=actor_key_hash[:12],
            action=action,
            client_host=(request.client.host if request.client else "<unknown>"),
            **detail,
        )
    except Exception:
        # Audit failure must never block a read — same posture as the
        # routing event audit log. Swallow.
        pass


# ── Response models ───────────────────────────────────────────────────────────


class TraceSummary(BaseModel):
    trace_id: str
    created_at: datetime
    trigger_source: str
    archetype: str
    model_selected: str
    latency_ms: int
    data_sensitivity: str
    tier: str
    scheduler_job_name: Optional[str] = None
    # #34 — capability_block_version is projected onto the list view so the
    # inspector can find the prior trace with a different version without
    # issuing one detail fetch per summary. Cheap (12-char string) projected
    # via JSONB ``->>`` operator at the repository layer.
    capability_block_version: Optional[str] = None


class TraceDetail(TraceSummary):
    input: str
    output: str
    model_version: str
    prompt_version: str
    assembled_context: dict[str, Any]
    tools_called: list[dict[str, Any]]
    user_reaction: Optional[dict[str, Any]] = None
    embedding_model_version: Optional[str] = None
    has_embedding: bool
    # Selector → human-readable reason map (#34). Computed off the stored
    # provenance. Empty {} when assembled_context is empty (e.g. legacy
    # rows from before #33 landed).
    decision_reasons: dict[str, str] = Field(default_factory=dict)


class TraceListResponse(BaseModel):
    traces: list[TraceSummary]
    next_cursor: Optional[str] = None


class FindSimilarRequest(BaseModel):
    embedding: list[float] = Field(..., min_length=EMBEDDING_DIM, max_length=EMBEDDING_DIM)
    limit: int = Field(10, ge=1, le=100)


class FindSimilarItem(BaseModel):
    trace_id: str
    distance: float


class FindSimilarResponse(BaseModel):
    matches: list[FindSimilarItem]


# #34 — re-render endpoint response shape. The body is intentionally
# self-contained so the inspector UI can diff against the original
# trace without any further round-trips. Privacy: the rendered prompt
# IS RAW_PERSONAL — it includes the user's life context and (via the
# assembler) any cached secrets in the system prompt. This payload
# crosses the loopback boundary only; routes inherit the localhost
# bind enforced above.
class RerenderPromptResponse(BaseModel):
    trace_id: str
    prompt: str
    prompt_hash: str
    provenance: dict[str, Any]
    original_provenance: dict[str, Any]
    matches_original: bool
    notes: list[str] = Field(default_factory=list)


# Dependency: yield the live ContextAssembler. Wired through the FastAPI
# app state so tests can override without touching the module global.
def get_assembler() -> Any:
    """Return the live ``ContextAssembler`` from the running PepperCore.

    Importing inside the function avoids a circular import (``agent.main``
    imports this module, and the assembler is owned by PepperCore which
    is constructed in ``main.lifespan``).
    """
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
    asm = getattr(pepper_obj, "assembler", None)
    if asm is None:
        raise HTTPException(
            status_code=503,
            detail="context assembler not available on pepper instance",
        )
    return asm


# ── Mapping helpers ───────────────────────────────────────────────────────────


def _to_summary(t: Trace) -> TraceSummary:
    cap_version = (t.assembled_context or {}).get("capability_block_version")
    return TraceSummary(
        trace_id=t.trace_id,
        created_at=t.created_at,
        trigger_source=t.trigger_source.value,
        archetype=t.archetype.value,
        model_selected=t.model_selected,
        latency_ms=t.latency_ms,
        data_sensitivity=t.data_sensitivity.value,
        tier=t.tier.value,
        scheduler_job_name=t.scheduler_job_name,
        capability_block_version=str(cap_version) if cap_version else None,
    )


def _decision_reasons_from_stored(
    assembled_context: dict[str, Any],
) -> dict[str, str]:
    """Compute ``selector_name -> human reason`` from a stored provenance dict.

    Delegates to :func:`agent.context.annotate_from_provenance` — the
    public API for rendering reasons against a JSON-serialized provenance
    shape. We pass the persisted ``selectors`` sub-dict (per #33) and let
    decisions.py own the explainer table.
    """
    selectors = (assembled_context or {}).get("selectors") or {}
    try:
        # Local import keeps the http module loadable without the context
        # subsystem when tests stub things out.
        from agent.context.decisions import annotate_from_provenance
    except Exception:
        return {}
    if not isinstance(selectors, dict):
        return {}
    return annotate_from_provenance(selectors)


def _to_detail(t: Trace) -> TraceDetail:
    return TraceDetail(
        trace_id=t.trace_id,
        created_at=t.created_at,
        trigger_source=t.trigger_source.value,
        archetype=t.archetype.value,
        model_selected=t.model_selected,
        model_version=t.model_version,
        prompt_version=t.prompt_version,
        latency_ms=t.latency_ms,
        data_sensitivity=t.data_sensitivity.value,
        tier=t.tier.value,
        scheduler_job_name=t.scheduler_job_name,
        input=t.input,
        output=t.output,
        assembled_context=t.assembled_context,
        tools_called=t.tools_called,
        user_reaction=t.user_reaction,
        embedding_model_version=t.embedding_model_version,
        has_embedding=t.embedding is not None,
        decision_reasons=_decision_reasons_from_stored(t.assembled_context),
    )


def _parse_cursor(raw: Optional[str]) -> Optional[tuple[datetime, str]]:
    if not raw:
        return None
    try:
        ts_str, trace_id = raw.split("|", 1)
        return (datetime.fromisoformat(ts_str), trace_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid cursor: {exc}") from exc


def _format_cursor(ts: datetime, trace_id: str) -> str:
    return f"{ts.isoformat()}|{trace_id}"


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=TraceListResponse,
    dependencies=[Depends(require_api_key)],
)
async def list_traces(
    request: Request,
    archetype: Optional[str] = Query(None),
    trigger_source: Optional[str] = Query(None),
    model_selected: Optional[str] = Query(None),
    data_sensitivity: Optional[str] = Query(None),
    tier: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    contains_text: Optional[str] = Query(None, max_length=512),
    cursor: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
) -> TraceListResponse:
    """List view — projected (no jsonb / embedding loaded)."""
    await _enforce_localhost_bind(request)

    repo = TraceRepository(session)
    traces = await repo.query(
        archetype=Archetype(archetype) if archetype else None,
        trigger_source=TriggerSource(trigger_source) if trigger_source else None,
        model_selected=model_selected,
        data_sensitivity=DataSensitivity(data_sensitivity) if data_sensitivity else None,
        tier=TraceTier(tier) if tier else None,
        since=since,
        until=until,
        contains_text=contains_text,
        cursor=_parse_cursor(cursor),
        limit=limit,
        with_payload=False,
    )

    summaries = [_to_summary(t) for t in traces]
    next_cursor = (
        _format_cursor(traces[-1].created_at, traces[-1].trace_id)
        if len(traces) == limit
        else None
    )

    api_key = request.headers.get("x-api-key", "")
    await _audit_read(
        actor_key_hash=api_key,
        action="list_traces",
        detail={"returned": len(summaries), "limit": limit},
        request=request,
    )
    return TraceListResponse(traces=summaries, next_cursor=next_cursor)


@router.get(
    "/{trace_id}",
    response_model=TraceDetail,
    dependencies=[Depends(require_api_key)],
)
async def get_trace_detail(
    trace_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> TraceDetail:
    """Detail view — full row including assembled_context + tools_called."""
    await _enforce_localhost_bind(request)

    repo = TraceRepository(session)
    try:
        trace = await repo.get_by_id(trace_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid trace_id: {exc}") from exc
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")

    api_key = request.headers.get("x-api-key", "")
    await _audit_read(
        actor_key_hash=api_key,
        action="get_trace_detail",
        detail={"trace_id": trace_id},
        request=request,
    )
    return _to_detail(trace)


@router.post(
    "/{trace_id}/find_similar",
    response_model=FindSimilarResponse,
    dependencies=[Depends(require_api_key)],
)
async def find_similar(
    trace_id: str,
    body: FindSimilarRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> FindSimilarResponse:
    """Embedding nearest-neighbours, ID-only.

    Body carries the embedding so the UI can supply a pre-computed
    vector (e.g. from the trace under inspection). Callers re-fetch
    detail rows via `GET /traces/{id}`.
    """
    await _enforce_localhost_bind(request)

    repo = TraceRepository(session)
    try:
        matches = await repo.find_similar(body.embedding, limit=body.limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    api_key = request.headers.get("x-api-key", "")
    await _audit_read(
        actor_key_hash=api_key,
        action="find_similar",
        detail={"anchor": trace_id, "matches": len(matches)},
        request=request,
    )
    return FindSimilarResponse(
        matches=[FindSimilarItem(trace_id=tid, distance=dist) for tid, dist in matches],
    )


# Fields in AssembledContext.provenance that legitimately differ across
# re-renders even when no code has changed:
#   - ``last_n_turns`` — depends on the live working-memory at the time
#     of re-render, which evolves with every new turn.
#   - ``selectors.last_n_turns.*`` — same reason.
#   - ``selectors.life_context.checksum`` may differ if the file was
#     edited; we include the file modification in the comparison.
# The structural-match check excludes these explicitly so a stable
# assembler against an unchanged code base reports ``matches_original=
# True``. When the assembler IS changed, ``capability_block_version``
# or ``life_context_sections_used`` will diverge and the flag flips.
_PROVENANCE_VOLATILE_TOP_LEVEL_KEYS: frozenset[str] = frozenset({
    "last_n_turns",
})

# Per-selector volatile keys. Same rationale.
_PROVENANCE_VOLATILE_SELECTOR_KEYS: dict[str, frozenset[str]] = {
    # ``content`` and ``role_counts`` track the live history shape and
    # drift across re-renders even when the assembler hasn't changed.
    "last_n_turns": frozenset(
        {"last_n_turns", "n_messages", "content", "role_counts"}
    ),
}


def _strip_volatile(provenance: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``provenance`` with known-volatile fields removed.

    Used by the re-render endpoint to compute a structural-equality
    signal that's robust to live-history drift between the original
    turn and the re-render.
    """
    filtered: dict[str, Any] = {}
    for k, v in provenance.items():
        if k in _PROVENANCE_VOLATILE_TOP_LEVEL_KEYS:
            continue
        if k == "selectors" and isinstance(v, dict):
            scrubbed_selectors: dict[str, Any] = {}
            for sel_name, sel_prov in v.items():
                if not isinstance(sel_prov, dict):
                    scrubbed_selectors[sel_name] = sel_prov
                    continue
                drop = _PROVENANCE_VOLATILE_SELECTOR_KEYS.get(
                    sel_name, frozenset()
                )
                scrubbed_selectors[sel_name] = {
                    sk: sv for sk, sv in sel_prov.items() if sk not in drop
                }
            filtered[k] = scrubbed_selectors
            continue
        filtered[k] = v
    return filtered


@router.post(
    "/{trace_id}/rerender-prompt",
    response_model=RerenderPromptResponse,
    dependencies=[Depends(require_api_key)],
)
async def rerender_prompt(
    trace_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    assembler: Any = Depends(get_assembler),
) -> RerenderPromptResponse:
    """Re-run the live assembler against this trace's stored input (#34).

    Why: when we change the assembler we want a way to say "would this
    have produced the same prompt for this past turn?". For unchanged
    code this should be a structural match; for code changes the diff
    is the answer.

    **Why POST (not GET) for a read-only re-render?** The rendered prompt
    is RAW_PERSONAL — it embeds the full life-context document and any
    secrets the system prompt carries. A GET would put identifying
    parameters in URL query strings (none today, but the pattern is
    fragile), and any future variant that accepts overrides would push
    that material into URL query strings, server access logs, browser
    history, and Referer headers. POST keeps the inputs in the request
    body and the rendered prompt in the response body only. The audit
    log records the request, never the body.

    Limitations (returned in ``notes`` so the UI can render them):

    1. **Proactive contexts are not stored.** The original Turn carried
       memory_context / web_context / calendar_context / etc. — those
       strings are not on the trace row. The re-render runs with empty
       proactive contexts, so the resulting prompt is shorter than the
       original. Provenance from selectors that DO read live state
       (life_context, capability_block) is still meaningful.
    2. **History is live.** ``last_n_turns`` reflects working memory at
       re-render time, which is later than the original turn. The
       structural-match check excludes history-dependent fields so an
       unchanged assembler against unchanged life-context reports
       ``matches_original=True``.
    3. **Skill match is currently always None per #33.** The skills
       index is exposed via progressive disclosure; no per-turn match.

    Privacy: the response body crosses the loopback boundary only and
    is **never logged**. The audit-log entry below records that a
    re-render happened — it does NOT capture the rendered text.
    """
    await _enforce_localhost_bind(request)

    # Defer the import of agent.context.types so this module stays
    # cheap to load — keeps test_traces_http.py's existing in-memory
    # mocks unaffected.
    from agent.context.types import Turn

    repo = TraceRepository(session)
    try:
        trace = await repo.get_by_id(trace_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid trace_id: {exc}") from exc
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")

    notes: list[str] = []
    if not trace.input:
        notes.append("trace input is empty; re-render uses an empty user_message")
    notes.append(
        "proactive contexts (memory/web/calendar/email/imessage/whatsapp/slack) "
        "are not stored on the trace; re-render uses empty strings for these"
    )
    notes.append(
        "history reflects working memory at re-render time, not at the original "
        "turn; structural-match check ignores history-dependent fields"
    )

    # Build a minimal Turn. We deliberately do NOT populate proactive
    # contexts because they aren't stored — the alternative would be
    # silently materializing a different prompt and pretending it's
    # the same one. Better to be honest in `notes` and let the UI diff.
    turn = Turn(
        user_message=trace.input or "",
        # No channel header — we don't store channel on the trace today.
        channel="",
        isolated=False,
        history_limit=20,
        memory_context="",
        memory_records=[],
        web_context="",
        routing_context="",
        calendar_context="",
        email_context="",
        imessage_context="",
        whatsapp_context="",
        slack_context="",
        include_skills_index=True,
        extra_system_suffix="",
        # Pin "now" to the trace's created_at so the time header is
        # deterministic across re-renders. Without this, every call
        # would differ in the rendered timestamp byte and the prompt
        # hash would never match.
        now_override=trace.created_at,
    )

    try:
        assembled = assembler.assemble(turn)
    except Exception as exc:
        logger.warning(
            "rerender_prompt_failed",
            trace_id=trace_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=500,
            detail=f"assembler failed during re-render: {exc}",
        ) from exc

    prompt = assembled.render_prompt()
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    new_provenance = assembled.provenance
    original_provenance = dict(trace.assembled_context or {})

    matches_original = (
        _strip_volatile(new_provenance) == _strip_volatile(original_provenance)
    )

    api_key = request.headers.get("x-api-key", "")
    # Audit: log that a re-render happened, NOT the result. The rendered
    # prompt and provenance live in the response body only — they do
    # not enter structlog, the audit log, or the traces table. This is
    # an explicit privacy contract for #34's inspector panel.
    await _audit_read(
        actor_key_hash=api_key,
        action="rerender_prompt",
        detail={
            "trace_id": trace_id,
            "matches_original": matches_original,
            "prompt_hash": prompt_hash,
        },
        request=request,
    )

    return RerenderPromptResponse(
        trace_id=trace_id,
        prompt=prompt,
        prompt_hash=prompt_hash,
        provenance=new_provenance,
        original_provenance=original_provenance,
        matches_original=matches_original,
        notes=notes,
    )
