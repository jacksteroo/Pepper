"""RetrievedMemorySelector — packages pre-fetched recall memory for the prompt.

The actual memory retrieval (``MemoryManager.build_context_for_query``) is
async and runs concurrently with calendar/email/etc. fetches inside
``core._chat_impl`` via ``asyncio.gather``. To preserve that concurrency the
selector takes the already-fetched string as input rather than fetching it
itself.

The selector's value is in normalising the result + carrying provenance,
not in re-doing the fetch.
"""

from __future__ import annotations

from agent.context.types import SelectorRecord


class RetrievedMemorySelector:
    name = "retrieved_memory"

    def select(self, memory_context: str) -> SelectorRecord:
        content = memory_context or ""
        provenance = {
            "selector": self.name,
            "chars": len(content),
            "present": bool(content),
        }
        return SelectorRecord(
            name=self.name,
            content=content,
            provenance=provenance,
        )
