"""StrategyBlockSelector — injects top-N strategies into the system prompt.

Per #54: "Strategy block in the system prompt: top-N strategies
relevant to the current input (matched by embedding similarity on
`text`). Bake into the assembler from #32."

v0 ranking is keyword-overlap (Jaccard over salient tokens), wired
through `agent.strategies_tools.rank_strategies`. Phase 2 swaps this
for cosine similarity over the existing 1024-dim embedding column —
the selector's contract is narrow enough that the swap is local.

The selector consumes a *snapshot* of active strategies passed in via
`set_active_strategies()` so the assembler does not need to perform
async DB I/O during prompt assembly. Core gathers the snapshot before
calling `assemble()` (same pattern used for memory_context). When no
snapshot is set, the selector emits an empty record (graceful boot
when the strategies module isn't wired yet).

Optimizer carve-out: `strategy_block_v0` is recorded as a selector
name on the trace's assembled_context so future contributors who add
a `strategy_block` optimizer target can be reminded that the strategy
TEXT is operator-authored and approval-gated. The OPTIMIZATION of
this selector's *prompt template* (the wrapper text around the
strategies) IS allowed; the strategies themselves are not.
"""
from __future__ import annotations

from typing import Any

from agent.context.types import SelectorRecord
from agent.strategies.repository import Strategy
from agent.strategies_tools import DEFAULT_TOP_K, rank_strategies


class StrategyBlockSelector:
    """Read-side selector for the strategy block in the system prompt."""

    name = "strategies"

    def __init__(self, *, top_k: int = DEFAULT_TOP_K) -> None:
        self._top_k = top_k
        self._active: list[Strategy] = []

    def set_active_strategies(self, strategies: list[Strategy]) -> None:
        """Inject the active-strategy snapshot for the next select() call.

        Called by core right before assembly. Pass an empty list (or
        omit) to suppress the strategy block — the selector emits an
        empty record cleanly.
        """
        self._active = list(strategies or [])

    def select(self, *, situation: str = "") -> SelectorRecord:
        if not self._active or not situation.strip():
            return SelectorRecord(
                name=self.name,
                content="",
                provenance={
                    "selector": self.name,
                    "active_count": len(self._active),
                    "situation_chars": len(situation),
                    "strategies_used": [],
                },
            )

        ranked = rank_strategies(situation, self._active, top_k=self._top_k)
        if not ranked:
            return SelectorRecord(
                name=self.name,
                content="",
                provenance={
                    "selector": self.name,
                    "active_count": len(self._active),
                    "situation_chars": len(situation),
                    "strategies_used": [],
                },
            )

        # Render the block. Each strategy is on its own line with its
        # confidence — the operator sees what was injected; the model
        # sees something it can name in its reasoning.
        lines = ["[Strategies relevant to this turn]"]
        for strategy, score in ranked:
            lines.append(
                f"- {strategy.text} "
                f"(score={score:.2f}, confidence={strategy.confidence:.2f})"
            )
        content = "\n".join(lines)

        provenance: dict[str, Any] = {
            "selector": self.name,
            "active_count": len(self._active),
            "situation_chars": len(situation),
            "strategies_used": [
                {
                    "strategy_id": str(s.strategy_id),
                    "version": s.version,
                    "score": round(score, 4),
                    "confidence": s.confidence,
                }
                for s, score in ranked
            ],
        }
        return SelectorRecord(name=self.name, content=content, provenance=provenance)
