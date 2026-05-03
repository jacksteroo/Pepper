"""Trace emission glue — `TraceBuilder` accumulator + `emit_trace`.

`TraceBuilder` is the mutable accumulator that turns a turn-in-progress
into a finalized `Trace`. The pattern is:

    tb = TraceBuilder.start(input=user_message, trigger_source=...)
    # ... turn proceeds ...
    tb.set_model(model_selected, model_version, prompt_version)
    tb.add_tool_call(name=..., args=..., result_summary=..., success=...)
    # ... output produced ...
    trace = tb.finish(output=response, latency_ms=...)
    await emit_trace(trace, session_factory=..., embed_fn=...)

`emit_trace` is the fail-soft persistence wrapper used by `agent/core.py`
and `agent/scheduler.py` (the latter lands in #23). It:

1. Inserts the trace via `TraceRepository.append` inside a fresh session.
2. Schedules an async embedding job that runs off the critical path and
   updates the row via `TraceRepository.set_embedding`.
3. Logs structured metadata on success/failure, never the row payload.
4. Swallows every exception — trace persistence failure must never break
   the user's turn. This is the operative invariant for Epic 01.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

from agent.error_classifier import DataSensitivity
from agent.traces.repository import TraceRepository
from agent.traces.schema import (
    EMBEDDING_DIM,
    PROMPT_VERSION_UNVERSIONED,
    Archetype,
    Trace,
    TraceTier,
    TriggerSource,
)

logger = structlog.get_logger(__name__)

# Async embedding worker is fire-and-forget. We keep a strong ref to the
# task in a process-local set so the GC doesn't drop it mid-flight.
_BACKGROUND_EMBED_TASKS: set[asyncio.Task] = set()


def _safe_error_message(exc: BaseException) -> str:
    """Render an exception for structlog without leaking bound parameters.

    SQLAlchemy `StatementError` / `DBAPIError` `__str__` includes
    `[parameters: (...)]` which can carry RAW_PERSONAL — `Trace.input`
    or `tool_call.args` substrings would land in structlog output. We
    only return the type name plus a short prefix of the exception's
    own message (NOT the formatted SQL) so the operator can still
    distinguish failure modes.
    """
    # Prefer `exc.orig` when present — it's the underlying DBAPI error,
    # whose message rarely includes parameters. Fall back to type name only.
    orig = getattr(exc, "orig", None)
    if orig is not None:
        return f"{type(orig).__name__}: {str(orig).splitlines()[0][:120]}"
    msg = str(exc).splitlines()[0] if str(exc) else ""
    # Hard guard: anything containing "[parameters" or "[SQL" is a
    # SQLAlchemy formatted message; drop the message entirely.
    if "[parameters" in msg or "[SQL" in msg:
        return type(exc).__name__
    return f"{type(exc).__name__}: {msg[:120]}"


@dataclass
class TraceBuilder:
    """Accumulator for one turn's trace fields. NOT a Trace itself —
    `finish()` constructs the frozen `Trace` exactly once and returns it.
    """

    input: str = ""
    trigger_source: TriggerSource = TriggerSource.USER
    archetype: Archetype = Archetype.ORCHESTRATOR
    scheduler_job_name: Optional[str] = None
    data_sensitivity: DataSensitivity = DataSensitivity.LOCAL_ONLY

    assembled_context: dict[str, Any] = field(default_factory=dict)
    model_selected: str = ""
    model_version: str = ""
    prompt_version: str = PROMPT_VERSION_UNVERSIONED
    tools_called: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def start(
        cls,
        *,
        input: str,
        trigger_source: TriggerSource = TriggerSource.USER,
        archetype: Archetype = Archetype.ORCHESTRATOR,
        scheduler_job_name: Optional[str] = None,
        data_sensitivity: DataSensitivity = DataSensitivity.LOCAL_ONLY,
    ) -> TraceBuilder:
        return cls(
            input=input,
            trigger_source=trigger_source,
            archetype=archetype,
            scheduler_job_name=scheduler_job_name,
            data_sensitivity=data_sensitivity,
        )

    # ── Accumulator entry points ──────────────────────────────────────────

    def set_context(self, assembled_context: dict[str, Any]) -> None:
        if not isinstance(assembled_context, dict):
            raise TypeError("assembled_context must be a dict")
        self.assembled_context = assembled_context

    def set_assembled_context(
        self, assembled_context: dict[str, Any] | None
    ) -> None:
        """Stamp the per-turn assembler provenance for #33.

        Thin alias around :meth:`set_context` that tolerates ``None``
        (no assembler ran for this turn — e.g. early-bail paths).
        Empty / ``None`` becomes ``{}`` so the dataclass invariant holds
        and the persisted JSONB column is consistent.
        """
        if assembled_context is None:
            self.assembled_context = {}
            return
        self.set_context(assembled_context)

    def set_model(
        self,
        model_selected: str,
        *,
        model_version: str = "",
        prompt_version: str = PROMPT_VERSION_UNVERSIONED,
    ) -> None:
        # Last-write-wins so retries reflect the final state surfaced to
        # the user — same convention as `chat_turn_logger.record_llm`.
        self.model_selected = model_selected or ""
        self.model_version = model_version or ""
        self.prompt_version = prompt_version or PROMPT_VERSION_UNVERSIONED

    def add_tool_call(
        self,
        *,
        name: str,
        args: Optional[dict[str, Any]] = None,
        result_summary: str = "",
        latency_ms: int = 0,
        success: bool = True,
        error: Optional[str] = None,
    ) -> None:
        if not name:
            # The dataclass invariant rejects nameless tool calls; fail
            # at the entry point so the call site sees a clear error.
            raise ValueError("tool_call name is required")
        self.tools_called.append(
            {
                "name": name,
                "args": args or {},
                "result_summary": result_summary,
                "latency_ms": latency_ms,
                "success": success,
                "error": error,
            },
        )

    # ── Finalisation ──────────────────────────────────────────────────────

    def finish(
        self,
        *,
        output: str,
        latency_ms: int,
        user_reaction: Optional[dict[str, Any]] = None,
    ) -> Trace:
        return Trace(
            trigger_source=self.trigger_source,
            archetype=self.archetype,
            scheduler_job_name=self.scheduler_job_name,
            input=self.input,
            assembled_context=self.assembled_context,
            output=output,
            model_selected=self.model_selected,
            model_version=self.model_version,
            prompt_version=self.prompt_version,
            tools_called=self.tools_called,
            latency_ms=latency_ms,
            user_reaction=user_reaction,
            data_sensitivity=self.data_sensitivity,
            tier=TraceTier.WORKING,
        )


# ── Persistence wrapper ───────────────────────────────────────────────────────


SessionFactory = Callable[[], Any]  # async context manager yielding AsyncSession
EmbedFn = Callable[[str], Awaitable[Sequence[float]]]


async def emit_trace(
    trace: Trace,
    *,
    session_factory: SessionFactory,
    embed_fn: Optional[EmbedFn] = None,
    embed_model_version: Optional[str] = None,
) -> Optional[str]:
    """Persist a trace and (optionally) schedule an async embed job.

    Returns the persisted `trace_id` on success, `None` on any failure
    (logged as `trace_emit_failed`). Never raises — trace persistence
    must never break the user's turn.
    """
    try:
        async with session_factory() as session:
            repo = TraceRepository(session)
            persisted = await repo.append(trace)
            await session.commit()
            trace_id = persisted.trace_id
        # Structured metadata only. Never log the row's payload.
        logger.info(
            "trace_emit_ok",
            trace_id=trace_id,
            archetype=trace.archetype.value,
            trigger_source=trace.trigger_source.value,
            latency_ms=trace.latency_ms,
            tool_calls=len(trace.tools_called),
            data_sensitivity=trace.data_sensitivity.value,
        )
    except Exception as exc:
        logger.warning(
            "trace_emit_failed",
            error_type=type(exc).__name__,
            error=_safe_error_message(exc),
        )
        return None

    # Embedding worker — fire-and-forget. Failures are also fail-soft.
    if embed_fn is not None and embed_model_version:
        try:
            task = asyncio.create_task(
                _embed_worker(
                    trace_id=trace_id,
                    text=(trace.input + "\n" + trace.output)[:8000],
                    session_factory=session_factory,
                    embed_fn=embed_fn,
                    embed_model_version=embed_model_version,
                ),
            )
            _BACKGROUND_EMBED_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_EMBED_TASKS.discard)
        except RuntimeError:
            # No running event loop — synchronous test contexts. Skip.
            pass

    return trace_id


async def _embed_worker(
    *,
    trace_id: str,
    text: str,
    session_factory: SessionFactory,
    embed_fn: EmbedFn,
    embed_model_version: str,
) -> None:
    """Generate the embedding and write it back via the compactor surface."""
    try:
        vec = await embed_fn(text)
        if len(vec) != EMBEDDING_DIM:
            logger.warning(
                "trace_embed_dim_mismatch",
                trace_id=trace_id,
                got=len(vec),
                expected=EMBEDDING_DIM,
            )
            return
        async with session_factory() as session:
            repo = TraceRepository(session)
            await repo.set_embedding(
                trace_id=trace_id,
                embedding=list(vec),
                embedding_model_version=embed_model_version,
            )
            await session.commit()
        logger.info("trace_embed_ok", trace_id=trace_id)
    except Exception as exc:
        logger.warning(
            "trace_embed_failed",
            trace_id=trace_id,
            error_type=type(exc).__name__,
            error=_safe_error_message(exc),
        )
