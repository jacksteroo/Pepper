"""Static + behavioural tests for `agent.strategies.repository`.

These tests do not require a live Postgres — they exercise the dataclass
contract, the public-method surface, and the contradiction-detection
flow against an in-memory stub session that satisfies just enough of
the SQLAlchemy AsyncSession protocol for the repository's call paths.
"""
from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from agent.strategies.repository import (
    STRATEGY_EMBEDDING_DIM,
    Strategy,
    StrategyCreatedBy,
    StrategyRepository,
    StrategyStatus,
    compute_confidence_v0,
)


# ── Dataclass invariants ─────────────────────────────────────────────────────


class TestStrategyDataclass:
    def test_default_status_is_active(self) -> None:
        s = Strategy(text="when X, do Y")
        assert s.status == StrategyStatus.ACTIVE

    def test_default_version_is_one(self) -> None:
        s = Strategy(text="when X, do Y")
        assert s.version == 1

    def test_default_created_by_is_jack(self) -> None:
        s = Strategy(text="when X, do Y")
        assert s.created_by == StrategyCreatedBy.JACK

    def test_empty_text_rejected(self) -> None:
        with pytest.raises(ValueError, match="text cannot be empty"):
            Strategy(text="")
        with pytest.raises(ValueError, match="text cannot be empty"):
            Strategy(text="   ")

    def test_invalid_created_by_rejected(self) -> None:
        with pytest.raises(ValueError, match="created_by"):
            Strategy(text="when X, do Y", created_by="optimizer")

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValueError, match="status"):
            Strategy(text="when X, do Y", status="archived")

    def test_version_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="version"):
            Strategy(text="when X, do Y", version=0)

    def test_confidence_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            Strategy(text="when X, do Y", confidence=1.5)
        with pytest.raises(ValueError, match="confidence"):
            Strategy(text="when X, do Y", confidence=-0.1)

    def test_embedding_dim_validated(self) -> None:
        with pytest.raises(ValueError, match="embedding"):
            Strategy(text="when X, do Y", embedding=[0.0] * 768)

    def test_embedding_with_correct_dim_accepted(self) -> None:
        s = Strategy(
            text="when X, do Y",
            embedding=[0.0] * STRATEGY_EMBEDDING_DIM,
        )
        assert s.embedding is not None and len(s.embedding) == STRATEGY_EMBEDDING_DIM


# ── Repository surface ───────────────────────────────────────────────────────


class TestRepositorySurface:
    """The repository must expose only the documented public methods.

    Adding a new public method requires updating both this set and the
    module docstring — keeps the surface from drifting.
    """

    expected_public: frozenset[str] = frozenset({
        "append",
        "append_version",
        "query_active",
        "get",
        "bump_usage",
        "confirm_correct",
        "set_status",
        "count_active",
    })

    def test_public_method_set_is_exhaustive(self) -> None:
        names = {
            n
            for n, _ in inspect.getmembers(StrategyRepository, predicate=inspect.isfunction)
            if not n.startswith("_")
        }
        assert names == self.expected_public, (
            f"expected {sorted(self.expected_public)}, got {sorted(names)}"
        )

    def test_no_destructive_text_edit_path(self) -> None:
        """Editing text must go through `append_version`, never an
        `update_text` / `delete` / etc."""
        forbidden = ("update_text", "edit", "rewrite", "delete", "purge", "drop")
        names = {
            n
            for n, _ in inspect.getmembers(StrategyRepository, predicate=inspect.isfunction)
        }
        bad = [n for n in names if any(n.startswith(p) for p in forbidden)]
        assert bad == [], f"forbidden mutation methods exposed: {bad}"


# ── Confidence v0 ────────────────────────────────────────────────────────────


class TestConfidenceV0:
    def test_zero_usage_no_confirmation_is_baseline(self) -> None:
        c = compute_confidence_v0(usage_count=0, last_confirmed_correct=None)
        assert c == pytest.approx(0.5)

    def test_usage_count_lifts_confidence(self) -> None:
        c5 = compute_confidence_v0(usage_count=5, last_confirmed_correct=None)
        c0 = compute_confidence_v0(usage_count=0, last_confirmed_correct=None)
        assert c5 > c0

    def test_recent_confirmation_lifts_confidence(self) -> None:
        now = datetime.now(timezone.utc)
        recent = now - timedelta(days=10)
        old = now - timedelta(days=200)
        c_recent = compute_confidence_v0(usage_count=0, last_confirmed_correct=recent, now=now)
        c_old = compute_confidence_v0(usage_count=0, last_confirmed_correct=old, now=now)
        assert c_recent > c_old

    def test_confidence_capped_at_one(self) -> None:
        now = datetime.now(timezone.utc)
        c = compute_confidence_v0(
            usage_count=10_000,
            last_confirmed_correct=now,
            now=now,
        )
        assert 0.0 <= c <= 1.0
        assert c == 1.0

    def test_confidence_floored_at_zero(self) -> None:
        # Constructed input: negative usage_count is clamped, so confidence
        # cannot fall below 0.5 in the v0 heuristic — but the bound applies.
        c = compute_confidence_v0(
            usage_count=-5, last_confirmed_correct=None
        )
        assert 0.0 <= c <= 1.0


