"""Tests for ``agent/optimizer/adapters/router_classifier.py``."""
from __future__ import annotations

import json

import pytest

from agent.optimizer.adapters.router_classifier import (
    DEFAULT_TEMPLATE,
    TARGET_NAME,
    RouterClassifierAdapter,
    _eval_runner,
    _structural_score,
    parse_template,
)
from agent.optimizer.schema import (
    CandidatePrompt,
    PromptStatus,
    TraceExample,
)
from agent.optimizer.storage import compute_version_hash


def _ex() -> TraceExample:
    return TraceExample(
        trace_id="t1", archetype="orchestrator", prompt_version="v1",
        input="q", output="schedule_lookup",
    )


def _candidate(text: str) -> CandidatePrompt:
    return CandidatePrompt(
        target=TARGET_NAME,
        version_hash=compute_version_hash(TARGET_NAME, text),
        parent_version="",
        optimizer_run_id="r",
        prompt_text=text,
        eval_score=0.0,
        status=PromptStatus.CANDIDATE,
    )


def test_default_template_is_valid_json():
    doc = json.loads(DEFAULT_TEMPLATE)
    assert "exemplars" in doc
    assert "instructions" in doc
    assert len(doc["exemplars"]) >= 1


def test_parse_template_validates_shape():
    doc = parse_template(DEFAULT_TEMPLATE)
    assert isinstance(doc["exemplars"], list)
    assert all("query" in ex and "intent_label" in ex for ex in doc["exemplars"])


def test_parse_template_rejects_garbage():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_template("not json")


def test_parse_template_rejects_non_object():
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_template("[1, 2, 3]")


def test_parse_template_rejects_missing_exemplars():
    with pytest.raises(ValueError, match="exemplars"):
        parse_template(json.dumps({"instructions": "x"}))


def test_parse_template_rejects_missing_instructions():
    with pytest.raises(ValueError, match="instructions"):
        parse_template(json.dumps({"exemplars": []}))


def test_parse_template_rejects_malformed_exemplar():
    with pytest.raises(ValueError, match="exemplar"):
        parse_template(json.dumps({
            "instructions": "x" * 30,
            "exemplars": [{"query": "no label"}],
        }))


def test_structural_score_default_template_is_high():
    assert _structural_score(DEFAULT_TEMPLATE) >= 0.85


def test_structural_score_invalid_json_is_zero():
    assert _structural_score("not json") == 0.0


def test_structural_score_oversized_is_zero():
    huge = json.dumps({
        "instructions": "x" * 100,
        "exemplars": [
            {"query": "x" * 1000, "intent_label": "general_chat"}
            for _ in range(100)
        ],
    })
    # Force into oversized regime.
    huge += " " * (64 * 1024)
    assert _structural_score(huge) == 0.0


def test_structural_score_zero_exemplars_is_zero():
    text = json.dumps({"instructions": "x" * 30, "exemplars": []})
    assert _structural_score(text) == 0.0


def test_adapter_target_name():
    assert RouterClassifierAdapter.target == "router_classifier"
    assert RouterClassifierAdapter().target == TARGET_NAME


def test_adapter_score_uses_structural():
    a = RouterClassifierAdapter()
    assert a.score(DEFAULT_TEMPLATE, _ex()) == _structural_score(DEFAULT_TEMPLATE)


def test_adapter_mutate_is_deterministic():
    a = RouterClassifierAdapter()
    m1 = a.mutate(DEFAULT_TEMPLATE, [], 0)
    m2 = a.mutate(DEFAULT_TEMPLATE, [], 0)
    assert m1 == m2
    # Every mutation must parse cleanly.
    for m in m1:
        parse_template(m)


def test_adapter_mutate_skips_unparseable_baseline():
    a = RouterClassifierAdapter()
    assert a.mutate("not json", [], 0) == []


def test_eval_runner_matches_structural_score():
    cand = _candidate(DEFAULT_TEMPLATE)
    assert _eval_runner(cand) == _structural_score(DEFAULT_TEMPLATE)


def test_adapter_registered():
    """Side-effect import via agent.optimizer.adapters package
    populates both the adapter and the eval-gate runner registries."""
    from agent.optimizer.adapters import ADAPTERS, get_adapter
    from agent.optimizer.eval_gate import EVAL_RUNNERS

    assert "router_classifier" in ADAPTERS
    assert "router_classifier" in EVAL_RUNNERS
    a = get_adapter("router_classifier")
    assert a.target == "router_classifier"


def test_runner_above_threshold():
    """Default template scores above the documented 0.85 threshold."""
    from agent.optimizer.eval_gate import (
        DEFAULT_THRESHOLDS,
        EVAL_RUNNERS,
    )
    cand = _candidate(DEFAULT_TEMPLATE)
    score = EVAL_RUNNERS["router_classifier"](cand)
    assert score >= DEFAULT_THRESHOLDS["router_classifier"]
