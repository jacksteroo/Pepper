"""Static + behavioural tests for `agent.identity_diffs` (#52).

Covers the dataclass invariants and the public method surface. The
end-to-end propose → approve → apply cycle is in
`test_identity_diffs_flow.py` against a stub session.
"""
from __future__ import annotations

import inspect
import uuid

import pytest

from agent.identity_diffs import (
    IdentityDiff,
    IdentityDiffRepository,
    IdentityDiffStatus,
)


class TestDiffDataclass:
    def test_default_status_is_pending(self) -> None:
        diff = IdentityDiff(proposed_text="new self.")
        assert diff.status == IdentityDiffStatus.PENDING

    def test_empty_text_rejected(self) -> None:
        with pytest.raises(ValueError, match="proposed_text cannot be empty"):
            IdentityDiff(proposed_text="")
        with pytest.raises(ValueError, match="proposed_text cannot be empty"):
            IdentityDiff(proposed_text="   \n")

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValueError, match="status must be one of"):
            IdentityDiff(proposed_text="x", status="archived")


class TestRepositorySurface:
    """Lock the public method set so the surface cannot drift."""

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
            for n, _ in inspect.getmembers(IdentityDiffRepository, predicate=inspect.isfunction)
            if not n.startswith("_")
        }
        assert names == self.expected_public

    def test_no_destructive_paths(self) -> None:
        forbidden = ("update_text", "edit", "rewrite", "delete", "purge", "drop")
        names = {n for n, _ in inspect.getmembers(IdentityDiffRepository)}
        bad = [n for n in names if any(n.startswith(p) for p in forbidden)]
        assert bad == []
