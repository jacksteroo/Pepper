"""Repository surface tests for `agent.traces.repository`.

Covers the static guarantees the issue spec asks for (no UPDATE/DELETE
methods, no mutation surface) without requiring a live Postgres.
Behavioural carve-out / validation tests use a stub session so we can
verify guard ordering without a DB.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.traces import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL_DEFAULT,
    TraceRepository,
    TraceTier,
)
from agent.traces.repository import MAX_FILTER_TEXT_LEN


class TestNoMutationSurface:
    """The repository must NOT expose any method whose name implies
    arbitrary mutation. Carve-outs documented in ADR-0005 (set_embedding,
    advance_tier, set_user_reaction) are the only sanctioned writes."""

    forbidden_prefixes: tuple[str, ...] = ("update", "delete", "purge", "drop", "truncate", "remove")

    def test_no_method_with_forbidden_prefix(self) -> None:
        members = inspect.getmembers(TraceRepository, predicate=inspect.isfunction)
        bad = [
            name
            for name, _ in members
            if any(name.startswith(p) for p in self.forbidden_prefixes)
            and not name.startswith("_")
        ]
        assert bad == [], f"forbidden mutation methods exposed: {bad}"

    def test_only_documented_carve_outs_exist(self) -> None:
        # The compactor surface is exactly these three. Any new mutation
        # method must be added intentionally to ADR-0005's carve-out list
        # AND to this test, in lockstep.
        carve_outs = {"set_embedding", "advance_tier", "set_user_reaction"}
        names = {n for n, _ in inspect.getmembers(TraceRepository, predicate=inspect.isfunction)}
        # Must exist.
        assert carve_outs.issubset(names), f"missing carve-out methods: {carve_outs - names}"

    def test_explicit_hasattr_check_for_update(self) -> None:
        # The issue spec's exact wording: "traces_repository.update(...)
        # does not exist as a method (assert via `hasattr`)."
        assert not hasattr(TraceRepository, "update")
        assert not hasattr(TraceRepository, "delete")


class TestPublicMethodSet:
    expected_public: frozenset[str] = frozenset({
        "append",
        "get_by_id",
        "query",
        "find_similar",
        "set_embedding",
        "advance_tier",
        "set_user_reaction",
    })

    def test_public_method_set_is_exhaustive(self) -> None:
        names = {
            n
            for n, _ in inspect.getmembers(TraceRepository, predicate=inspect.isfunction)
            if not n.startswith("_")
        }
        # Adding a new public method on the repository requires updating
        # this set AND ADR-0005 — keeps the surface from drifting.
        assert names == self.expected_public, (
            f"expected {sorted(self.expected_public)}, got {sorted(names)}"
        )


def _stub_session_with_row(row=None) -> AsyncMock:
    """Return an AsyncSession-shaped mock whose `get()` resolves to `row`."""
    session = MagicMock()
    session.get = AsyncMock(return_value=row)
    session.flush = AsyncMock()
    session.add = MagicMock()
    return session


class TestAdvanceTierIdempotency:
    @pytest.mark.asyncio
    async def test_same_tier_is_no_op(self) -> None:
        # Re-running the nightly compression job over a partially-completed
        # batch must NOT raise on rows already at the target tier.
        row = MagicMock()
        row.tier = TraceTier.RECALL.value
        repo = TraceRepository(_stub_session_with_row(row))
        # Should not raise; row.tier should not change.
        await repo.advance_tier("00000000-0000-0000-0000-000000000001", TraceTier.RECALL)
        assert row.tier == TraceTier.RECALL.value

    @pytest.mark.asyncio
    async def test_forward_transition_succeeds(self) -> None:
        row = MagicMock()
        row.tier = TraceTier.WORKING.value
        repo = TraceRepository(_stub_session_with_row(row))
        await repo.advance_tier("00000000-0000-0000-0000-000000000001", TraceTier.RECALL)
        assert row.tier == TraceTier.RECALL.value

    @pytest.mark.asyncio
    async def test_backwards_transition_raises(self) -> None:
        row = MagicMock()
        row.tier = TraceTier.ARCHIVAL.value
        repo = TraceRepository(_stub_session_with_row(row))
        with pytest.raises(ValueError, match="forward-only"):
            await repo.advance_tier(
                "00000000-0000-0000-0000-000000000001",
                TraceTier.WORKING,
            )

    @pytest.mark.asyncio
    async def test_missing_row_raises_lookup(self) -> None:
        repo = TraceRepository(_stub_session_with_row(row=None))
        with pytest.raises(LookupError):
            await repo.advance_tier(
                "00000000-0000-0000-0000-000000000001",
                TraceTier.RECALL,
            )


class TestSetUserReactionValidation:
    @pytest.mark.asyncio
    async def test_unknown_keys_rejected(self) -> None:
        repo = TraceRepository(_stub_session_with_row(MagicMock()))
        with pytest.raises(ValueError, match="unknown user_reaction keys"):
            await repo.set_user_reaction(
                "00000000-0000-0000-0000-000000000001",
                {"thumbs": 1, "rogue": "field"},
            )

    @pytest.mark.asyncio
    async def test_well_formed_payload_passes(self) -> None:
        row = MagicMock()
        row.user_reaction = None
        repo = TraceRepository(_stub_session_with_row(row))
        await repo.set_user_reaction(
            "00000000-0000-0000-0000-000000000001",
            {"thumbs": 1, "followup_correction": False, "source": "explicit"},
        )
        assert row.user_reaction == {
            "thumbs": 1,
            "followup_correction": False,
            "source": "explicit",
        }

    @pytest.mark.asyncio
    async def test_non_dict_rejected(self) -> None:
        repo = TraceRepository(_stub_session_with_row(MagicMock()))
        with pytest.raises(TypeError):
            await repo.set_user_reaction(
                "00000000-0000-0000-0000-000000000001",
                ["not", "a", "dict"],  # type: ignore[arg-type]
            )


class TestSetEmbeddingValidation:
    @pytest.mark.asyncio
    async def test_dimension_mismatch_raises(self) -> None:
        repo = TraceRepository(_stub_session_with_row(MagicMock()))
        with pytest.raises(ValueError, match="dimension"):
            await repo.set_embedding(
                "00000000-0000-0000-0000-000000000001",
                [0.0] * 768,
                EMBEDDING_MODEL_DEFAULT,
            )

    @pytest.mark.asyncio
    async def test_empty_model_version_raises(self) -> None:
        repo = TraceRepository(_stub_session_with_row(MagicMock()))
        with pytest.raises(ValueError, match="embedding_model_version is required"):
            await repo.set_embedding(
                "00000000-0000-0000-0000-000000000001",
                [0.0] * EMBEDDING_DIM,
                "",
            )


class TestQueryCaps:
    @pytest.mark.asyncio
    async def test_contains_text_length_cap(self) -> None:
        # Build a session whose execute() never gets called — the cap
        # raises before it would.
        session = MagicMock()
        session.execute = AsyncMock()
        repo = TraceRepository(session)
        with pytest.raises(ValueError, match="contains_text exceeds"):
            await repo.query(contains_text="x" * (MAX_FILTER_TEXT_LEN + 1))
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_model_selected_length_cap(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock()
        repo = TraceRepository(session)
        with pytest.raises(ValueError, match="model_selected exceeds"):
            await repo.query(model_selected="x" * (MAX_FILTER_TEXT_LEN + 1))


class TestQuerySignature:
    """Verify the surface accepts the documented filters without
    requiring a live DB. The actual SELECT shape is exercised by the
    integration tests."""

    def test_query_accepts_data_sensitivity_filter(self) -> None:
        sig = inspect.signature(TraceRepository.query)
        assert "data_sensitivity" in sig.parameters

    def test_query_accepts_with_payload_flag(self) -> None:
        sig = inspect.signature(TraceRepository.query)
        assert "with_payload" in sig.parameters
        # Default must be False so list-view callers don't pay the jsonb cost.
        assert sig.parameters["with_payload"].default is False

    def test_query_cursor_is_composite(self) -> None:
        # Pagination stability requires composite (created_at, trace_id).
        sig = inspect.signature(TraceRepository.query)
        ann = sig.parameters["cursor"].annotation
        # Exact form: Optional[tuple[datetime, str]]
        assert "tuple" in str(ann).lower()
