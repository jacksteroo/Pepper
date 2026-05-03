"""Dataset builder — projects traces from the store into ``TraceExample``s.

Lives in its own module so the runner doesn't import the trace repository
directly. Tests build datasets in-memory via ``build_from_traces``; the
production path uses ``build_from_repo``.

Filtering surface mirrors what #45 calls out: archetype, prompt_version,
date window. Free-text contains-filter is intentionally NOT exposed here
— that path runs trigram scans against raw trace text and we want the
optimizer's input to be reproducible.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Optional

import structlog

from agent.optimizer.schema import TraceExample
from agent.traces.repository import MAX_QUERY_LIMIT, TraceRepository
from agent.traces.schema import Archetype, Trace

logger = structlog.get_logger(__name__)


def trace_to_example(trace: Trace) -> TraceExample:
    """Project a ``Trace`` into a ``TraceExample``.

    Drops fields the optimizer doesn't need (model_selected, latency,
    tier, embedding, ...). Keeps ``assembled_context`` and
    ``tools_called`` whatever the caller loaded — empty by default.
    """
    return TraceExample(
        trace_id=trace.trace_id,
        archetype=trace.archetype.value,
        prompt_version=trace.prompt_version,
        input=trace.input,
        output=trace.output,
        assembled_context=dict(trace.assembled_context or {}),
        tools_called=list(trace.tools_called or []),
        user_reaction=dict(trace.user_reaction) if trace.user_reaction else None,
    )


def build_from_traces(
    traces: Iterable[Trace],
    *,
    prompt_version: Optional[str] = None,
) -> list[TraceExample]:
    """Build a dataset from an in-memory iterable.

    Used by tests and by callers that already hold ``Trace`` instances.
    Production code uses ``build_from_repo``.

    ``prompt_version`` filter is applied here too so the in-memory and
    repo-backed paths have the same semantics.
    """
    out: list[TraceExample] = []
    for t in traces:
        if prompt_version is not None and t.prompt_version != prompt_version:
            continue
        out.append(trace_to_example(t))
    return out


async def build_from_repo(
    repo: TraceRepository,
    *,
    archetype: Archetype,
    prompt_version: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = MAX_QUERY_LIMIT,
    with_payload: bool = True,
) -> list[TraceExample]:
    """Pull traces matching the filters and project them to examples.

    ``with_payload=True`` by default because most optimizer adapters need
    ``assembled_context``. Override to ``False`` if the adapter only
    consumes ``input``/``output`` to save the JSONB load.

    The repository's existing pagination cap (``MAX_QUERY_LIMIT``) is
    respected; callers needing more should chunk via ``cursor`` and
    re-call (the optimizer never needs more than ~1000 examples per run
    in practice — a bigger sample makes GEPA slower without adding
    signal).
    """
    traces: Sequence[Trace] = await repo.query(
        archetype=archetype,
        since=since,
        until=until,
        limit=limit,
        with_payload=with_payload,
    )
    examples = build_from_traces(traces, prompt_version=prompt_version)
    logger.info(
        "optimizer.dataset.built",
        archetype=archetype.value,
        prompt_version_filter=prompt_version,
        since=since.isoformat() if since else None,
        until=until.isoformat() if until else None,
        traces_in=len(traces),
        examples_out=len(examples),
    )
    return examples


def dataset_hash(examples: Iterable[TraceExample]) -> str:
    """Stable hex hash of a dataset, used in the audit log.

    Sorts trace_ids before hashing so dataset equality is order-
    independent. Returns sha256 hex digest.
    """
    ids = sorted(e.trace_id for e in examples)
    h = hashlib.sha256()
    for tid in ids:
        h.update(tid.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()
