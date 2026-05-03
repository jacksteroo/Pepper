"""Recurring failure-mode detection (#41 / monitor_operative pattern).

Runs after the daily reflection persists. Pulls "low-quality" traces
from the day — ones the operator thumbs-downed or corrected via a
followup — and clusters them by embedding similarity. Each cluster
of size >= MIN_CLUSTER_SIZE becomes a `pattern_alerts` row.

Design choices for v0 (per the issue spec, "default recommendation:
start with embedding clustering — cheaper, deterministic — add LLM
pass only if clustering misses obvious patterns"):

- Greedy cosine clustering. Each trace either joins the existing
  cluster whose centroid is most similar (above SIMILARITY_THRESHOLD)
  or starts its own.
- No LLM-generated summary in v0; the alert summary is a deterministic
  one-line description naming archetype + size + dominant trigger
  source. The operator clicks the alert in the UI to see the actual
  trace contents (via the existing #34 trace inspector). #42 may
  layer an LLM summary on top once the clustering is calibrated.
- No external clustering library. We compute cosine similarity
  manually from the embedding vectors already on each trace row.
  Avoids pulling sklearn/scipy into the agents/ dependency surface.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

import structlog

from agent.traces import Trace, TraceRepository
from agents.reflector import alerts as ralerts

logger = structlog.get_logger(__name__)

# Embedding similarity (cosine) above this counts as "the same shape
# of failure". Tuned conservatively so unrelated turns do not collapse
# into one cluster; the operator can lower it after the first soak
# week if too many true clusters split. The issue spec asks for a
# manual review of the first week of alerts to calibrate.
SIMILARITY_THRESHOLD: float = 0.85

# Issue spec: surface clusters with >= 3 members.
MIN_CLUSTER_SIZE: int = 3

# Bounded scan: even on a busy day we don't pull thousands of traces
# into memory for the cluster pass. The repository's MAX_QUERY_LIMIT
# is the hard cap; this is the per-pass soft cap.
MAX_TRACES_PER_DETECTION: int = 200


# ── Cosine similarity helpers ────────────────────────────────────────────────


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _norm(v: Sequence[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1]. Returns 0.0 on degenerate inputs."""
    if not a or not b or len(a) != len(b):
        return 0.0
    na = _norm(a)
    nb = _norm(b)
    if na == 0.0 or nb == 0.0:
        return 0.0
    return _dot(a, b) / (na * nb)


# ── Clustering ───────────────────────────────────────────────────────────────


@dataclass
class _Cluster:
    """In-progress cluster as we scan traces. Centroid is the running
    mean of member embeddings, recomputed cheaply on each addition."""

    members: list[Trace]
    centroid: list[float]

    def add(self, trace: Trace) -> None:
        # Caller has verified `trace.embedding is not None`.
        emb = trace.embedding or []
        n = len(self.members)
        # Running mean: c_new = c_old * n/(n+1) + emb / (n+1).
        scale_old = n / (n + 1)
        scale_new = 1.0 / (n + 1)
        self.centroid = [
            self.centroid[i] * scale_old + emb[i] * scale_new
            for i in range(len(self.centroid))
        ]
        self.members.append(trace)


def cluster_traces(
    traces: Sequence[Trace],
    *,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
) -> list[_Cluster]:
    """Greedy cosine clustering of traces with embeddings.

    Order-dependent (a different ordering can produce different
    clusters). The order from the repository is newest-first; we
    accept this for v0 — calibration in week one will tell us
    whether to switch to a stable seed or a proper k-means.
    """
    clusters: list[_Cluster] = []
    for t in traces:
        if not t.embedding:
            continue
        # Find the most-similar existing cluster.
        best_idx: Optional[int] = None
        best_sim = -1.0
        for i, c in enumerate(clusters):
            sim = _cosine(t.embedding, c.centroid)
            if sim > best_sim:
                best_sim = sim
                best_idx = i
        if best_idx is not None and best_sim >= similarity_threshold:
            clusters[best_idx].add(t)
        else:
            clusters.append(_Cluster(members=[t], centroid=list(t.embedding)))
    return clusters


# ── Trace selection ──────────────────────────────────────────────────────────


def _is_low_quality(trace: Trace) -> bool:
    """The "low-quality" filter the issue spec calls out:

    - explicit thumbs-down on the user_reaction, OR
    - followup flagged as a correction.

    Per `docs/trace-schema.md`, `thumbs` is numeric (`-1 | 0 | +1 |
    null`) and `followup_correction` is a boolean. Negative thumbs
    is the thumbs-down case; we accept any negative value defensively
    in case the encoding broadens later.
    """
    reaction = trace.user_reaction or {}
    thumbs = reaction.get("thumbs")
    if isinstance(thumbs, (int, float)) and thumbs < 0:
        return True
    if reaction.get("followup_correction") is True:
        return True
    return False


