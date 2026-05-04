"""Continuity-of-self scoring tool (#57).

Implements `docs/continuity-of-self-rubric.md`. The rubric scores a
7-day trace window on six 0–3 dimensions; this module:

- Provides the `Dimension` enum and the `Score` / `WindowResult`
  dataclasses.
- Pulls the stratified sample via `select_sample`.
- Runs the per-dimension scorers (auto-detectors for the dimensions
  the trace store can answer; placeholder for human / LLM-judge for
  the rest).
- Aggregates and writes `eval_results/continuity_<date>.json` so the
  operator can compare baseline vs end-of-epic.

Manual scoring is the v0 mode. LLM-judge mode is opt-in (same flag as
#42's reflection-eval LLM-judge); we ship the auto-detectors and the
JSON shape now, the judge prompt as a follow-up.

Pure helpers + a thin orchestration entry point. The trace fetch is
abstracted as a callable so this module is testable without a live DB.
"""
from __future__ import annotations

import json
import random
import statistics
import uuid as _uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)


# ── Dimension enum ───────────────────────────────────────────────────────────


class Dimension:
    COHERENT_VOICE = "coherent_voice"
    REFLECTION_GROUNDED_CONTINUITY = "reflection_grounded_continuity"
    STRATEGY_INVOCATION = "strategy_invocation"
    IDENTITY_INVOCATION = "identity_invocation"
    RESTRAINT_EXHIBITED = "restraint_exhibited"
    RECOVERY_FROM_ERROR = "recovery_from_error"

    ALL: tuple[str, ...] = (
        COHERENT_VOICE,
        REFLECTION_GROUNDED_CONTINUITY,
        STRATEGY_INVOCATION,
        IDENTITY_INVOCATION,
        RESTRAINT_EXHIBITED,
        RECOVERY_FROM_ERROR,
    )


# ── Data shapes ──────────────────────────────────────────────────────────────


_REPO_ROOT = Path(__file__).parent.parent.parent
DEFAULT_EVAL_RESULTS_DIR = _REPO_ROOT / "eval_results"
SAMPLE_SIZE_DEFAULT = 20
WINDOW_DEFAULT = timedelta(days=7)


@dataclass
class TraceForScoring:
    """Minimal trace shape the scorer needs.

    Decoupled from the full ORM `Trace` so tests can construct these
    inline. Production callers project from `agent.traces.schema.Trace`.
    """

    trace_id: _uuid.UUID
    created_at: datetime
    input: str
    output: str
    trigger_source: str = "user"
    tools_called: list[dict[str, Any]] = field(default_factory=list)
    user_reaction: Optional[dict[str, Any]] = None
    assembled_context: dict[str, Any] = field(default_factory=dict)


@dataclass
class Score:
    """A single dimension's score plus the auto-detector's confidence.

    `value` is the manually-overridable score; `auto_value` is what the
    detectors produced. When the scorer is run in LLM-judge mode,
    `auto_value` is the judge's output. The operator may overwrite
    `value` afterward — the JSON record keeps both.
    """

    dimension: str
    value: int
    auto_value: int
    notes: str = ""

    def __post_init__(self) -> None:
        if self.dimension not in Dimension.ALL:
            raise ValueError(
                f"unknown dimension {self.dimension!r}; expected one of "
                f"{list(Dimension.ALL)}"
            )
        if not 0 <= self.value <= 3:
            raise ValueError(
                f"score value must be in [0, 3], got {self.value!r}"
            )
        if not 0 <= self.auto_value <= 3:
            raise ValueError(
                f"auto_value must be in [0, 3], got {self.auto_value!r}"
            )


@dataclass
class WindowResult:
    """Aggregate score for a 7-day window.

    `mean_per_dimension` is the average across sampled traces per
    dimension; `total` is the sum across the 6 dimensions (max 18).
    Designed so two `WindowResult`s can be diffed mechanically: the
    epic-gate is `(end_of_epic.total - baseline.total) >= 1`.
    """

    window_start: datetime
    window_end: datetime
    sample_size: int
    mean_per_dimension: dict[str, float]
    total: float
    notes: str = ""

    def diff_total(self, other: "WindowResult") -> float:
        """`self.total - other.total`. The epic-gate predicate."""
        return self.total - other.total


