"""CapabilityBlockSelector — surfaces the live capability registry block.

The capability block is already embedded in the system prompt by
``build_system_prompt`` (see :mod:`agent.life_context`). This selector exists
so #33 can attach a structured "what sources were available this turn?" record
to traces without re-parsing the rendered system prompt.

It does NOT add a separate string to the prompt — that would duplicate what's
already in the life-context system prompt. ``content`` is the rendered block
for diagnostic / test access; the assembler does not concatenate it again.
"""

from __future__ import annotations

from typing import Any

from agent.context.types import SelectorRecord
from agent.life_context import build_capability_block


class CapabilityBlockSelector:
    name = "capability_block"

    def __init__(self, capability_registry: Any | None = None) -> None:
        self._registry = capability_registry
        # Cache the rendered block + available sources across turns. The
        # block is identical to what ``build_system_prompt`` already embeds
        # in the life-context system prompt, so it changes on the same
        # cadence as the life-context cache (i.e. only when the registry's
        # capability statuses change). The assembler's
        # ``refresh_life_context`` hook calls ``refresh()`` to invalidate.
        self._cached_block: str | None = None
        self._cached_available: list[str] | None = None

    def refresh(self) -> None:
        """Drop the cached block + available_sources list.

        Called by the assembler whenever ``refresh_life_context`` runs, so
        capability changes recorded in the registry surface on the next turn.
        """
        self._cached_block = None
        self._cached_available = None

    def _resolve_available(self) -> list[str]:
        try:
            if self._registry is not None and hasattr(
                self._registry, "get_available_sources"
            ):
                return list(self._registry.get_available_sources() or [])
        except Exception:
            # Registry probing is fail-soft — provenance reflects what we
            # actually saw, not what we wished for.
            return []
        return []

    def select(self) -> SelectorRecord:
        if self._cached_block is None:
            self._cached_block = build_capability_block(self._registry)
            self._cached_available = self._resolve_available()

        block = self._cached_block
        available = list(self._cached_available or [])

        provenance = {
            "selector": self.name,
            "available_sources": sorted(available),
            "block_chars": len(block or ""),
            "registry_present": self._registry is not None,
        }
        return SelectorRecord(
            name=self.name,
            content=block,
            provenance=provenance,
        )
