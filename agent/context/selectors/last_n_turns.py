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
        else:
            try:
                history = list(self._memory.get_working_memory(limit=limit))
            except Exception:
                # Memory layer should never crash a turn — graceful degrade
                # to empty history matches previous behaviour.
                history = []

        roles = [m.get("role") for m in history if isinstance(m, dict)]
        provenance = {
            "selector": self.name,
            "limit": limit,
            "isolated": isolated,
            "n_messages": len(history),
            "role_counts": {
                "user": sum(1 for r in roles if r == "user"),
                "assistant": sum(1 for r in roles if r == "assistant"),
                "system": sum(1 for r in roles if r == "system"),
                "tool": sum(1 for r in roles if r == "tool"),
            },
        }
        return SelectorRecord(
            name=self.name,
            content=history,
            provenance=provenance,
        )
