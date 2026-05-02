"""Retrieval evaluation runner — Epic 02, issue #30.

Loads a query set, runs each query against a pluggable retriever, and computes
Recall@K, MRR, and per-category breakdowns. Used in two contexts:

* Unit test (`test_retrieval_eval.py`) against a synthetic fixture corpus,
  so CI can verify the runner without a real DB or RAW_PERSONAL eval set.
* Local script (`scripts/run_retrieval_eval.py`) against the live memory DB
  and the gitignored real eval set, producing the markdown reports under
  `eval_results/` that gate the epic.

The retriever interface is intentionally minimal — a coroutine that takes a
query string and a `k`, returns a list of memory IDs ordered best-first. Both
the production `MemoryManager` and the test fixture conform to it.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Awaitable, Callable, Iterable

# A retriever is any async callable: (eval_query, k) -> list[memory_id]
# Takes the full EvalQuery (not just the query string) so per-query knobs
# like #29's time_window override flow through without a signature change.
Retriever = Callable[["EvalQuery", int], Awaitable[list[int]]]

CATEGORIES = ("factual", "person", "temporal", "open_loop", "hybrid")
DEFAULT_K_VALUES: tuple[int, ...] = (1, 3, 5, 10)
DEFAULT_GATE_THRESHOLD_PP = 10.0  # epic gate per issue #30


@dataclass(frozen=True)
class EvalQuery:
    query: str
    expected_ids: tuple[int, ...]
    category: str
    notes: str = ""
    # Optional per-query retrieval knobs. Subissues #27/#28/#29 read these
    # via the EvalQuery argument to the Retriever protocol; unused fields
    # default to None so existing entries stay valid.
    time_window_days: int | None = None  # #29 — recency τ override
    bm25_weight: float | None = None  # #28 — fusion override (advisory)

    @classmethod
    def from_dict(cls, raw: dict) -> "EvalQuery":
        cat = raw.get("category", "factual")
        if cat not in CATEGORIES:
            raise ValueError(
                f"unknown category {cat!r}; expected one of {CATEGORIES}"
            )
        if "expected_ids" not in raw:
            raise ValueError("missing required field: expected_ids")
        ids_raw = raw["expected_ids"]
        if not isinstance(ids_raw, list):
            raise ValueError("expected_ids must be a list")
        expected_list: list[int] = []
        for x in ids_raw:
            # bool is a subclass of int — exclude it explicitly so True/False
            # don't silently coerce to 1/0.
            if isinstance(x, bool) or not isinstance(x, int):
                raise ValueError(
                    f"expected_ids must contain ints, got {type(x).__name__}"
                )
            expected_list.append(x)
        if not expected_list:
            raise ValueError(f"query {raw.get('query')!r} has empty expected_ids")
        tw = raw.get("time_window_days")
        if tw is not None and (isinstance(tw, bool) or not isinstance(tw, int) or tw <= 0):
            raise ValueError("time_window_days must be a positive int")
        bw = raw.get("bm25_weight")
        if bw is not None and (
            isinstance(bw, bool) or not isinstance(bw, (int, float)) or not 0.0 <= bw <= 1.0
        ):
            raise ValueError("bm25_weight must be a float in [0.0, 1.0]")
        return cls(
            query=str(raw["query"]),
            expected_ids=tuple(expected_list),
            category=cat,
            notes=str(raw.get("notes", "")),
            time_window_days=tw,
            bm25_weight=float(bw) if bw is not None else None,
        )


@dataclass
class EvalReport:
    """Aggregate metrics. Per-query detail is intentionally omitted from this
    object — callers that need it should keep their own list. Only aggregates
    cross the privacy boundary into `eval_results/*.md`."""

    total_queries: int
    recall_at_k: dict[int, float]
    mrr: float
    per_category: dict[str, dict[str, float]] = field(default_factory=dict)

    def avg_recall_at_5(self) -> float:
        return self.recall_at_k.get(5, 0.0)

    def to_dict(self) -> dict:
        return {
            "total_queries": self.total_queries,
            "recall_at_k": {str(k): v for k, v in self.recall_at_k.items()},
            "mrr": self.mrr,
            "per_category": self.per_category,
        }


def load_eval_set(path: str | Path) -> list[EvalQuery]:
    """Load a JSONL eval set. Each line: query, expected_ids, category, notes."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"eval set not found: {p}")
    out: list[EvalQuery] = []
    with p.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                out.append(EvalQuery.from_dict(json.loads(line)))
            except (ValueError, json.JSONDecodeError) as e:
                raise ValueError(f"{p}:{lineno}: {e}") from e
    return out


def _recall_at_k(predicted: list[int], expected: Iterable[int], k: int) -> float:
    expected_set = set(expected)
    if not expected_set:
        return 0.0
    top_k = predicted[:k]
    hits = sum(1 for pid in top_k if pid in expected_set)
    return hits / len(expected_set)


