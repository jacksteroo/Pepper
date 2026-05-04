"""Unit tests for `agent.strategies_tools` (#54).

Covers:
- tool schema declares required fields
- query_strategies validates input + bumps usage on matches
- ranking returns top_k by Jaccard overlap, ties broken by recency
- propose_strategy_update NEVER writes directly — it goes through the
  diffs queue
- propose_strategy_update validates input
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from agent.strategies.repository import (
    Strategy,
    StrategyCreatedBy,
    StrategyStatus,
)
from agent.strategy_diffs import StrategyDiff, StrategyDiffStatus
from agent.strategies_tools import (
    DEFAULT_TOP_K,
    MAX_TOP_K,
    STRATEGIES_TOOLS,
    execute_propose_strategy_update,
    execute_query_strategies,
    execute_strategies_tool,
    rank_strategies,
)


# ── Schema ───────────────────────────────────────────────────────────────────


class TestSchema:
    def test_two_tools_named_correctly(self) -> None:
        names = [t["function"]["name"] for t in STRATEGIES_TOOLS]
        assert set(names) == {"query_strategies", "propose_strategy_update"}

    def test_query_situation_required(self) -> None:
        for t in STRATEGIES_TOOLS:
            if t["function"]["name"] == "query_strategies":
                assert "situation" in t["function"]["parameters"]["required"]

    def test_propose_required_fields(self) -> None:
        for t in STRATEGIES_TOOLS:
            if t["function"]["name"] == "propose_strategy_update":
                req = t["function"]["parameters"]["required"]
                assert "new_text" in req
                assert "reason" in req
                assert "strategy_id" not in req


# ── Ranking ──────────────────────────────────────────────────────────────────


class TestRanking:
    def _strategy(self, text: str, *, days_old: int = 0) -> Strategy:
        return Strategy(
            text=text,
            created_by=StrategyCreatedBy.JACK,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_old),
        )

    def test_zero_overlap_returns_empty(self) -> None:
        ranked = rank_strategies(
            "morning brief planning",
            [self._strategy("when sending email, double-check recipients")],
            top_k=5,
        )
        assert ranked == []

    def test_overlap_returns_match_with_score(self) -> None:
        s = self._strategy("when sending email double check recipients")
        ranked = rank_strategies("the email I want to send", [s], top_k=5)
        assert len(ranked) == 1
        assert ranked[0][0] is s
        assert 0 < ranked[0][1] <= 1.0

    def test_ties_broken_by_recency(self) -> None:
        s_old = self._strategy("reply email check sender", days_old=200)
        s_new = self._strategy("reply email check sender", days_old=2)
        ranked = rank_strategies("reply email check sender", [s_old, s_new], top_k=5)
        # Same text → same score; the newer must come first.
        assert ranked[0][0] is s_new

    def test_top_k_caps_results(self) -> None:
        strategies = [
            self._strategy(f"matching text {i}") for i in range(20)
        ]
        ranked = rank_strategies("matching text", strategies, top_k=5)
        assert len(ranked) <= 5


# ── Stub repos ───────────────────────────────────────────────────────────────


class _FakeStrategyRepo:
    def __init__(self, active: list[Strategy]) -> None:
        self._active = active
        self.usage_bumps: list[uuid.UUID] = []

    async def query_active(self, *, limit: int = 100) -> list[Strategy]:
        return list(self._active)[:limit]

    async def bump_usage(self, strategy_id) -> None:
        sid = strategy_id if isinstance(strategy_id, uuid.UUID) else uuid.UUID(str(strategy_id))
        self.usage_bumps.append(sid)


class _FakeDiffsRepo:
    def __init__(self) -> None:
        self.appended: list[StrategyDiff] = []

    async def append(self, diff: StrategyDiff) -> StrategyDiff:
        self.appended.append(diff)
        return diff


# ── Query executor ───────────────────────────────────────────────────────────


class TestExecuteQuery:
    @pytest.mark.asyncio
    async def test_missing_situation_returns_error(self) -> None:
        repo = _FakeStrategyRepo([])
        result = await execute_query_strategies({}, repo=repo)  # type: ignore[arg-type]
        assert "error" in result

    @pytest.mark.asyncio
    async def test_empty_situation_returns_error(self) -> None:
        repo = _FakeStrategyRepo([])
        result = await execute_query_strategies(
            {"situation": "   "}, repo=repo  # type: ignore[arg-type]
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_invalid_top_k_type_returns_error(self) -> None:
        repo = _FakeStrategyRepo([])
        result = await execute_query_strategies(
            {"situation": "ok", "top_k": "lots"}, repo=repo  # type: ignore[arg-type]
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_top_k_clamped_to_max(self) -> None:
        # Should not error, just clamp.
        repo = _FakeStrategyRepo([])
        result = await execute_query_strategies(
            {"situation": "ok", "top_k": 1000}, repo=repo  # type: ignore[arg-type]
        )
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_match_bumps_usage(self) -> None:
        s = Strategy(text="when sending email double check recipients")
        repo = _FakeStrategyRepo([s])
        result = await execute_query_strategies(
            {"situation": "the email I want to send"}, repo=repo  # type: ignore[arg-type]
        )
        assert result["ok"] is True
        assert result["count"] == 1
        assert repo.usage_bumps == [s.strategy_id]

    @pytest.mark.asyncio
    async def test_no_match_does_not_bump(self) -> None:
        s = Strategy(text="when sending email double check recipients")
        repo = _FakeStrategyRepo([s])
        result = await execute_query_strategies(
            {"situation": "morning brief"}, repo=repo  # type: ignore[arg-type]
        )
        assert result["count"] == 0
        assert repo.usage_bumps == []


# ── Propose executor ─────────────────────────────────────────────────────────


class TestExecutePropose:
    @pytest.mark.asyncio
    async def test_missing_new_text_returns_error(self) -> None:
        diffs = _FakeDiffsRepo()
        result = await execute_propose_strategy_update(
            {"reason": "ok"}, diffs_repo=diffs  # type: ignore[arg-type]
        )
        assert "error" in result
        assert diffs.appended == []

    @pytest.mark.asyncio
    async def test_missing_reason_returns_error(self) -> None:
        diffs = _FakeDiffsRepo()
        result = await execute_propose_strategy_update(
            {"new_text": "ok"}, diffs_repo=diffs  # type: ignore[arg-type]
        )
        assert "error" in result
        assert diffs.appended == []

    @pytest.mark.asyncio
    async def test_routes_through_diffs_queue_not_directly(self) -> None:
        diffs = _FakeDiffsRepo()
        result = await execute_propose_strategy_update(
            {"new_text": "new strategy.", "reason": "noticed a pattern"},
            diffs_repo=diffs,  # type: ignore[arg-type]
        )
        assert result["ok"] is True
        assert result["queued"] is True
        assert len(diffs.appended) == 1
        diff = diffs.appended[0]
        assert diff.proposed_text == "new strategy."
        assert diff.rationale == "noticed a pattern"
        assert diff.status == StrategyDiffStatus.PENDING
        # No target_strategy_id → new lineage.
        assert diff.target_strategy_id is None

    @pytest.mark.asyncio
    async def test_invalid_strategy_id_uuid_returns_error(self) -> None:
        diffs = _FakeDiffsRepo()
        result = await execute_propose_strategy_update(
            {
                "new_text": "ok",
                "reason": "ok",
                "strategy_id": "not-a-uuid",
            },
            diffs_repo=diffs,  # type: ignore[arg-type]
        )
        assert "error" in result
        assert diffs.appended == []

    @pytest.mark.asyncio
    async def test_with_strategy_id_creates_versioned_diff(self) -> None:
        diffs = _FakeDiffsRepo()
        target = uuid.uuid4()
        result = await execute_propose_strategy_update(
            {
                "new_text": "improved.",
                "reason": "the old wording was vague",
                "strategy_id": str(target),
            },
            diffs_repo=diffs,  # type: ignore[arg-type]
        )
        assert result["ok"] is True
        assert diffs.appended[0].target_strategy_id == target


# ── Dispatcher ───────────────────────────────────────────────────────────────


class TestDispatcher:
    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self) -> None:
        repo = _FakeStrategyRepo([])
        diffs = _FakeDiffsRepo()
        result = await execute_strategies_tool(
            "frobnicate",
            {},
            repo=repo,  # type: ignore[arg-type]
            diffs_repo=diffs,  # type: ignore[arg-type]
        )
        assert "error" in result
        assert "unknown" in result["error"]
