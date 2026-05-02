"""Unit tests for the retrieval eval runner (Epic 02, issue #30).

These exercise the runner against a synthetic fixture corpus so CI does not
depend on the gitignored real eval set or a populated Postgres."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.tests._retrieval_eval_fixtures import (
    FIXTURE_EVAL_SET,
    make_fixture_retriever,
)
from agent.tests.retrieval_eval import (
    CATEGORIES,
    EvalQuery,
    compare_to_baseline,
    load_eval_set,
    run_eval,
)


# ─── Schema / loader ──────────────────────────────────────────────────────


def test_eval_query_rejects_unknown_category():
    with pytest.raises(ValueError, match="unknown category"):
        EvalQuery.from_dict(
            {"query": "x", "expected_ids": [1], "category": "bogus"}
        )


def test_eval_query_rejects_empty_expected_ids():
    with pytest.raises(ValueError, match="empty expected_ids"):
        EvalQuery.from_dict(
            {"query": "x", "expected_ids": [], "category": "factual"}
        )


def test_eval_query_rejects_missing_expected_ids():
    with pytest.raises(ValueError, match="missing required field: expected_ids"):
        EvalQuery.from_dict({"query": "x", "category": "factual"})


@pytest.mark.parametrize(
    "bad_value",
    ["1", 1.5, True, False, None],
    ids=["str", "float", "bool-true", "bool-false", "none"],
)
def test_eval_query_rejects_non_int_expected_ids(bad_value):
    with pytest.raises(ValueError, match="must contain ints"):
        EvalQuery.from_dict(
            {"query": "x", "expected_ids": [bad_value], "category": "factual"}
        )


def test_eval_query_accepts_optional_retrieval_knobs():
    q = EvalQuery.from_dict(
        {
            "query": "x",
            "expected_ids": [1, 2],
            "category": "person",
            "time_window_days": 14,
            "bm25_weight": 0.5,
        }
    )
    assert q.time_window_days == 14
    assert q.bm25_weight == 0.5


def test_eval_query_rejects_invalid_time_window():
    with pytest.raises(ValueError, match="time_window_days"):
        EvalQuery.from_dict(
            {
                "query": "x",
                "expected_ids": [1],
                "category": "person",
                "time_window_days": 0,
            }
        )


def test_eval_query_rejects_out_of_range_bm25_weight():
    with pytest.raises(ValueError, match="bm25_weight"):
        EvalQuery.from_dict(
            {
                "query": "x",
                "expected_ids": [1],
                "category": "factual",
                "bm25_weight": 1.5,
            }
        )


def test_load_eval_set_skips_blank_and_comment_lines(tmp_path: Path):
    p = tmp_path / "eval.jsonl"
    p.write_text(
        "\n"
        "# this is a comment\n"
        '{"query": "a", "expected_ids": [1], "category": "factual"}\n'
        "\n"
        '{"query": "b", "expected_ids": [2, 3], "category": "person"}\n',
        encoding="utf-8",
    )
    out = load_eval_set(p)
    assert [q.query for q in out] == ["a", "b"]
    assert out[1].expected_ids == (2, 3)


def test_load_eval_set_reports_line_number_on_bad_json(tmp_path: Path):
    p = tmp_path / "eval.jsonl"
    p.write_text(
        '{"query": "a", "expected_ids": [1], "category": "factual"}\n'
        "not-json\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=":2:"):
        load_eval_set(p)


def test_example_eval_set_parses():
    """The committed .example file must always parse — it's the template."""
    example = (
        Path(__file__).parent / "retrieval_eval_set.example.jsonl"
    )
    assert example.exists(), "missing committed eval-set template"
    parsed = load_eval_set(example)
    assert len(parsed) >= 5
    seen_categories = {q.category for q in parsed}
    assert seen_categories == set(CATEGORIES), (
        f"example set must cover all 5 categories, got {seen_categories}"
    )


# ─── Runner metrics ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_eval_against_fixture_corpus_returns_sane_metrics():
    retriever = make_fixture_retriever()
    report = await run_eval(list(FIXTURE_EVAL_SET), retriever)
    assert report.total_queries == len(FIXTURE_EVAL_SET)
    assert set(report.recall_at_k) == {1, 3, 5, 10}
    for k, v in report.recall_at_k.items():
        assert 0.0 <= v <= 1.0, f"recall@{k} out of range: {v}"
    assert 0.0 <= report.mrr <= 1.0
    # Token-overlap retriever should at least find the factual-lookup answers.
    assert report.recall_at_k[5] > 0.0
    # Each category exercised by FIXTURE_EVAL_SET appears in the breakdown.
    for q in FIXTURE_EVAL_SET:
        assert q.category in report.per_category