# ── Summary builder ──────────────────────────────────────────────────────────


def _summarise_cluster(cluster: _Cluster) -> str:
    """One-line description of a cluster.

    No LLM, no trace contents — just deterministic facts the operator
    can use to decide whether to click in. The trace inspector (#34)
    is the place to read the actual content.
    """
    members = cluster.members
    archetypes = sorted({m.archetype.value for m in members})
    triggers = sorted({m.trigger_source.value for m in members})
    thumbs_down = 0
    followup_corrections = 0
    for m in members:
        reaction = m.user_reaction or {}
        thumbs = reaction.get("thumbs")
        if isinstance(thumbs, (int, float)) and thumbs < 0:
            thumbs_down += 1
        if reaction.get("followup_correction") is True:
            followup_corrections += 1
    reactions_summary = (
        f"{thumbs_down}× thumbs-down, "
        f"{followup_corrections}× followup-correction"
    )
    return (
        f"Cluster of {len(members)} similar low-quality turns "
        f"({reactions_summary}); archetype={','.join(archetypes)}; "
        f"trigger={','.join(triggers)}."
    )


def _suggest_action(cluster: _Cluster) -> str:
    """Operator-facing hint about how to act on the alert.

    Mirrors the workflow from the issue spec: dismiss / file as a
    deterministic-intercept candidate / file as an optimizer target.
    """
    return (
        "Review the linked traces. If they share a clear failure "
        "shape, file as either a deterministic-intercept candidate "
        "(see agent/core.py health-metric intercept) or as an "
        "optimizer target for E5 (#44). Otherwise dismiss."
    )


def _confidence(cluster: _Cluster) -> float:
    """Average pairwise cosine to the centroid, clipped to [0, 1].

    Crude but useful for ranking: a cluster whose members are tightly
    packed gets a higher score than one whose members are barely
    above the threshold. The UI sorts by confidence so the operator
    sees the strongest patterns first.
    """
    if not cluster.members:
        return 0.0
    sims: list[float] = []
    for m in cluster.members:
        if not m.embedding:
            continue
        sims.append(_cosine(m.embedding, cluster.centroid))
    if not sims:
        return 0.0
    avg = sum(sims) / len(sims)
    return max(0.0, min(1.0, avg))


# ── Detector entrypoint ──────────────────────────────────────────────────────


async def detect_patterns(
    *,
    window_start: datetime,
    window_end: datetime,
    session_factory,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    min_cluster_size: int = MIN_CLUSTER_SIZE,
    max_traces: int = MAX_TRACES_PER_DETECTION,
) -> list[ralerts.PatternAlert]:
    """Run a single pattern-detection pass over a trace window.

    Returns the list of `PatternAlert` objects the detector
    persisted. Empty list = no clusters of size >= min_cluster_size.
    """
    async with session_factory() as session:
        traces_repo = TraceRepository(session)
        # All trace turns in the window. The detector reads only
        # `user_reaction` and `embedding`; no raw text leaves the
        # box. We deliberately do NOT filter by `data_sensitivity`
        # — failure modes can recur on sanitized turns too (a
        # hallucination on a public-data lookup is still a
        # hallucination), and the issue spec doesn't restrict.
        candidates = await traces_repo.query(
            since=window_start,
            until=window_end,
            limit=max_traces,
            with_payload=True,
        )

    low_quality = [t for t in candidates if _is_low_quality(t)]
    logger.info(
        "pattern_detector_starting",
        n_candidates=len(candidates),
        n_low_quality=len(low_quality),
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
    )
    if not low_quality:
        return []

    clusters = cluster_traces(low_quality, similarity_threshold=similarity_threshold)

    persisted: list[ralerts.PatternAlert] = []
    async with session_factory() as session:
        repo = ralerts.PatternAlertRepository(session)
        for c in clusters:
            if len(c.members) < min_cluster_size:
                continue
            alert = ralerts.PatternAlert(
                trace_ids=[m.trace_id for m in c.members],
                cluster_size=len(c.members),
                window_start=window_start,
                window_end=window_end,
                confidence=_confidence(c),
                summary=_summarise_cluster(c),
                suggested_action=_suggest_action(c),
                metadata_={
                    "similarity_threshold": similarity_threshold,
                    "centroid_norm": _norm(c.centroid),
                },
            )
            await repo.append(alert)
            persisted.append(alert)

    logger.info(
        "pattern_detector_done",
        n_clusters_total=len(clusters),
        n_alerts_filed=len(persisted),
    )
    return persisted
