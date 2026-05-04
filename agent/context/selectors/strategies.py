"""StrategySelector — injects the strategy block into the system prompt.

Embeds the current user message, queries the strategy repository by
similarity, and returns a formatted block of behavioral guidelines for
the LLM to follow.

Design notes:
  - Never raises — all failures return an empty SelectorRecord so a
    missing DB or unavailable Ollama never breaks a turn.
  - NOT optimizable: strategy text is ground truth set by the owner.
    The ``non_optimizable = True`` flag signals this to the optimizer.
  - The strategy block is recorded in ``assembled_context`` provenance
    so the trace inspector can display which strategies were active.
"""
from __future__ import annotations

from typing import Any

import structlog

from agent.context.types import SelectorRecord

logger = structlog.get_logger(__name__)

# Maximum number of strategies to inject per turn.
_DEFAULT_TOP_K = 5

# Flag that tells the optimizer to skip this selector.
# The optimizer adapter checks for this attribute before mutating.
non_optimizable: bool = True


class StrategySelector:
    """Query active strategies and format them for the system prompt.

    This selector is NOT optimizable — strategy text is owner-authored
    ground truth and must not be rewritten by the optimizer.
    """

    name = "strategies"
    # Optimizer exclusion flag: same pattern as identity block in ADR-0008.
    non_optimizable = True

    def __init__(
        self,
        *,
        db_factory: Any | None = None,
        llm_client: Any | None = None,
        top_k: int = _DEFAULT_TOP_K,
    ) -> None:
        self._db_factory = db_factory
        self._llm_client = llm_client
        self._top_k = top_k

    def select_sync(self, user_message: str = "") -> SelectorRecord:
        """Synchronous wrapper — returns an empty record.

        The real work happens in :meth:`select_async`. The assembler
        calls the async version; this method exists so the selector
        can be used in synchronous test contexts.
        """
        return SelectorRecord(
            name=self.name,
            content="",
            provenance={
                "selector": self.name,
                "present": False,
                "strategy_ids": [],
                "n_strategies": 0,
                "note": "sync_select_not_supported",
            },
        )

    async def select(self, user_message: str = "") -> SelectorRecord:
        """Embed ``user_message``, query strategies, return a formatted block.

        Returns an empty ``SelectorRecord`` on any failure so a broken
        DB or embedder never interrupts a turn.
        """
        if not self._db_factory or not self._llm_client:
            return self._empty_record("no_db_or_llm")

        try:
            embedding = await self._llm_client.embed(user_message or "general")
        except Exception as exc:
            logger.warning("strategy_selector_embed_failed", error=str(exc))
            return self._empty_record("embed_failed")

        try:
            async with self._db_factory() as session:
                from agent.strategies.repository import StrategyRepository

                repo = StrategyRepository(session)
                rows = await repo.query_by_similarity(
                    embedding, top_k=self._top_k
                )
        except Exception as exc:
            logger.warning("strategy_selector_query_failed", error=str(exc))
            return self._empty_record("query_failed")

        if not rows:
            return self._empty_record("no_strategies")

        lines = [f"- {row.text}" for row in rows]
        content = (
            "[Behavioral strategies — follow these guidelines]\n"
            + "\n".join(lines)
            + "\n[End strategies]"
        )

        strategy_ids = [str(row.strategy_id) for row in rows]
        return SelectorRecord(
            name=self.name,
            content=content,
            provenance={
                "selector": self.name,
                "present": True,
                "strategy_ids": strategy_ids,
                "n_strategies": len(rows),
                "chars": len(content),
                # non_optimizable flag recorded in provenance for the
                # optimizer to inspect at runtime.
                "non_optimizable": True,
            },
        )

    def _empty_record(self, reason: str) -> SelectorRecord:
        return SelectorRecord(
            name=self.name,
            content="",
            provenance={
                "selector": self.name,
                "present": False,
                "strategy_ids": [],
                "n_strategies": 0,
                "reason": reason,
                "non_optimizable": True,
            },
        )