@pytest.mark.asyncio
async def test_recency_changes_temporal_ranking():
    """The recency-boosted retriever should not score worse on the temporal
    category than the recency-off retriever. Sanity-checks the eval is
    sensitive to the kind of change #29 will introduce."""
    base = await run_eval(
        list(FIXTURE_EVAL_SET), make_fixture_retriever(use_recency=False)
    )
    boosted = await run_eval(
        list(FIXTURE_EVAL_SET), make_fixture_retriever(use_recency=True)
    )
    base_temporal = base.per_category.get("temporal", {}).get("recall@5", 0.0)
    boosted_temporal = boosted.per_category.get("temporal", {}).get("recall@5", 0.0)
    assert boosted_temporal >= base_temporal


@pytest.mark.asyncio
async def test_run_eval_rejects_empty_set():
    with pytest.raises(ValueError, match="empty"):
        await run_eval([], make_fixture_retriever())


# ─── Gate comparison ──────────────────────────────────────────────────────


def test_compare_to_baseline_passes_when_lift_above_threshold():
    from agent.tests.retrieval_eval import EvalReport

    current = EvalReport(
        total_queries=10,
        recall_at_k={1: 0.4, 3: 0.6, 5: 0.7, 10: 0.8},
        mrr=0.5,
        per_category={
            "factual": {"recall@5": 0.8, "mrr": 0.6, "n": 5.0},
            "temporal": {"recall@5": 0.7, "mrr": 0.5, "n": 5.0},
        },
    )
    baseline = {
        "per_category": {
            "factual": {"recall@5": 0.6},
            "temporal": {"recall@5": 0.5},
        }
    }
    verdict = compare_to_baseline(current, baseline, min_recall_at_5_lift_pp=10.0)
    assert verdict["passes_gate"] is True
    assert verdict["avg_recall_at_5_lift_pp"] == pytest.approx(20.0)


def test_compare_to_baseline_fails_when_lift_below_threshold():
    from agent.tests.retrieval_eval import EvalReport

    current = EvalReport(
        total_queries=4,
        recall_at_k={1: 0.5, 3: 0.6, 5: 0.65, 10: 0.7},
        mrr=0.55,
        per_category={"factual": {"recall@5": 0.65, "mrr": 0.55, "n": 4.0}},
    )
    baseline = {"per_category": {"factual": {"recall@5": 0.6}}}
    verdict = compare_to_baseline(current, baseline, min_recall_at_5_lift_pp=10.0)
    assert verdict["passes_gate"] is False
    assert verdict["avg_recall_at_5_lift_pp"] == pytest.approx(5.0)


def test_compare_to_baseline_rejects_malformed_baseline():
    from agent.tests.retrieval_eval import EvalReport

    current = EvalReport(
        total_queries=1,
        recall_at_k={5: 0.0},
        mrr=0.0,
        per_category={"factual": {"recall@5": 0.0, "mrr": 0.0, "n": 1.0}},
    )
    with pytest.raises(ValueError, match="must be a JSON object"):
        compare_to_baseline(current, [])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="per_category"):
        compare_to_baseline(current, {"per_category": "bogus"})
    with pytest.raises(ValueError, match="must be a number"):
        compare_to_baseline(
            current, {"per_category": {"factual": {"recall@5": "not-a-number"}}}
        )


def test_compare_to_baseline_handles_missing_categories():
    from agent.tests.retrieval_eval import EvalReport

    current = EvalReport(
        total_queries=2,
        recall_at_k={1: 1.0, 3: 1.0, 5: 1.0, 10: 1.0},
        mrr=1.0,
        per_category={"new_only": {"recall@5": 1.0, "mrr": 1.0, "n": 2.0}},
    )
    baseline = {"per_category": {"old_only": {"recall@5": 0.5}}}
    verdict = compare_to_baseline(current, baseline)
    assert verdict["shared_categories"] == []
    assert verdict["passes_gate"] is False  # no shared cats → 0.0 lift


def test_eval_report_to_dict_round_trip():
    from agent.tests.retrieval_eval import EvalReport

    r = EvalReport(
        total_queries=3,
        recall_at_k={1: 0.33, 5: 0.66},
        mrr=0.5,
        per_category={"factual": {"recall@5": 0.66, "mrr": 0.5, "n": 3.0}},
    )
    blob = r.to_dict()
    # Keys are stringified for JSON-friendliness.
    assert blob["recall_at_k"] == {"1": 0.33, "5": 0.66}
    json.dumps(blob)  # round-trips cleanly
