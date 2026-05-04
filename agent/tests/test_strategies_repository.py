"""Tests for agent.strategies.repository — Epic 06 (#53 / #54).

Four test groups matching the acceptance criteria:
  1. test_strategies_repository_roundtrip
  2. test_strategies_contradiction_detection
  3. test_query_strategies_tool
  4. test_propose_strategy_update_never_writes_directly

All DB tests use a stub AsyncSession — no live Postgres required.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.strategies.models import StrategyRow
from agent.strategies.repository import (
    STATUS_ACTIVE,
    STATUS_FLAGGED,
    STATUS_SUPERSEDED,
    StrategyRepository,
    _simple_negation_overlap,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_strategy(
    text: str = "test strategy",
    status: str = STATUS_ACTIVE,
    version: int = 1,
    parent_id: uuid.UUID | None = None,
    embedding: list[float] | None = None,
) -> StrategyRow:
    row = StrategyRow()
    row.strategy_id = uuid.uuid4()
    row.text = text
    row.version = version
    row.parent_strategy_id = parent_id
    row.created_by = "jack"
    row.confidence = 0.7
    row.usage_count = 0
    row.status = status
    row.embedding = embedding
    return row


def _make_session(get_result=None, scalars_result=None):
    """Stub AsyncSession that returns ``get_result`` for .get() and
    ``scalars_result`` for execute().scalars().all()."""
    session = AsyncMock()

    async def _get(model, pk):
        return get_result

    session.get.side_effect = _get
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    # execute().scalars().all()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = scalars_result or []
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_mock
    session.execute = AsyncMock(return_value=execute_result)

    return session


# ── 1. Repository round-trips ──────────────────────────────────────────────


class TestRepositoryRoundtrip:
    """insert / query / version round-trips work without a live DB."""

    @pytest.mark.asyncio
    async def test_append_calls_add_and_flush(self):
        """append() calls session.add + flush + refresh."""
        row = _make_strategy()
        session = _make_session()
        repo = StrategyRepository(session)

        result = await repo.append(row)

        session.add.assert_called_once_with(row)
        session.flush.assert_awaited_once()
        session.refresh.assert_awaited_once_with(row)
        assert result is row

    @pytest.mark.asyncio
    async def test_flag_sets_status(self):
        """flag() changes status to flagged."""
        row = _make_strategy()
        session = _make_session(get_result=row)
        repo = StrategyRepository(session)

        await repo.flag(row.strategy_id, reason="test reason")

        assert row.status == STATUS_FLAGGED
        session.flush.assert_awaited()

    @pytest.mark.asyncio
    async def test_supersede_sets_status(self):
        """supersede() marks the old row as superseded."""
        old_row = _make_strategy()
        new_id = uuid.uuid4()
        session = _make_session(get_result=old_row)
        repo = StrategyRepository(session)

        await repo.supersede(old_row.strategy_id, new_id)

        assert old_row.status == STATUS_SUPERSEDED
        session.flush.assert_awaited()

    @pytest.mark.asyncio
    async def test_supersede_idempotent(self):
        """supersede() is a no-op if already superseded."""
        row = _make_strategy(status=STATUS_SUPERSEDED)
        session = _make_session(get_result=row)
        repo = StrategyRepository(session)

        await repo.supersede(row.strategy_id, uuid.uuid4())

        # flush should NOT have been called (no mutation needed)
        session.flush.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_flag_raises_on_missing(self):
        """flag() raises LookupError when strategy not found."""
        session = _make_session(get_result=None)
        repo = StrategyRepository(session)

        with pytest.raises(LookupError):
            await repo.flag(uuid.uuid4(), reason="gone")

    @pytest.mark.asyncio
    async def test_query_all_active_filters_by_status(self):
        """query_all_active() uses a WHERE status='active' filter."""
        session = _make_session(scalars_result=[])
        repo = StrategyRepository(session)
        await repo.query_all_active()

        # execute was called — we don't inspect the exact SQL object
        # but we do verify it was called at all.
        session.execute.assert_awaited_once()

    def test_version_chain_parent_id(self):
        """A versioned strategy carries parent_strategy_id."""
        parent = _make_strategy(version=1)
        child = _make_strategy(
            text="updated strategy",
            version=2,
            parent_id=parent.strategy_id,
        )
        assert child.parent_strategy_id == parent.strategy_id
        assert child.version == 2


# ── 2. Contradiction detection ────────────────────────────────────────────


class TestContradictionDetection:
    """detect_contradiction heuristic / LLM path."""

    def test_simple_negation_overlap_detects_flip(self):
        """Two strategies with shared terms, one negated → True."""
        a = "check calendar data before answering schedule questions"
        b = "never check calendar data when answering schedule questions"
        assert _simple_negation_overlap(a, b) is True

    def test_simple_negation_overlap_unrelated_false(self):
        """Completely different topics → False."""
        a = "reply concisely to email queries"
        b = "never eat lunch at the desk"
        assert _simple_negation_overlap(a, b) is False

    def test_simple_negation_overlap_both_negated_false(self):
        """Both negated — still a contradiction in reality but the
        heuristic requires one side to be affirmative."""
        a = "never answer without checking facts"
        b = "never skip fact checking"
        # Both use negation — not flagged as a contradiction by the simple rule.
        assert _simple_negation_overlap(a, b) is False

    @pytest.mark.asyncio
    async def test_detect_contradiction_empty_list_returns_none(self):
        """No existing strategies → no contradiction."""
        session = _make_session()
        repo = StrategyRepository(session)
        result = await repo.detect_contradiction("any strategy", existing=[])
        assert result is None

    @pytest.mark.asyncio
    async def test_detect_contradiction_no_match_returns_none(self):
        """Unrelated strategies → no contradiction detected."""
        existing = [
            _make_strategy("reply concisely to emails"),
            _make_strategy("check calendar first"),
        ]
        session = _make_session()
        repo = StrategyRepository(session)
        result = await repo.detect_contradiction(
            "always use bullet points for lists",
            existing=existing,
            llm_client=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_detect_contradiction_found(self):
        """A negating strategy is detected."""
        conflicting = _make_strategy(
            "always check calendar before answering schedule questions"
        )
        existing = [conflicting]
        session = _make_session()
        repo = StrategyRepository(session)
        result = await repo.detect_contradiction(
            "never check calendar when answering schedule questions",
            existing=existing,
            llm_client=None,
        )
        assert result is conflicting

    @pytest.mark.asyncio
    async def test_detect_contradiction_skips_non_active(self):
        """Superseded strategies are not checked for contradiction."""
        old = _make_strategy(
            "always check calendar before answering schedule questions",
            status=STATUS_SUPERSEDED,
        )
        session = _make_session()
        repo = StrategyRepository(session)
        result = await repo.detect_contradiction(
            "never check calendar when answering schedule questions",
            existing=[old],
            llm_client=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_detect_contradiction_llm_path(self):
        """LLM path is tried first; YES response → contradiction detected."""
        conflicting = _make_strategy("always check calendar")
        existing = [conflicting]
        session = _make_session()
        repo = StrategyRepository(session)

        llm = AsyncMock()
        llm.chat = AsyncMock(return_value={"content": "YES"})

        result = await repo.detect_contradiction(
            "never check calendar",
            existing=existing,
            llm_client=llm,
        )
        assert result is conflicting
        llm.chat.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_detect_contradiction_llm_no_returns_none(self):
        """LLM says NO → no contradiction."""
        existing = [_make_strategy("always check calendar")]
        session = _make_session()
        repo = StrategyRepository(session)

        llm = AsyncMock()
        llm.chat = AsyncMock(return_value={"content": "NO"})

        result = await repo.detect_contradiction(
            "never check calendar",
            existing=existing,
            llm_client=llm,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_detect_contradiction_llm_failure_falls_back(self):
        """LLM error → falls back to keyword heuristic."""
        conflicting = _make_strategy("always check calendar before answering")
        existing = [conflicting]
        session = _make_session()
        repo = StrategyRepository(session)

        llm = AsyncMock()
        llm.chat = AsyncMock(side_effect=RuntimeError("timeout"))

        result = await repo.detect_contradiction(
            "never check calendar when answering schedule questions",
            existing=existing,
            llm_client=llm,
        )
        # Falls back to heuristic which should detect the flip.
        assert result is conflicting


# ── 3. query_strategies tool ──────────────────────────────────────────────


class TestQueryStrategesTool:
    """The query_strategies tool returns ranked strategies."""

    @pytest.mark.asyncio
    async def test_returns_strategies_on_success(self):
        """Tool returns ranked strategy dicts when DB and embed succeed."""
        from agent.strategies_tools import execute_query_strategies

        row = _make_strategy(text="check calendar first", embedding=[0.1] * 768)
        row.usage_count = 3
        row.confidence = 0.8

        # db_factory context manager returns a session with our row
        mock_session = _make_session(scalars_result=[row])
        mock_session.commit = AsyncMock()

        db_factory = MagicMock()
        db_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        db_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        llm = AsyncMock()
        llm.embed = AsyncMock(return_value=[0.1] * 768)

        result = await execute_query_strategies(
            {"situation": "schedule conflict", "top_k": 3},
            db_factory=db_factory,
            llm_client=llm,
        )

        assert "strategies" in result
        assert result["count"] == 1
        strat = result["strategies"][0]
        assert strat["text"] == "check calendar first"
        assert "strategy_id" in strat
        assert "confidence" in strat
        assert "usage_count" in strat

    @pytest.mark.asyncio
    async def test_missing_situation_returns_error(self):
        """Missing 'situation' argument returns an error dict."""
        from agent.strategies_tools import execute_query_strategies

        result = await execute_query_strategies(
            {},
            db_factory=None,
            llm_client=None,
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_embed_failure_falls_back_to_all_active(self):
        """Embedding failure triggers fallback to query_all_active."""
        from agent.strategies_tools import execute_query_strategies

        row = _make_strategy(text="fallback strategy")
        mock_session = _make_session(scalars_result=[row])
        mock_session.commit = AsyncMock()

        db_factory = MagicMock()
        db_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        db_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        llm = AsyncMock()
        llm.embed = AsyncMock(side_effect=RuntimeError("ollama down"))

        result = await execute_query_strategies(
            {"situation": "anything"},
            db_factory=db_factory,
            llm_client=llm,
        )

        # Falls back gracefully — still returns strategies
        assert "strategies" in result


# ── 4. propose_strategy_update never writes directly ─────────────────────


class TestProposeStrategyUpdateNeverWritesDirectly:
    """propose_strategy_update must enqueue, never write to DB."""

    @pytest.mark.asyncio
    async def test_enqueues_pending_action(self):
        """Tool creates a pending action, not a DB write."""
        from agent.pending_actions import PendingActionsQueue
        from agent.strategies_tools import execute_propose_strategy_update

        queue = PendingActionsQueue()

        result = await execute_propose_strategy_update(
            {
                "new_text": "Always greet Jack by name",
                "reason": "Jack prefers a personal greeting",
            },
            pending_actions=queue,
        )

        assert result["status"] == "pending"
        assert "action_id" in result
        # The action is in the queue
        pending = queue.list_pending()
        assert len(pending) == 1
        assert pending[0]["tool_name"] == "apply_strategy_update"

    @pytest.mark.asyncio
    async def test_does_not_touch_db(self):
        """No DB session is opened by propose_strategy_update."""
        from agent.pending_actions import PendingActionsQueue
        from agent.strategies_tools import execute_propose_strategy_update

        queue = PendingActionsQueue()

        # If DB were accessed this would fail (no db_factory provided)
        result = await execute_propose_strategy_update(
            {
                "new_text": "Prioritize urgent messages",
                "reason": "pattern from recent traces",
            },
            pending_actions=queue,
        )

        assert "error" not in result
        assert result["status"] == "pending"

    @pytest.mark.asyncio
    async def test_strategy_id_included_when_provided(self):
        """strategy_id flows into the pending action args."""
        from agent.pending_actions import PendingActionsQueue
        from agent.strategies_tools import execute_propose_strategy_update

        queue = PendingActionsQueue()
        old_id = str(uuid.uuid4())

        await execute_propose_strategy_update(
            {
                "strategy_id": old_id,
                "new_text": "Updated strategy text",
                "reason": "refinement",
            },
            pending_actions=queue,
        )

        pending = queue.list_pending()
        assert len(pending) == 1
        assert pending[0]["args"]["strategy_id"] == old_id

    @pytest.mark.asyncio
    async def test_missing_new_text_returns_error(self):
        """Missing 'new_text' returns an error without queuing."""
        from agent.pending_actions import PendingActionsQueue
        from agent.strategies_tools import execute_propose_strategy_update

        queue = PendingActionsQueue()

        result = await execute_propose_strategy_update(
            {"reason": "some reason"},
            pending_actions=queue,
        )

        assert "error" in result
        assert len(queue.list_pending()) == 0

    @pytest.mark.asyncio
    async def test_missing_reason_returns_error(self):
        """Missing 'reason' returns an error without queuing."""
        from agent.pending_actions import PendingActionsQueue
        from agent.strategies_tools import execute_propose_strategy_update

        queue = PendingActionsQueue()

        result = await execute_propose_strategy_update(
            {"new_text": "some strategy"},
            pending_actions=queue,
        )

        assert "error" in result
        assert len(queue.list_pending()) == 0
