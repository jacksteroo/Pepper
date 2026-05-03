"""Tests for ``agent/optimizer/adapters/context_assembly.py``."""
from __future__ import annotations


from agent.optimizer.adapters.context_assembly import (
    DEFAULT_TEMPLATE,
    TARGET_NAME,
    ContextAssemblyAdapter,
    _eval_runner,
    _structural_score,
    render_template,
)
from agent.optimizer.schema import (
    CandidatePrompt,
    PromptStatus,
    TraceExample,
)
from agent.optimizer.storage import compute_version_hash


def _ex() -> TraceExample:
    return TraceExample(
        trace_id="t1", archetype="orchestrator", prompt_version="v3",
        input="q", output="a",
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


def test_default_template_has_required_placeholder():
    assert "{memory_lines}" in DEFAULT_TEMPLATE


def test_default_template_renders():
    rendered = render_template(DEFAULT_TEMPLATE, "• A\n• B")
    assert "[Relevant memories" in rendered
    assert "• A" in rendered


def test_render_template_handles_braces_in_candidate():
    """Candidate templates may contain stray { or } characters from
    optimizer mutations. ``render_template`` must not raise — it
    uses ``str.replace``, not ``str.format``."""
    weird = "Here are memories: {memory_lines} (note: dict-like {x: 1} examples)"
    rendered = render_template(weird, "• Z")
    assert "• Z" in rendered
    assert "{x: 1}" in rendered


def test_structural_score_default_template_is_high():
    assert _structural_score(DEFAULT_TEMPLATE) > 0.85


def test_structural_score_missing_placeholder_is_zero():
    assert _structural_score("[Relevant memories]\n[End]") == 0.0


def test_structural_score_oversized_is_zero():
    huge = "x" * 5000 + "{memory_lines}"
    assert _structural_score(huge) == 0.0


def test_structural_score_too_short_is_low():
    score = _structural_score("{memory_lines}")
    # Short but valid — placeholder present, < 30 bytes → 0.5
    assert 0.4 <= score <= 0.6


def test_adapter_target_name():
    assert ContextAssemblyAdapter.target == "context_assembly"
    assert ContextAssemblyAdapter().target == TARGET_NAME


def test_adapter_score_uses_structural():
    a = ContextAssemblyAdapter()
    assert a.score(DEFAULT_TEMPLATE, _ex()) == _structural_score(DEFAULT_TEMPLATE)


def test_adapter_mutate_is_deterministic():
    a = ContextAssemblyAdapter()
    m1 = a.mutate(DEFAULT_TEMPLATE, [], 0)
    m2 = a.mutate(DEFAULT_TEMPLATE, [], 0)
    assert m1 == m2
    # All mutations must keep the placeholder, otherwise structural
    # score is zero and the runner filters them out.
    for m in m1:
        assert "{memory_lines}" in m


def test_eval_runner_matches_structural_score():
    cand = _candidate(DEFAULT_TEMPLATE)
    assert _eval_runner(cand) == _structural_score(DEFAULT_TEMPLATE)


def test_adapter_registered_via_registry():
    """Importing the adapters package triggers the side-effect import
    of context_assembly, which calls register_adapter and
    register_runner. Both registries must contain context_assembly."""
    from agent.optimizer.adapters import ADAPTERS, get_adapter
    from agent.optimizer.eval_gate import EVAL_RUNNERS

    assert "context_assembly" in ADAPTERS
    assert "context_assembly" in EVAL_RUNNERS
    a = get_adapter("context_assembly")
    assert a.target == "context_assembly"


def test_runner_above_threshold(monkeypatch):
    """Default template scores above the documented 0.65 threshold."""
    from agent.optimizer.eval_gate import (
        DEFAULT_THRESHOLDS,
        EVAL_RUNNERS,
    )
    cand = _candidate(DEFAULT_TEMPLATE)
    score = EVAL_RUNNERS["context_assembly"](cand)
    assert score >= DEFAULT_THRESHOLDS["context_assembly"]
