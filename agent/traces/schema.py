"""Canonical Python contract for the `traces` table.

This file is the in-code mirror of `docs/trace-schema.md`. Any change here
must be reflected there (and in the migration #20 once that lands).

Scope of #18 is the schema only — the SQLAlchemy model, repository, and
emitter all land in later sub-issues. This module intentionally does not
import from `agent.db` so it can be loaded standalone (e.g. by the
optimizer, by tests that fake the store).

The dataclass is `frozen=True` so an in-process holder cannot silently
mutate a constructed `Trace` between `start()` and persistence. The
mutable accumulator that turns a turn-in-progress into a finalized
`Trace` lands as `TraceBuilder` in #22.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from agent.error_classifier import DataSensitivity

# Embedding dimension is locked to qwen3-embedding:0.6b's output. Mismatched
# dimensions would silently store and explode at the pgvector layer; the
# dataclass refuses them up front. See ADR-0005 §"Storage decision".
EMBEDDING_DIM: int = 1024
EMBEDDING_MODEL_DEFAULT: str = "qwen3-embedding:0.6b"

# Sentinel for prompt version when #48's prompt-versioning has not yet
# wrapped a code path. #22 callers may set this freely; the optimizer
# (#46) treats `unversioned` as "ineligible for prompt-level rollups".
PROMPT_VERSION_UNVERSIONED: str = "unversioned"


class TriggerSource(str, Enum):
    """What initiated the turn this trace records."""

    USER = "user"
    SCHEDULER = "scheduler"
    AGENT = "agent"


class Archetype(str, Enum):
    """Which agent process produced this trace.

    Maps 1:1 to the four processes named in ADR-0004.
    """

    ORCHESTRATOR = "orchestrator"
    REFLECTOR = "reflector"
    MONITOR = "monitor"
    RESEARCHER = "researcher"


class TraceTier(str, Enum):
    """Compression tier (see #21).

    New rows are created in WORKING. The nightly job from #21 is the only
    writer that may transition a row from WORKING → RECALL → ARCHIVAL —
    one of the documented UPDATE carve-outs in ADR-0005 §Mutability.
    """

    WORKING = "working"
    RECALL = "recall"
    ARCHIVAL = "archival"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_trace_id() -> str:
    return str(uuid.uuid4())


# Keys that every element of `tools_called` must carry. The full shape
# lives in `docs/trace-schema.md`; this is the minimum #20's GIN index
# can rely on.
_TOOLS_CALLED_REQUIRED_KEYS = frozenset({"name"})


@dataclass(frozen=True)
class Trace:
    """One turn of agent behavior — input through outcome.

    Field semantics, nullability, and RAW_PERSONAL annotations live in
    `docs/trace-schema.md`. This dataclass is the contract that #20's
    SQLAlchemy model and #22's `TraceBuilder` materialize against.

    Frozen: a constructed `Trace` is the row that will be persisted.
    Pre-persist accumulation happens in `TraceBuilder` (#22), which calls
    `Trace(...)` exactly once at `finish()`.
    """

    # Identity & timing
    trace_id: str = field(default_factory=_new_trace_id)
    created_at: datetime = field(default_factory=_utcnow)

    # Provenance
    trigger_source: TriggerSource = TriggerSource.USER
    archetype: Archetype = Archetype.ORCHESTRATOR
    scheduler_job_name: Optional[str] = None

    # Conversation payload (RAW_PERSONAL — redacted from __repr__)
    input: str = ""
    assembled_context: dict[str, Any] = field(default_factory=dict)
    output: str = ""

    # Model & prompt
    model_selected: str = ""
    model_version: str = ""
    prompt_version: str = PROMPT_VERSION_UNVERSIONED

    # Tool calls (RAW_PERSONAL inside .args / .result_summary — redacted from __repr__)
    tools_called: list[dict[str, Any]] = field(default_factory=list)

    # Outcome
    latency_ms: int = 0
    user_reaction: Optional[dict[str, Any]] = None
    data_sensitivity: DataSensitivity = DataSensitivity.LOCAL_ONLY

    # Embedding (lazy-populated; nullable by design — see #21 / #22)
    embedding: Optional[list[float]] = None
    embedding_model_version: Optional[str] = None

    # Compression tier — only the #21 nightly job advances this.
    tier: TraceTier = TraceTier.WORKING

    def __post_init__(self) -> None:
        # ── Embedding invariants ──
        if self.embedding is not None:
            if not self.embedding_model_version:
                raise ValueError(
                    "embedding_model_version is required when embedding is set",
                )
            if len(self.embedding) != EMBEDDING_DIM:
                raise ValueError(
                    f"embedding dimension must be {EMBEDDING_DIM}, "
                    f"got {len(self.embedding)}",
                )

        # ── Provenance invariants ──
        if (
            self.trigger_source != TriggerSource.SCHEDULER
            and self.scheduler_job_name is not None
        ):
            raise ValueError(
                "scheduler_job_name set on a non-scheduler trace",
            )

        # ── Shape invariants on the open jsonb columns ──
        if not isinstance(self.assembled_context, dict):
            raise TypeError(
                f"assembled_context must be dict, got {type(self.assembled_context).__name__}",
            )
        if not isinstance(self.tools_called, list):
            raise TypeError(
                f"tools_called must be list, got {type(self.tools_called).__name__}",
            )
        for i, tc in enumerate(self.tools_called):
            if not isinstance(tc, dict):
                raise TypeError(
                    f"tools_called[{i}] must be dict, got {type(tc).__name__}",
                )
            missing = _TOOLS_CALLED_REQUIRED_KEYS - tc.keys()
            if missing:
                raise ValueError(
                    f"tools_called[{i}] missing required keys: {sorted(missing)}",
                )

        # ── JSON serialisability — the row is going to jsonb, fail at the boundary
        # rather than deep inside the SQLAlchemy emitter. ──
        try:
            json.dumps(self.assembled_context, default=str)
            json.dumps(self.tools_called, default=str)
            if self.user_reaction is not None:
                json.dumps(self.user_reaction, default=str)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"trace jsonb columns must be JSON-serialisable: {exc}",
            ) from exc

    # __repr__ is the most likely accidental leak path: a stray
    # `logger.info(trace)` between now and #25's regression test would
    # spill RAW_PERSONAL into structured logs that are not part of the
    # traces table's privacy posture. Redact those columns; keep the
    # metadata that helps debugging.
    def __repr__(self) -> str:  # pragma: no cover - exercised by tests
        return (
            f"Trace(trace_id={self.trace_id!r}, "
            f"created_at={self.created_at.isoformat()!r}, "
            f"trigger_source={self.trigger_source.value!r}, "
            f"archetype={self.archetype.value!r}, "
            f"model_selected={self.model_selected!r}, "
            f"prompt_version={self.prompt_version!r}, "
            f"latency_ms={self.latency_ms}, "
            f"data_sensitivity={self.data_sensitivity.value!r}, "
            f"tier={self.tier.value!r}, "
            f"input=<redacted len={len(self.input)}>, "
            f"output=<redacted len={len(self.output)}>, "
            f"assembled_context=<redacted keys={sorted(self.assembled_context.keys())}>, "
            f"tools_called=<redacted n={len(self.tools_called)}>, "
            f"embedding=<{'present' if self.embedding is not None else 'none'}>)"
        )
