"""Tests for `agent.context.selectors.strategies.StrategyBlockSelector` (#54).

Covers:
- empty active set yields empty record
- empty user message yields empty record
- non-matching active strategies yield empty record
- matching strategies are rendered with provenance
"""
from __future__ import annotations

from datetime import datetime, timezone

from agent.context.selectors.strategies import StrategyBlockSelector
from agent.strategies.repository import Strategy


def _strategy(text: str) -> Strategy:
    return Strategy(text=text, created_at=datetime.now(timezone.utc))


class TestStrategyBlockSelector:
    def test_empty_active_yields_empty_record(self) -> None:
        sel = StrategyBlockSelector()
        record = sel.select(situation="anything")
        assert record.content == ""
        assert record.provenance["strategies_used"] == []

    def test_empty_situation_yields_empty_record(self) -> None:
        sel = StrategyBlockSelector()
        sel.set_active_strategies([_strategy("when X, do Y")])
        record = sel.select(situation="")
        assert record.content == ""

    def test_no_overlap_yields_empty_record(self) -> None:
        sel = StrategyBlockSelector()
        sel.set_active_strategies([_strategy("when sending email, double check recipients")])
        record = sel.select(situation="morning brief planning")
        assert record.content == ""
        assert record.provenance["strategies_used"] == []

    def test_match_renders_block_and_provenance(self) -> None:
        sel = StrategyBlockSelector(top_k=3)
        s_match = _strategy("when sending email double check recipients")
        s_dud = _strategy("morning brief should be short")
        sel.set_active_strategies([s_match, s_dud])
        record = sel.select(situation="the email I want to send")
        assert "[Strategies relevant to this turn]" in record.content
        assert "double check recipients" in record.content
        assert "morning brief" not in record.content
        used = record.provenance["strategies_used"]
        assert len(used) == 1
        assert used[0]["strategy_id"] == str(s_match.strategy_id)
        assert "score" in used[0]
        assert "confidence" in used[0]

    def test_set_active_strategies_replaces_previous_snapshot(self) -> None:
        sel = StrategyBlockSelector()
        sel.set_active_strategies([_strategy("first")])
        sel.set_active_strategies([])  # Replace, not append.
        record = sel.select(situation="first")
        assert record.content == ""
