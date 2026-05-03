"""Unit tests for `agents.reflector.alerts` — surface + dataclass guards."""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from agents.reflector.alerts import (
    STATUS_DISMISSED,
    STATUS_FILED,
    STATUS_OPEN,
    PatternAlert,
    PatternAlertRepository,
)


class TestNoMutationSurface:
    forbidden_prefixes = ("update", "delete", "purge", "drop", "truncate", "remove")

    def test_no_method_with_forbidden_prefix(self) -> None:
        members = inspect.getmembers(
            PatternAlertRepository, predicate=inspect.isfunction
        )
        bad = [
            name
            for name, _ in members
            if any(name.startswith(p) for p in self.forbidden_prefixes)
            and not name.startswith("_")
        ]
        assert bad == [], f"forbidden mutation methods: {bad}"

    def test_only_documented_public_methods(self) -> None:
        public = sorted(
            name
            for name, _ in inspect.getmembers(
                PatternAlertRepository, predicate=inspect.isfunction
            )
            if not name.startswith("_")
        )
        assert public == [
            "append",
            "get_by_id",
            "list_by_status",
            "list_open",
            "set_status",
        ]


class TestPatternAlertGuards:
    def _make(self, **overrides) -> PatternAlert:
        now = datetime.now(timezone.utc)
        defaults = dict(
            trace_ids=["00000000-0000-0000-0000-000000000001"],
            cluster_size=1,
            window_start=now - timedelta(hours=24),
            window_end=now,
        )
        defaults.update(overrides)
        return PatternAlert(**defaults)

    def test_default_status_is_open(self) -> None:
        a = self._make()
        assert a.status == STATUS_OPEN

    @pytest.mark.parametrize("status", [STATUS_OPEN, STATUS_DISMISSED, STATUS_FILED])
    def test_known_statuses_accepted(self, status: str) -> None:
        a = self._make(status=status)
        assert a.status == status

    def test_unknown_status_rejected(self) -> None:
        with pytest.raises(ValueError, match="status"):
            self._make(status="archived")

    def test_cluster_size_must_match_trace_ids(self) -> None:
        with pytest.raises(ValueError, match="cluster_size"):
            self._make(
                trace_ids=["a", "b", "c"],
                cluster_size=2,
            )

    def test_cluster_size_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            self._make(trace_ids=[], cluster_size=0)

    @pytest.mark.parametrize("conf", [-0.1, 1.1, 2.0, -10.0])
    def test_confidence_must_be_in_unit_interval(self, conf: float) -> None:
        with pytest.raises(ValueError, match="confidence"):
            self._make(confidence=conf)

    def test_inverted_window_rejected(self) -> None:
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="window_end"):
            PatternAlert(
                trace_ids=["a"],
                cluster_size=1,
                window_start=now,
                window_end=now - timedelta(hours=1),
            )

    def test_frozen_alert_cannot_mutate(self) -> None:
        a = self._make()
        with pytest.raises(Exception):  # FrozenInstanceError
            a.status = STATUS_DISMISSED  # type: ignore[misc]
