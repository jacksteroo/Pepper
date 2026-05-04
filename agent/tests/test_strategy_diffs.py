"""Tests for `agent.strategy_diffs` (#54).

Covers dataclass invariants, repository surface lock, and the
propose → approve → apply cycle through stub repositories.
"""
from __future__ import annotations

import inspect
import uuid
from unittest.mock import AsyncMock

import pytest

from agent.strategies.repository import (
    Strategy,
    StrategyCreatedBy,
    StrategyStatus,
)
from agent.strategy_diffs import (
    StrategyDiff,
    StrategyDiffRepository,
    StrategyDiffStatus,
)


class TestDiffDataclass:
    def test_default_status_is_pending(self) -> None:
        diff = StrategyDiff(proposed_text="when X, do Y")
        assert diff.status == StrategyDiffStatus.PENDING

    def test_empty_text_rejected(self) -> None:
        with pytest.raises(ValueError, match="proposed_text cannot be empty"):
            StrategyDiff(proposed_text="")
        with pytest.raises(ValueError, match="proposed_text cannot be empty"):
            StrategyDiff(proposed_text="   ")

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValueError, match="status must be one of"):
            StrategyDiff(proposed_text="x", status="archived")

    def test_invalid_proposed_by_rejected(self) -> None:
        with pytest.raises(ValueError, match="proposed_by"):
            StrategyDiff(proposed_text="x", proposed_by="optimizer")


class TestRepositorySurface:
    expected_public: frozenset[str] = frozenset({
        "append",
        "list_pending",
        "get",
        "approve",
        "reject",
    })

    def test_public_method_set_is_exhaustive(self) -> None:
        names = {
            n
            for n, _ in inspect.getmembers(StrategyDiffRepository, predicate=inspect.isfunction)
            if not n.startswith("_")
        }
        assert names == self.expected_public

    def test_no_destructive_paths(self) -> None:
        forbidden = ("update_text", "edit", "rewrite", "delete", "purge", "drop")
        names = {n for n, _ in inspect.getmembers(StrategyDiffRepository)}
        bad = [n for n in names if any(n.startswith(p) for p in forbidden)]
        assert bad == []


# ── End-to-end through a stub strategies-repo ───────────────────────────────


class _FakeStrategiesRepo:
    """Records what append/append_version were called with."""

    def __init__(self) -> None:
        self.appends: list[Strategy] = []
        self.versions: list[tuple[Strategy, str]] = []
        self._by_id: dict[uuid.UUID, Strategy] = {}

    def seed(self, strategy: Strategy) -> None:
        self._by_id[strategy.strategy_id] = strategy

    async def append(self, strategy: Strategy, *, is_contradicting=None) -> Strategy:
        self.appends.append(strategy)
        self._by_id[strategy.strategy_id] = strategy
        return strategy

    async def append_version(
        self,
        *,
        parent: Strategy,
        new_text: str,
        created_by: str,
        source_trace_ids=None,
        embedding=None,
    ) -> Strategy:
        new = Strategy(
            text=new_text,
            version=parent.version + 1,
            parent_strategy_id=parent.strategy_id,
            created_by=created_by,
        )
        self.versions.append((parent, new_text))
        self._by_id[new.strategy_id] = new
        return new

    async def get(self, strategy_id) -> Strategy | None:
        sid = strategy_id if isinstance(strategy_id, uuid.UUID) else uuid.UUID(str(strategy_id))
        return self._by_id.get(sid)


class _FakeDiffsRepo(StrategyDiffRepository):
    """Skip the SQL session; keep the dispatch shape from the real
    repository but back the storage with a dict."""

    def __init__(self, strategies_repo: _FakeStrategiesRepo) -> None:
        self._strategies = strategies_repo
        self._diffs: dict[uuid.UUID, StrategyDiff] = {}

    async def append(self, diff):
        self._diffs[diff.diff_id] = diff
        return diff

    async def list_pending(self, *, limit: int = 50):
        return [d for d in self._diffs.values() if d.status == StrategyDiffStatus.PENDING]

    async def get(self, diff_id):
        sid = diff_id if isinstance(diff_id, uuid.UUID) else uuid.UUID(str(diff_id))
        return self._diffs.get(sid)

    async def reject(self, diff_id):
        d = await self.get(diff_id)
        if d is None:
            return
        self._diffs[d.diff_id] = StrategyDiff(
            proposed_text=d.proposed_text,
            rationale=d.rationale,
            target_strategy_id=d.target_strategy_id,
            proposed_by=d.proposed_by,
            source_trace_ids=list(d.source_trace_ids),
            status=StrategyDiffStatus.REJECTED,
            diff_id=d.diff_id,
            created_at=d.created_at,
        )

    async def approve(self, diff_id) -> Strategy:
        diff = await self.get(diff_id)
        assert diff is not None and diff.status == StrategyDiffStatus.PENDING
        # Flip status in-place for the fake.
        self._diffs[diff.diff_id] = StrategyDiff(
            proposed_text=diff.proposed_text,
            rationale=diff.rationale,
            target_strategy_id=diff.target_strategy_id,
            proposed_by=diff.proposed_by,
            source_trace_ids=list(diff.source_trace_ids),
            status=StrategyDiffStatus.APPROVED,
            diff_id=diff.diff_id,
            created_at=diff.created_at,
        )
        if diff.target_strategy_id is None:
            return await self._strategies.append(
                Strategy(
                    text=diff.proposed_text,
                    created_by=diff.proposed_by,
                    source_trace_ids=list(diff.source_trace_ids),
                )
            )
        parent = await self._strategies.get(diff.target_strategy_id)
        assert parent is not None
        return await self._strategies.append_version(
            parent=parent,
            new_text=diff.proposed_text,
            created_by=diff.proposed_by,
            source_trace_ids=list(diff.source_trace_ids),
        )


class TestProposeApproveCycle:
    @pytest.mark.asyncio
    async def test_new_lineage_approval_appends_strategy(self) -> None:
        srepo = _FakeStrategiesRepo()
        drepo = _FakeDiffsRepo(srepo)

        diff = StrategyDiff(
            proposed_text="when summarising, stop sooner.",
            rationale="brief was padding too much",
        )
        await drepo.append(diff)

        new_strategy = await drepo.approve(diff.diff_id)
        assert new_strategy.text == "when summarising, stop sooner."
        assert len(srepo.appends) == 1
        assert srepo.versions == []
        # Diff is now APPROVED.
        approved = await drepo.get(diff.diff_id)
        assert approved is not None and approved.status == StrategyDiffStatus.APPROVED

    @pytest.mark.asyncio
    async def test_versioned_approval_calls_append_version(self) -> None:
        srepo = _FakeStrategiesRepo()
        parent = Strategy(text="old wording")
        srepo.seed(parent)
        drepo = _FakeDiffsRepo(srepo)

        diff = StrategyDiff(
            proposed_text="new wording.",
            rationale="cleaner",
            target_strategy_id=parent.strategy_id,
        )
        await drepo.append(diff)
        await drepo.approve(diff.diff_id)

        assert len(srepo.versions) == 1
        assert srepo.versions[0][0] is parent
        assert srepo.versions[0][1] == "new wording."
