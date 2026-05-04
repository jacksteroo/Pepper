"""Tests for `agents.reflector.continuity_eval` (#57).

Covers the auto-detectors, the sampling helper, the orchestrator, and
the epic-gate predicate. The rubric document itself is the test plan
for the manual / LLM-judge dimensions; the auto-detectors here have
deterministic test inputs.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents.reflector.continuity_eval import (
    Dimension,
    Score,
    ScoringInputs,
    TraceForScoring,
    WindowResult,
    detect_recovery_from_error,
    detect_restraint_exhibited,
    detect_strategy_invocation,
    epic_gate_passes,
    needs_human_review_score,
    score_window,
    select_sample,
    write_result,
)


def _ts(days_ago: int = 0, hours_ago: int = 0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)


def _trace(
    *,
    input_text: str = "",
    output: str = "",
    source: str = "user",
    tools: list[dict] | None = None,
    user_reaction: dict | None = None,
    selectors: dict | None = None,
    days_ago: int = 0,
    hours_ago: int = 0,
) -> TraceForScoring:
    return TraceForScoring(
        trace_id=uuid.uuid4(),
        created_at=_ts(days_ago=days_ago, hours_ago=hours_ago),
        input=input_text,
        output=output,
        trigger_source=source,
        tools_called=tools or [],
        user_reaction=user_reaction,
        assembled_context={"selectors": selectors or {}},
    )


# ── Score dataclass ──────────────────────────────────────────────────────────


class TestScoreDataclass:
    def test_invalid_dimension_rejected(self) -> None:
        with pytest.raises(ValueError, match="dimension"):
            Score(dimension="bogus", value=1, auto_value=1)

    def test_value_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="score value"):
            Score(dimension=Dimension.COHERENT_VOICE, value=4, auto_value=1)
        with pytest.raises(ValueError, match="score value"):
            Score(dimension=Dimension.COHERENT_VOICE, value=-1, auto_value=1)

    def test_auto_value_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="auto_value"):
            Score(dimension=Dimension.COHERENT_VOICE, value=1, auto_value=5)


# ── detect_strategy_invocation ───────────────────────────────────────────────


class TestStrategyInvocation:
    def test_zero_invocations_scores_zero(self) -> None:
        traces = [_trace() for _ in range(5)]
        assert detect_strategy_invocation(traces) == 0

    def test_low_fraction_scores_one(self) -> None:
        traces = [_trace() for _ in range(20)]
        traces[0] = _trace(tools=[{"name": "query_strategies", "args": {}}])
        # 1 / 20 = 5% < 10% → 1
        assert detect_strategy_invocation(traces) == 1

    def test_mid_fraction_scores_two(self) -> None:
        traces = [
            _trace(tools=[{"name": "query_strategies", "args": {}}]) for _ in range(5)
        ] + [_trace() for _ in range(5)]
        # 5 / 10 = 50% → 2
        assert detect_strategy_invocation(traces) == 2

    def test_high_fraction_scores_three(self) -> None:
        traces = [
            _trace(tools=[{"name": "query_strategies", "args": {}}]) for _ in range(8)
        ] + [_trace() for _ in range(2)]
        # 8 / 10 = 80% → 3
        assert detect_strategy_invocation(traces) == 3

    def test_provenance_strategies_used_counts_too(self) -> None:
        traces = [
            _trace(
                selectors={
                    "strategies": {
                        "strategies_used": [{"strategy_id": "x", "score": 0.5}]
                    }
                }
            )
            for _ in range(6)
        ] + [_trace() for _ in range(4)]
        # 6 / 10 = 60% → 3
        assert detect_strategy_invocation(traces) == 3


# ── detect_restraint_exhibited ───────────────────────────────────────────────


class TestRestraintExhibited:
    def test_no_waits_scores_zero(self) -> None:
        traces = [_trace() for _ in range(5)]
        assert detect_restraint_exhibited(traces) == 0

    def test_waits_no_thumbs_scores_one(self) -> None:
        traces = [_trace(tools=[{"name": "wait", "args": {"reason": "ok"}}])]
        assert detect_restraint_exhibited(traces) == 1

    def test_waits_strong_thumbs_up_scores_three(self) -> None:
        traces = [_trace(tools=[{"name": "wait", "args": {"reason": "ok"}}])]
        assert (
            detect_restraint_exhibited(
                traces, explicit_thumbs_up=8, explicit_thumbs_down=1
            )
            == 3
        )

    def test_waits_majority_thumbs_up_scores_two(self) -> None:
        traces = [_trace(tools=[{"name": "wait", "args": {"reason": "ok"}}])]
        assert (
            detect_restraint_exhibited(
                traces, explicit_thumbs_up=3, explicit_thumbs_down=2
            )
            == 2
        )


# ── detect_recovery_from_error ───────────────────────────────────────────────


class TestRecoveryFromError:
    def test_no_thumbs_down_scores_zero(self) -> None:
        # Insufficient evidence — manual review may revise.
        traces = [_trace() for _ in range(5)]
        assert detect_recovery_from_error(traces) == 0

    def test_recovery_no_repeat_scores_three(self) -> None:
        traces = [
            _trace(user_reaction={"thumbs": "down"}, hours_ago=24),
            # No subsequent thumbs-down in window.
            _trace(hours_ago=12),
            _trace(),
        ]
        assert detect_recovery_from_error(traces) == 3

    def test_partial_repeat_scores_two(self) -> None:
        # err1 (24h ago) is followed by err2 (12h ago) within 24h —
        # err1 counts as "had a repeat"; err2 has no later thumbs-down
        # so it's "recovered." Recovered fraction = 1/2 = 0.5 → score 2.
        traces = [
            _trace(user_reaction={"thumbs": "down"}, hours_ago=24),
            _trace(user_reaction={"thumbs": "down"}, hours_ago=12),
        ]
        assert detect_recovery_from_error(traces) == 2

    def test_long_repeat_chain_scores_one(self) -> None:
        # 4 thumbs-down within 24h of each other in chain. The first
        # 3 each have a repeat within 24h; the last has none. Recovered
        # fraction = 1/4 = 0.25 → score 1.
        traces = [
            _trace(user_reaction={"thumbs": "down"}, hours_ago=24),
            _trace(user_reaction={"thumbs": "down"}, hours_ago=18),
            _trace(user_reaction={"thumbs": "down"}, hours_ago=12),
            _trace(user_reaction={"thumbs": "down"}, hours_ago=6),
        ]
        assert detect_recovery_from_error(traces) == 1


# ── select_sample ────────────────────────────────────────────────────────────


class TestSelectSample:
    def test_small_corpus_returns_all(self) -> None:
        traces = [_trace() for _ in range(3)]
        sample = select_sample(traces, sample_size=20, seed=1)
        assert len(sample) == 3

    def test_caps_at_sample_size(self) -> None:
        traces = [_trace() for _ in range(50)]
        sample = select_sample(traces, sample_size=20, seed=1)
        assert len(sample) <= 20

    def test_zero_sample_size_returns_empty(self) -> None:
        traces = [_trace() for _ in range(5)]
        assert select_sample(traces, sample_size=0) == []

    def test_strata_represented_when_possible(self) -> None:
        # Mix of (scheduler, tools) buckets.
        traces = [
            _trace(source="scheduler") for _ in range(5)
        ] + [
            _trace(source="user", tools=[{"name": "wait"}]) for _ in range(5)
        ]
        sample = select_sample(traces, sample_size=10, seed=1)
        sources = {t.trigger_source for t in sample}
        assert sources == {"scheduler", "user"}


# ── score_window + epic_gate_passes ──────────────────────────────────────────


class TestScoreWindow:
    def test_total_is_sum_of_dimension_scores(self) -> None:
        traces = [
            _trace(tools=[{"name": "query_strategies"}]) for _ in range(8)
        ] + [
            _trace(tools=[{"name": "wait"}]) for _ in range(2)
        ]
        result = score_window(
            traces,
            window_start=_ts(days_ago=7),
            window_end=_ts(),
            inputs=ScoringInputs(explicit_thumbs_up=5, explicit_thumbs_down=0),
        )
        assert result.sample_size == 10
        # Strategy invocation should saturate at 3 with 80% rate.
        assert result.mean_per_dimension[Dimension.STRATEGY_INVOCATION] == 3
        # Restraint should hit 3 with strong thumbs up.
        assert result.mean_per_dimension[Dimension.RESTRAINT_EXHIBITED] == 3
        assert result.total == sum(result.mean_per_dimension.values())


class TestEpicGate:
    def test_lift_at_threshold_passes(self) -> None:
        baseline = WindowResult(
            window_start=_ts(days_ago=14),
            window_end=_ts(days_ago=7),
            sample_size=10,
            mean_per_dimension={d: 1.0 for d in Dimension.ALL},
            total=6.0,
        )
        end = WindowResult(
            window_start=_ts(days_ago=7),
            window_end=_ts(),
            sample_size=10,
            mean_per_dimension={d: 1.5 for d in Dimension.ALL},
            total=7.0,
        )
        # Lift = 1.0 (default threshold) — passes.
        assert epic_gate_passes(baseline=baseline, end_of_epic=end) is True

    def test_lift_below_threshold_fails(self) -> None:
        baseline = WindowResult(
            window_start=_ts(days_ago=14),
            window_end=_ts(days_ago=7),
            sample_size=10,
            mean_per_dimension={d: 1.0 for d in Dimension.ALL},
            total=6.0,
        )
        end = WindowResult(
            window_start=_ts(days_ago=7),
            window_end=_ts(),
            sample_size=10,
            mean_per_dimension={d: 1.0 for d in Dimension.ALL},
            total=6.5,
        )
        # Lift = 0.5 < 1.0 — does not pass.
        assert epic_gate_passes(baseline=baseline, end_of_epic=end) is False


# ── write_result ─────────────────────────────────────────────────────────────


class TestWriteResult:
    def test_writes_json_with_correct_shape(self, tmp_path: Path) -> None:
        result = WindowResult(
            window_start=_ts(days_ago=7),
            window_end=_ts(),
            sample_size=10,
            mean_per_dimension={d: 1.5 for d in Dimension.ALL},
            total=9.0,
            notes="baseline",
        )
        path = write_result(result, out_dir=tmp_path, prefix="continuity")
        assert path.exists()
        payload = json.loads(path.read_text())
        assert payload["sample_size"] == 10
        assert payload["total"] == 9.0
        assert payload["notes"] == "baseline"
        assert set(payload["mean_per_dimension"]) == set(Dimension.ALL)