def _reciprocal_rank(predicted: list[int], expected: Iterable[int]) -> float:
    expected_set = set(expected)
    for rank, pid in enumerate(predicted, start=1):
        if pid in expected_set:
            return 1.0 / rank
    return 0.0


async def run_eval(
    eval_set: list[EvalQuery],
    retriever: Retriever,
    k_values: tuple[int, ...] = DEFAULT_K_VALUES,
) -> EvalReport:
    """Run every query through the retriever and aggregate metrics.

    Queries fan out concurrently — embedding latency dominates per-query
    cost (~150ms each via Ollama), so a 30-query set drops from ~5s
    sequential to roughly the slowest single query in parallel.
    """
    if not eval_set:
        raise ValueError("eval_set is empty")
    max_k = max(k_values)

    import asyncio  # local import to keep module import side-effect free

    predicted_lists = await asyncio.gather(
        *(retriever(q, max_k) for q in eval_set)
    )

    recall_sums: dict[int, float] = {k: 0.0 for k in k_values}
    rr_sum = 0.0
    cat_recalls: dict[str, dict[int, list[float]]] = defaultdict(
        lambda: {k: [] for k in k_values}
    )
    cat_rr: dict[str, list[float]] = defaultdict(list)

    for q, predicted in zip(eval_set, predicted_lists):
        rr = _reciprocal_rank(predicted, q.expected_ids)
        rr_sum += rr
        cat_rr[q.category].append(rr)
        for k in k_values:
            r = _recall_at_k(predicted, q.expected_ids, k)
            recall_sums[k] += r
            cat_recalls[q.category][k].append(r)

    n = len(eval_set)
    per_category: dict[str, dict[str, float]] = {}
    for cat, by_k in cat_recalls.items():
        rrs = cat_rr[cat]
        per_category[cat] = {
            **{f"recall@{k}": mean(by_k[k]) if by_k[k] else 0.0 for k in k_values},
            "mrr": mean(rrs) if rrs else 0.0,
            "n": float(len(rrs)),
        }

    return EvalReport(
        total_queries=n,
        recall_at_k={k: recall_sums[k] / n for k in k_values},
        mrr=rr_sum / n,
        per_category=per_category,
    )


def _validate_baseline_shape(baseline: dict) -> None:
    """Guard against a corrupted or hand-edited baseline.json — the gate
    decision is load-bearing for closing the epic, so we want a clear
    error rather than a silently-coerced numeric comparison."""
    if not isinstance(baseline, dict):
        raise ValueError("baseline must be a JSON object")
    per_cat = baseline.get("per_category")
    if not isinstance(per_cat, dict):
        raise ValueError("baseline.per_category must be an object")
    for cat, metrics in per_cat.items():
        if not isinstance(metrics, dict):
            raise ValueError(f"baseline.per_category[{cat!r}] must be an object")
        # Accept either schema variant; reject non-numeric values.
        r5 = metrics.get("recall@5", metrics.get("recall_at_5"))
        if r5 is None:
            continue  # category may be sparse; the comparator skips it
        if isinstance(r5, bool) or not isinstance(r5, (int, float)):
            raise ValueError(
                f"baseline.per_category[{cat!r}].recall@5 must be a number"
            )


def compare_to_baseline(
    current: EvalReport,
    baseline: dict,
    min_recall_at_5_lift_pp: float = DEFAULT_GATE_THRESHOLD_PP,
) -> dict:
    """Compare a current report against a locked baseline JSON.

    The epic gate (#30): after-numbers must exceed baseline Recall@5 by at
    least `min_recall_at_5_lift_pp` percentage points averaged across
    categories present in both runs. Returns a structured verdict including
    the lift, so callers can render it into markdown or fail CI.
    """
    _validate_baseline_shape(baseline)
    cur_per_cat = current.per_category
    base_per_cat = baseline.get("per_category", {})
    shared = sorted(set(cur_per_cat) & set(base_per_cat))
    lifts: dict[str, float] = {}
    for cat in shared:
        cur_r5 = float(cur_per_cat[cat].get("recall@5", 0.0))
        base_r5 = float(base_per_cat[cat].get(
            "recall@5",
            base_per_cat[cat].get("recall_at_5", 0.0),
        ))
        lifts[cat] = (cur_r5 - base_r5) * 100.0  # percentage points

    avg_lift_pp = mean(lifts.values()) if lifts else 0.0
    return {
        "shared_categories": shared,
        "per_category_lift_pp": lifts,
        "avg_recall_at_5_lift_pp": avg_lift_pp,
        "threshold_pp": min_recall_at_5_lift_pp,
        "passes_gate": avg_lift_pp >= min_recall_at_5_lift_pp,
    }