# ── Lineage / version invariants ─────────────────────────────────────────────


class _StubSession:
    """In-memory stub of just enough of `AsyncSession` for the repo's
    code paths. Tracks adds + flush calls + executed statements.
    """

    def __init__(self) -> None:
        self.added: list = []
        self.flush_count: int = 0
        self.execute = AsyncMock(return_value=_StubResult([]))

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1


class _StubResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return _StubScalars(self._rows)


class _StubScalars:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)


class TestAppendValidations:
    @pytest.mark.asyncio
    async def test_append_rejects_versions_above_one(self) -> None:
        repo = StrategyRepository(_StubSession())
        with pytest.raises(ValueError, match="new lineage"):
            await repo.append(Strategy(text="hi", version=2))

    @pytest.mark.asyncio
    async def test_append_rejects_parent_id(self) -> None:
        repo = StrategyRepository(_StubSession())
        with pytest.raises(ValueError, match="new lineage"):
            await repo.append(
                Strategy(text="hi", parent_strategy_id=uuid.uuid4())
            )

    @pytest.mark.asyncio
    async def test_set_status_rejects_unknown_status(self) -> None:
        repo = StrategyRepository(_StubSession())
        with pytest.raises(ValueError, match="status must be one of"):
            await repo.set_status(uuid.uuid4(), "archived")


class TestVersionLineage:
    @pytest.mark.asyncio
    async def test_append_version_rejects_already_superseded_parent(self) -> None:
        repo = StrategyRepository(_StubSession())
        parent = Strategy(text="parent", status=StrategyStatus.SUPERSEDED)
        with pytest.raises(ValueError, match="already superseded"):
            await repo.append_version(
                parent=parent, new_text="child", created_by=StrategyCreatedBy.REFLECTOR
            )


# ── Contradiction detection ──────────────────────────────────────────────────


class TestContradictionDetection:
    @pytest.mark.asyncio
    async def test_first_match_flagged(self) -> None:
        """Only the FIRST contradiction is flagged. A new strategy that
        contradicts multiple existing ones is a signal to surface, not
        an excuse to mass-flag."""
        existing_a = Strategy(text="always reply within 10 minutes")
        existing_b = Strategy(text="reply only when there is something to add")
        # Build a stub repo that returns these as the active set, and
        # records the set_status calls.
        flagged_ids: list[uuid.UUID] = []

        class _Repo(StrategyRepository):
            async def query_active(self, *, limit: int = 100):
                return [existing_a, existing_b]

            async def set_status(self, strategy_id, status: str):
                flagged_ids.append(strategy_id)

            async def _insert(self, strategy):
                # Skip the DB write so the test is pure in-memory.
                return strategy

        async def always_contradicts(_existing: str, _new: str) -> bool:
            return True

        new_strategy = Strategy(text="reply at most once a day")
        repo = _Repo(_StubSession())
        await repo.append(new_strategy, is_contradicting=always_contradicts)
        assert len(flagged_ids) == 1
        assert flagged_ids[0] == existing_a.strategy_id

    @pytest.mark.asyncio
    async def test_judge_failure_does_not_block_insert(self) -> None:
        existing = Strategy(text="reply within 10 minutes")
        inserted: list[Strategy] = []

        class _Repo(StrategyRepository):
            async def query_active(self, *, limit: int = 100):
                return [existing]

            async def set_status(self, strategy_id, status: str):
                raise AssertionError("must not flag when judge raises")

            async def _insert(self, strategy):
                inserted.append(strategy)
                return strategy

        async def boom(_a: str, _b: str) -> bool:
            raise RuntimeError("LLM judge unavailable")

        new_strategy = Strategy(text="reply at most once a day")
        repo = _Repo(_StubSession())
        await repo.append(new_strategy, is_contradicting=boom)
        assert inserted == [new_strategy]
