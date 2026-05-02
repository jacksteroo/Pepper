"""Lightweight per-turn JSONL logger feeding the semantic-router migration.

Phase 0 Task 2 of docs/SEMANTIC_ROUTER_MIGRATION.md. Every chat turn writes
one row to logs/chat_turns/<date>.jsonl. File-based so it survives DB
migrations and is easy to grep/replay.

Dual-writer durability (Phase 1 Task 4): this JSONL is the plaintext
source of truth. The routing_events table is a queryable copy populated
by ``PepperCore._log_routing_event`` in a background task. If the DB
writer fails, the JSONL row still persists and ``agent.router_backfill``
reconciles missed rows by replaying these files into routing_events.
``write_turn`` is therefore intentionally synchronous, called from a
``finally`` block in ``PepperCore.chat``, and swallows every exception
internally — never raising past the caller.

Privacy: writes stay on-disk, never leave the machine.
"""

from __future__ import annotations

import json
import os
import threading
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_LOG_DIR = _REPO_ROOT / "logs" / "chat_turns"

_CURRENT_TRACE: ContextVar[dict | None] = ContextVar("chat_turn_trace", default=None)

# JSONL writes are append-only short lines; a process-local lock is enough
# for concurrent asyncio turns. Cross-process safety is not required because
# Pepper runs as a single container.
_WRITE_LOCK = threading.Lock()


def start_turn() -> dict[str, Any]:
    """Initialise a fresh trace for the current async context.

    Returns the trace dict so the caller can hold a reference even if the
    ContextVar is later reset by a nested task.
    """
    trace: dict[str, Any] = {
        "model": None,
        "tool_calls": [],
        "routing": None,
        "assembled_context": None,
    }
    _CURRENT_TRACE.set(trace)
    return trace


def get_trace() -> dict[str, Any] | None:
    """Return the active turn trace dict, or None if no turn is in flight."""
    return _CURRENT_TRACE.get()


def record_routing(
    *,
    intent: str | None,
    sources: list[str] | None,
    confidence: float | None,
) -> None:
    """Stamp the active turn's trace with the regex router's primary decision.

    Phase 1 Task 2: feeds the routing_events DB row written at end-of-turn.
    """
    trace = _CURRENT_TRACE.get()
    if trace is None:
        return
    trace["routing"] = {
        "intent": intent,
        "sources": list(sources) if sources else None,
        "confidence": confidence,
    }


def record_assembled_context(provenance: dict[str, Any] | None) -> None:
    """Stamp the active turn's trace with the assembler's provenance map.

    Called from ``PepperCore._chat_impl`` right after
    ``ContextAssembler.assemble`` returns. The provenance dict (one entry per
    selector) is opaque here — we just attach it to the trace ContextVar so
    the trace builder in ``PepperCore.chat`` can pick it up alongside the
    routing + LLM data it already reads. Issue #33 (E3) will read this off
    the trace snapshot to attach assembled-context provenance to the
    persisted ``traces`` row.

    No-op when no turn is in flight.
    """
    if provenance is None:
        return
    trace = _CURRENT_TRACE.get()
    if trace is None:
        return
    trace["assembled_context"] = provenance


def record_llm(model: str | None, tool_calls: list[dict] | None) -> None:
    """Stamp the active turn's trace with the model used and any tool calls.

    Safe to call multiple times — last write wins for ``model`` and
    ``tool_calls`` is replaced (not appended) so retries reflect the final
    state surfaced to the user.
    """
    trace = _CURRENT_TRACE.get()
    if trace is None:
        return
    if model is not None:
        trace["model"] = model
    trace["tool_calls"] = _summarize_tool_calls(tool_calls or [])


def _summarize_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """Reduce ollama tool_call dicts to {name, arguments} with truncated args."""
    summarized: list[dict] = []
    for call in tool_calls:
        fn = (call or {}).get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        args = fn.get("arguments")
        if isinstance(args, dict):
            args_repr: Any = {k: _truncate(v) for k, v in args.items()}
        else:
            args_repr = _truncate(args)
        summarized.append({"name": name, "arguments": args_repr})
    return summarized


def _truncate(value: Any, max_chars: int = 500) -> Any:
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + "…"
    return value


def write_turn(
    *,
    query: str,
    response: str,
    latency_ms: int,
    session_id: str,
    channel: str,
    log_dir: Path | None = None,
) -> datetime:
    """Append a JSONL row for this turn. Best-effort; never raises.

    Returns the UTC timestamp stamped on the row, so the caller's
    inline DB writer can use the same value — keeps JSONL and
    routing_events timestamps identical, which router_backfill relies
    on for exact-match deduplication.
    """
    stamped_at = datetime.now(timezone.utc)
    trace = _CURRENT_TRACE.get() or {"model": None, "tool_calls": []}
    row = {
        "timestamp": stamped_at.isoformat(),
        "session_id": session_id,
        "channel": channel,
        "query": query,
        "response": response,
        "tool_calls": trace.get("tool_calls", []),
        "latency_ms": latency_ms,
        "model": trace.get("model"),
    }
    target_dir = log_dir or _DEFAULT_LOG_DIR
    try:
        os.makedirs(target_dir, exist_ok=True)
        path = target_dir / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with _WRITE_LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception as exc:
        logger.warning("chat_turn_log_write_failed", error=str(exc))
    return stamped_at
