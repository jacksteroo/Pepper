"""RetrievedMemorySelector — packages pre-fetched recall memory for the prompt.

The actual memory retrieval (``MemoryManager.build_context_for_query``) is
async and runs concurrently with calendar/email/etc. fetches inside
``core._chat_impl`` via ``asyncio.gather``. To preserve that concurrency the
selector takes the already-fetched string as input rather than fetching it
itself.

The selector's value is in normalising the result + carrying provenance,
not in re-doing the fetch.

#33 adds structured memory IDs to provenance so traces can answer "which
memories did the model see, with what score?". The selector accepts
``memory_records`` — a list of result dicts from ``MemoryManager`` —
alongside the rendered string. Privacy: only IDs and scores travel into
provenance; raw memory text never appears here (it's already in the
trace's ``input``/``output`` to the extent that matters).
"""

from __future__ import annotations

from typing import Any

from agent.context.types import SelectorRecord


class RetrievedMemorySelector:
    name = "retrieved_memory"

    def select(
        self,
        memory_context: str,
        memory_records: list[dict[str, Any]] | None = None,
    ) -> SelectorRecord:
        content = memory_context or ""
        records = memory_records or []

        memory_ids: list[list[Any]] = []
        for row in records:
            if not isinstance(row, dict):
                continue
            rid = row.get("id")
            if rid is None:
                continue
            # Score may be under ``score`` (RRF / blended) or ``sim``
            # (pure semantic). Prefer ``score`` so ranked-list semantics
            # match what the retrieval layer surfaces. Cast to float and
            # default to 0.0 so the JSONB shape is stable.
            score = row.get("score")
            if score is None:
                score = row.get("sim")
            try:
                score_f = float(score) if score is not None else 0.0
            except (TypeError, ValueError):
                score_f = 0.0
            memory_ids.append([str(rid), score_f])

        provenance = {
            "selector": self.name,
            "chars": len(content),
            "present": bool(content),
            "memory_ids": memory_ids,
            "n_memories": len(memory_ids),
        }
        return SelectorRecord(
            name=self.name,
            content=content,
            provenance=provenance,
        )
