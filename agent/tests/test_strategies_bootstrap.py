"""Bootstrap-loader tests for the Strategy Hub.

Verifies the loader's contract:
- inserts 5–10 strategies from `BOOTSTRAP_STRATEGIES`
- only inserts when the table is empty
- stamps each row as `created_by=bootstrap`
"""
from __future__ import annotations

import pytest

from agent.strategies.bootstrap import BOOTSTRAP_STRATEGIES, bootstrap_if_empty
from agent.strategies.repository import (
    Strategy,
    StrategyCreatedBy,
    StrategyRepository,
)


class _FakeRepo:
    """In-memory stand-in for StrategyRepository that captures `append`
    calls in the order they fire."""

    def __init__(self, *, existing: int = 0) -> None:
        self._count = existing
        self.appended: list[Strategy] = []

    async def count_active(self) -> int:
        return self._count

    async def append(self, strategy: Strategy, *, is_contradicting=None) -> Strategy:
        self.appended.append(strategy)
        self._count += 1
        return strategy


class TestBootstrap:
    def test_bootstrap_strategy_count_in_band(self) -> None:
        # The data layer ships with 5–10 strategies per #53 AC.
        assert 5 <= len(BOOTSTRAP_STRATEGIES) <= 10

    def test_bootstrap_strategies_are_nonempty(self) -> None:
        for s in BOOTSTRAP_STRATEGIES:
            assert s.strip(), "bootstrap strategy text must be nonempty"

    def test_bootstrap_strategies_are_unique(self) -> None:
        # Catches accidental copy-paste.
        assert len(set(BOOTSTRAP_STRATEGIES)) == len(BOOTSTRAP_STRATEGIES)

    @pytest.mark.asyncio
    async def test_loader_inserts_when_empty(self) -> None:
        repo = _FakeRepo(existing=0)
        n = await bootstrap_if_empty(repo)  # type: ignore[arg-type]
        assert n == len(BOOTSTRAP_STRATEGIES)
        assert len(repo.appended) == len(BOOTSTRAP_STRATEGIES)
        for s in repo.appended:
            assert s.created_by == StrategyCreatedBy.BOOTSTRAP
            assert s.version == 1
            assert s.parent_strategy_id is None

    @pytest.mark.asyncio
    async def test_loader_skips_when_nonempty(self) -> None:
        repo = _FakeRepo(existing=3)
        n = await bootstrap_if_empty(repo)  # type: ignore[arg-type]
        assert n == 0
        assert repo.appended == []

    @pytest.mark.asyncio
    async def test_loader_preserves_text_verbatim(self) -> None:
        repo = _FakeRepo(existing=0)
        await bootstrap_if_empty(repo)  # type: ignore[arg-type]
        assert tuple(s.text for s in repo.appended) == BOOTSTRAP_STRATEGIES