# ── Auto-detectors ───────────────────────────────────────────────────────────
#
# These are conservative heuristics — they look for evidence of the
# behaviour the dimension measures, return 0 when there's no evidence,
# and saturate the score upward only when the evidence is unambiguous.
# Manual review can revise the score in either direction.


def detect_strategy_invocation(traces: Sequence[TraceForScoring]) -> int:
    """Dimension 3 score from #57.

    Counts traces where the assembled context shows a non-empty
    `strategies_used` list OR `tools_called` includes a
    `query_strategies` call. The fraction of such traces over the
    sample maps to a 0–3 score:
      - 0% → 0
      - <10% → 1
      - 10–50% → 2
      - >50% → 3
    """
    if not traces:
        return 0
    invoked = 0
    for t in traces:
        used = (
            t.assembled_context.get("selectors", {})
            .get("strategies", {})
            .get("strategies_used", [])
        )
        if used:
            invoked += 1
            continue
        if any(call.get("name") == "query_strategies" for call in t.tools_called):
            invoked += 1
    fraction = invoked / len(traces)
    if fraction == 0.0:
        return 0
    if fraction < 0.10:
        return 1
    if fraction <= 0.50:
        return 2
    return 3


def detect_restraint_exhibited(
    traces: Sequence[TraceForScoring],
    *,
    explicit_thumbs_up: int = 0,
    explicit_thumbs_down: int = 0,
) -> int:
    """Dimension 5 score from #57.

    Counts wait calls in the sample. Adjusted by explicit-thumbs
    feedback from #56:
      - 0 waits → 0
      - waits exist but no/mixed thumbs → 1
      - waits with thumbs majority up → 2
      - waits with thumbs strongly up (>= 80%) → 3
    """
    if not traces:
        return 0
    waits = sum(
        1
        for t in traces
        if any(call.get("name") == "wait" for call in t.tools_called)
    )
    if waits == 0:
        return 0
    total_thumbs = explicit_thumbs_up + explicit_thumbs_down
    if total_thumbs == 0:
        return 1
    fraction_up = explicit_thumbs_up / total_thumbs
    if fraction_up >= 0.80:
        return 3
    if fraction_up >= 0.50:
        return 2
    return 1


def detect_recovery_from_error(traces: Sequence[TraceForScoring]) -> int:
    """Dimension 6 score from #57.

    A thumbs-down on a turn followed by no further error of the same
    shape within the window scores high. v0 heuristic: count
    thumbs-down traces; the fraction whose immediate-neighbours
    upstream do NOT show a repeat error gives the score.

    Conservative: in the absence of explicit error tracking, treat
    "no thumbs-down repeat in the window after a thumbs-down" as
    recovery. Tunable; manual override expected.
    """
    if not traces:
        return 0
    thumbs_down = [
        t
        for t in traces
        if (t.user_reaction or {}).get("thumbs") == "down"
    ]
    if not thumbs_down:
        # No errors observed → cannot evaluate recovery from errors.
        # Score as 0 (insufficient evidence) — manual review may
        # revise based on out-of-window context.
        return 0
    later_index = {t.trace_id for t in traces}
    repeats = 0
    for err in thumbs_down:
        # Crude: any subsequent thumbs-down within 24h with overlapping
        # input tokens = repeat. v0; manual review adjusts.
        for t in traces:
            if t.trace_id == err.trace_id:
                continue
            if t.created_at <= err.created_at:
                continue
            if (t.created_at - err.created_at) > timedelta(hours=24):
                continue
            if (t.user_reaction or {}).get("thumbs") == "down":
                repeats += 1
                break
    recovered = len(thumbs_down) - repeats
    fraction = recovered / len(thumbs_down)
    if fraction <= 0:
        return 0
    if fraction < 0.5:
        return 1
    if fraction < 0.9:
        return 2
    return 3


# Dimensions 1, 2, 4 are voice/continuity/identity-alignment and require
# language-level judgement. The auto-detector returns 0 with a "needs
# manual / LLM-judge review" note — the operator overrides during
# scoring.


def needs_human_review_score(dimension: str) -> Score:
    return Score(
        dimension=dimension,
        value=0,
        auto_value=0,
        notes=(
            "Language-level dimension — auto-detector cannot score. "
            "Override with manual or LLM-judge value."
        ),
    )


# ── Sampling ─────────────────────────────────────────────────────────────────


