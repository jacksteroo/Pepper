"""LastNTurnsSelector — pulls the recent working-memory turns for the prompt.

Wraps :meth:`MemoryManager.get_working_memory`. ``isolated`` callers (scheduler
/ automation jobs) get an empty history so their work never bleeds into a
user session — same semantics as ``history = [] if isolated else …`` in the
previous core.py code.
"""

from __future__ import annotations

from typing import Any

from agent.context.types import SelectorRecord


class LastNTurnsSelector:
    name = "last_n_turns"

    def __init__(self, memory_manager: Any) -> None:
        self._memory = memory_manager

    def select(
        self,
        *,
        limit: int,
        isolated: bool,
    ) -> SelectorRecord:
        if isolated:
            history: list[dict[str, Any]] = []
            history_with_ts: list[dict[str, Any]] = []
        else:
            try:
                history = list(self._memory.get_working_memory(limit=limit))
            except Exception:
                # Memory layer should never crash a turn — graceful degrade
                # to empty history matches previous behaviour.
                history = []
            # Optional per-turn timestamps (#34) for the inspector. Falls
            # back to the timestamp-less list when the memory manager
            # doesn't expose the helper.
            try:
                if hasattr(self._memory, "get_working_memory_with_timestamps"):
                    history_with_ts = list(
                        self._memory.get_working_memory_with_timestamps(
                            limit=limit,
                        )
                    )
                else:
                    history_with_ts = list(history)
            except Exception:
                history_with_ts = list(history)

        roles = [m.get("role") for m in history if isinstance(m, dict)]
        # ``last_n_turns`` (#33 required key) is the number of conversation
        # *turns* — not raw messages. A turn is a (user, assistant) pair, so
        # we floor-divide message count by 2 (rounded up to capture trailing
        # user-only turns). ``n_messages`` and ``limit`` stay for richer
        # inspection.
        n_turns = (len(history) + 1) // 2
        provenance = {
            "selector": self.name,
            "limit": limit,
            "isolated": isolated,
            "n_messages": len(history),
            "last_n_turns": n_turns,
            "role_counts": {
                "user": sum(1 for r in roles if r == "user"),
                "assistant": sum(1 for r in roles if r == "assistant"),
                "system": sum(1 for r in roles if r == "system"),
                "tool": sum(1 for r in roles if r == "tool"),
            },
            # #34 — per-turn rows (with timestamps when the memory manager
            # exposes them) for the inspector. The LLM-facing ``content``
            # field above stays timestamp-less to keep the prompt small;
            # this duplicates the rows for inspection. Privacy: same content
            # already lives in the prompt itself.
            "content": list(history_with_ts),
        }
        return SelectorRecord(
            name=self.name,
            content=history,
            provenance=provenance,
        )
