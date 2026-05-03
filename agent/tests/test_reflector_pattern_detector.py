"""Unit tests for `agents.reflector.pattern_detector`.

The clustering math + low-quality filter are pure functions over the
`Trace` dataclass, so they test directly without mocks. The
`detect_patterns` entrypoint stubs the trace + alert repositories.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.error_classifier import DataSensitivity
from agent.traces.schema import Archetype, Trace, TriggerSource
from agents.reflector import alerts as ralerts
from agents.reflector import pattern_detector as pd


# ── Helpers ─────────────────────────────────────────────────────────────────


_DIM = 1024


def _emb(seed: int) -> list[float]:
    """Deterministic 1024-dim unit-ish vector keyed on a seed.

    Different seeds produce orthogonal-ish vectors; same seed
    produces identical vectors. Lets tests build clusters without
    needing a real embedder.
    """
    import random

    rng = random.Random(seed)
    return [rng.uniform(-1, 1) for _ in range(_DIM)]


def _trace(
    *,
    seed: int,
    user_reaction: Optional[dict] = None,
    archetype: Archetype = Archetype.ORCHESTRATOR,
    trigger_source: TriggerSource = TriggerSource.USER,
    embedding: Optional[list[float]] = None,
) -> Trace:
    chosen_embedding = embedding if embedding is not None else _emb(seed)
    return Trace(
        trigger_source=trigger_source,
        archetype=archetype,
        input=f"in {seed}",
        output=f"out {seed}",
        data_sensitivity=DataSensitivity.LOCAL_ONLY,
        embedding=chosen_embedding if chosen_embedding else None,
        embedding_model_version=(
            "qwen3-embedding:0.6b" if chosen_embedding else None
        ),
        user_reaction=user_reaction,
    )


# ── Cosine math ──────────────────────────────────────────────────────────────


class TestCosineMath:
    def test_identical_vectors(self) -> None:
        v = [1.0, 2.0, 3.0]
        assert pd._cosine(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert pd._cosine(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self) -> None:
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert pd._cosine(a, b) == pytest.approx(-1.0)

    @pytest.mark.parametrize("inputs", [([], [1.0]), ([1.0], []), ([1.0, 2.0], [1.0])])
    def test_degenerate_inputs_return_zero(
        self, inputs: tuple[list[float], list[float]]
    ) -> None:
        a, b = inputs
        assert pd._cosine(a, b) == 0.0

    def test_zero_norm_vector_returns_zero(self) -> None:
        assert pd._cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


# ── Low-quality filter ──────────────────────────────────────────────────────


class TestLowQualityFilter:
    """Per `docs/trace-schema.md`, `thumbs` is numeric (-1/0/+1) and
    `followup_correction` is boolean. The filter must read both
    correctly."""

    def test_thumbs_down_negative_is_low_quality(self) -> None:
        t = _trace(seed=1, user_reaction={"thumbs": -1})
        assert pd._is_low_quality(t)

    def test_thumbs_up_positive_is_not_low_quality(self) -> None:
        t = _trace(seed=1, user_reaction={"thumbs": 1})
        assert not pd._is_low_quality(t)

    def test_thumbs_zero_is_not_low_quality(self) -> None:
        t = _trace(seed=1, user_reaction={"thumbs": 0})
        assert not pd._is_low_quality(t)

    def test_followup_correction_true_is_low_quality(self) -> None:
        t = _trace(seed=1, user_reaction={"followup_correction": True})
        assert pd._is_low_quality(t)

    def test_followup_correction_false_is_not_low_quality(self) -> None:
        t = _trace(seed=1, user_reaction={"followup_correction": False})
        assert not pd._is_low_quality(t)

    def test_no_reaction_is_not_low_quality(self) -> None:
        t = _trace(seed=1, user_reaction=None)
        assert not pd._is_low_quality(t)

    def test_empty_reaction_is_not_low_quality(self) -> None:
        t = _trace(seed=1, user_reaction={})
        assert not pd._is_low_quality(t)


# ── Clustering ──────────────────────────────────────────────────────────────


class TestClusterTraces:
    def test_identical_embeddings_cluster_together(self) -> None:
        v = _emb(7)
        traces = [_trace(seed=i, embedding=v) for i in range(5)]
        clusters = pd.cluster_traces(traces, similarity_threshold=0.99)
        assert len(clusters) == 1
        assert len(clusters[0].members) == 5

    def test_distinct_seeds_split_into_separate_clusters(self) -> None:
        # With high seeds and a high threshold, random vectors do not
        # cluster together. Exact cluster count is implementation-
        # dependent on the rng draw, but it MUST be > 1.
        traces = [_trace(seed=10 + i) for i in range(10)]
        clusters = pd.cluster_traces(traces, similarity_threshold=0.95)
        assert len(clusters) > 1

    def test_traces_without_embedding_are_skipped(self) -> None:
        traces = [
            _trace(seed=1, embedding=[]),  # empty embedding → skipped
            _trace(seed=2, embedding=[]),
        ]
        clusters = pd.cluster_traces(traces, similarity_threshold=0.5)
        assert clusters == []

    def test_low_threshold_collapses_everything(self) -> None:
        # Any positive cosine clears threshold=-1.0, so all traces
        # land in the first cluster regardless of similarity.
        traces = [_trace(seed=20 + i) for i in range(6)]
        clusters = pd.cluster_traces(traces, similarity_threshold=-1.0)
        assert len(clusters) == 1
        assert len(clusters[0].members) == 6


# ── Cluster summarisers ────────────────────────────────────────────────────


class TestSummariseCluster:
    def test_summary_does_not_leak_trace_text(self) -> None:
        v = _emb(99)
        traces = [
            _trace(seed=99, embedding=v, user_reaction={"thumbs": -1})
            for _ in range(3)
        ]
        cluster = pd._Cluster(members=traces, centroid=v)
        s = pd._summarise_cluster(cluster)
        # The summary names archetype, trigger, reaction counts —
        # but never the trace input/output. The trace inspector is
        # the place to read content.
        assert "in 99" not in s
        assert "out 99" not in s
        assert "Cluster of 3" in s
        assert "thumbs-down" in s

    def test_confidence_is_in_unit_interval(self) -> None:
        v = _emb(50)
        traces = [_trace(seed=50, embedding=v) for _ in range(4)]
        cluster = pd._Cluster(members=traces, centroid=v)
        c = pd._confidence(cluster)
        assert 0.0 <= c <= 1.0


# ── Entrypoint ──────────────────────────────────────────────────────────────


def _factory_with(
    *,
    candidates: Sequence[Trace],
    sink: list[ralerts.PatternAlert],
):
    """Build an async-context-managed session-factory stub.

    Patches the trace + alert repositories so the real DB never
    touches. Anything passed in `candidates` is returned by the
    trace query; `sink` collects appended alerts.
    """

    @asynccontextmanager
    async def _factory():
        with (
            patch("agents.reflector.pattern_detector.TraceRepository") as TR,
            patch.object(ralerts, "PatternAlertRepository") as AR,
        ):
            tr = MagicMock()
            tr.query = AsyncMock(return_value=list(candidates))
            TR.return_value = tr

            ar = MagicMock()

            async def _append(alert):
                sink.append(alert)
                return alert

            ar.append = AsyncMock(side_effect=_append)
            AR.return_value = ar
            yield None

    return _factory


@pytest.mark.asyncio
class TestDetectPatternsEntrypoint:
    async def test_no_low_quality_traces_files_no_alerts(self) -> None:
        candidates = [_trace(seed=i, user_reaction={"thumbs": 1}) for i in range(5)]
        sink: list[ralerts.PatternAlert] = []
        factory = _factory_with(candidates=candidates, sink=sink)

        out = await pd.detect_patterns(
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 2, tzinfo=timezone.utc),
            session_factory=factory,
        )
        assert out == []
        assert sink == []

    async def test_three_thumbs_down_at_same_embedding_files_one_alert(self) -> None:
        v = _emb(123)
        candidates = [
            _trace(seed=123, embedding=v, user_reaction={"thumbs": -1})
            for _ in range(3)
        ]
        sink: list[ralerts.PatternAlert] = []
        factory = _factory_with(candidates=candidates, sink=sink)

        out = await pd.detect_patterns(
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 2, tzinfo=timezone.utc),
            session_factory=factory,
            similarity_threshold=0.99,
        )
        assert len(out) == 1
        assert len(sink) == 1
        assert sink[0].cluster_size == 3
        assert sink[0].status == ralerts.STATUS_OPEN
        # Trace IDs are recorded so the operator can drill in via
        # the existing #34 trace inspector.
        assert len(sink[0].trace_ids) == 3

    async def test_clusters_below_min_size_are_not_filed(self) -> None:
        v = _emb(7)
        # Only 2 thumbs-down at the same embedding — below
        # MIN_CLUSTER_SIZE=3.
        candidates = [
            _trace(seed=7, embedding=v, user_reaction={"thumbs": -1})
            for _ in range(2)
        ]
        sink: list[ralerts.PatternAlert] = []
        factory = _factory_with(candidates=candidates, sink=sink)

        out = await pd.detect_patterns(
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 2, tzinfo=timezone.utc),
            session_factory=factory,
            similarity_threshold=0.99,
        )
        assert out == []
        assert sink == []