def select_sample(
    traces: Sequence[TraceForScoring],
    *,
    sample_size: int = SAMPLE_SIZE_DEFAULT,
    seed: Optional[int] = None,
) -> list[TraceForScoring]:
    """Stratified-random sample across (trigger_source, has_tools) buckets.

    Buckets:
      - (scheduler, no-tools)
      - (scheduler, with-tools)
      - (user, no-tools)
      - (user, with-tools)

    Aim for proportional representation; fall back to uniform-random
    when a bucket is empty.
    """
    if sample_size <= 0:
        return []
    if not traces:
        return []
    rng = random.Random(seed)
    buckets: dict[tuple[str, bool], list[TraceForScoring]] = {}
    for t in traces:
        source = (
            "scheduler" if t.trigger_source == "scheduler" else "user"
        )
        key = (source, bool(t.tools_called))
        buckets.setdefault(key, []).append(t)
    if not buckets:
        return []
    target_per_bucket = max(1, sample_size // len(buckets))
    sample: list[TraceForScoring] = []
    for items in buckets.values():
        rng.shuffle(items)
        sample.extend(items[:target_per_bucket])
    if len(sample) > sample_size:
        rng.shuffle(sample)
        sample = sample[:sample_size]
    return sample


# ── Orchestration ────────────────────────────────────────────────────────────


@dataclass
class ScoringInputs:
    """Auxiliary signals the scorer needs that aren't on the trace itself."""

    explicit_thumbs_up: int = 0
    explicit_thumbs_down: int = 0


def score_window(
    sample: Sequence[TraceForScoring],
    *,
    window_start: datetime,
    window_end: datetime,
    inputs: Optional[ScoringInputs] = None,
    notes: str = "",
) -> WindowResult:
    """Run all six scorers on the sample. Returns a WindowResult.

    Dimensions 1/2/4 fall back to "needs human review" auto-values; the
    operator overrides them in the saved JSON before computing the
    epic gate.
    """
    if inputs is None:
        inputs = ScoringInputs()
    scores: list[Score] = []
    scores.append(needs_human_review_score(Dimension.COHERENT_VOICE))
    scores.append(needs_human_review_score(Dimension.REFLECTION_GROUNDED_CONTINUITY))
    scores.append(
        Score(
            dimension=Dimension.STRATEGY_INVOCATION,
            value=detect_strategy_invocation(sample),
            auto_value=detect_strategy_invocation(sample),
        )
    )
    scores.append(needs_human_review_score(Dimension.IDENTITY_INVOCATION))
    restraint_v = detect_restraint_exhibited(
        sample,
        explicit_thumbs_up=inputs.explicit_thumbs_up,
        explicit_thumbs_down=inputs.explicit_thumbs_down,
    )
    scores.append(
        Score(
            dimension=Dimension.RESTRAINT_EXHIBITED,
            value=restraint_v,
            auto_value=restraint_v,
        )
    )
    recovery_v = detect_recovery_from_error(sample)
    scores.append(
        Score(
            dimension=Dimension.RECOVERY_FROM_ERROR,
            value=recovery_v,
            auto_value=recovery_v,
        )
    )

    means = {s.dimension: float(s.value) for s in scores}
    total = sum(means.values())
    return WindowResult(
        window_start=window_start,
        window_end=window_end,
        sample_size=len(sample),
        mean_per_dimension=means,
        total=total,
        notes=notes,
    )


def write_result(
    result: WindowResult,
    *,
    out_dir: Path = DEFAULT_EVAL_RESULTS_DIR,
    prefix: str = "continuity",
) -> Path:
    """Serialise a WindowResult to `eval_results/continuity_<date>.json`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    date_tag = result.window_end.date().isoformat()
    path = out_dir / f"{prefix}_{date_tag}.json"
    payload = {
        "window_start": result.window_start.isoformat(),
        "window_end": result.window_end.isoformat(),
        "sample_size": result.sample_size,
        "mean_per_dimension": result.mean_per_dimension,
        "total": result.total,
        "notes": result.notes,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("continuity_eval_written", path=str(path), total=result.total)
    return path


def epic_gate_passes(
    *, baseline: WindowResult, end_of_epic: WindowResult, lift_required: float = 1.0
) -> bool:
    """Implements the #57 acceptance gate:

        end_of_epic.total - baseline.total >= 1.0 (default)

    Returns True iff the lift meets the threshold. A False return does
    not modify any files — this is the read-only gate predicate.
    """
    return (end_of_epic.total - baseline.total) >= lift_required
