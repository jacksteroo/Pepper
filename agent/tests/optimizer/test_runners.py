"""Tests for ``agent/optimizer/runners.py``.

End-to-end via ``DeterministicRunner`` (no GEPA dependency). The
GEPA-specific path is exercised by the integration test that lazy-imports
``gepa`` at runtime — kept separate so the rest of the suite stays
hermetic.
"""
from __future__ import annotations



from agent.optimizer.audit import AuditLog
from agent.optimizer.runners import (
    DeterministicRunner,
    run_optimizer,
)
from agent.optimizer.schema import PromptStatus, TraceExample
from agent.optimizer.storage import PromptStore


class _FixtureAdapter:
    """Score = number of mutation-suffix matches; mutations are deterministic."""

    target = "ctx_assembly"

    def score(self, prompt_text: str, example: TraceExample) -> float:
        # Reward longer prompts (so mutations that append something win).
        return float(len(prompt_text))

    def mutate(self, prompt_text, examples, seed):
        # Deterministic mutations. Same seed → same outputs.
        return [
            prompt_text + "+a",
            prompt_text + "+ab",
            prompt_text + "+abc",
        ]


def _examples(n=3) -> list[TraceExample]:
    return [
        TraceExample(
            trace_id=f"id-{i}",
            archetype="orchestrator",
            prompt_version="v3",
            input=f"in-{i}",
            output=f"out-{i}",
        )
        for i in range(n)
    ]


def test_deterministic_runner_returns_improvers():
    runner = DeterministicRunner()
    adapter = _FixtureAdapter()
    candidates = runner.run(
        baseline_prompt="seed",
        examples=_examples(),
        adapter=adapter,
        seed=0,
    )
    # All three mutations beat baseline (longer => higher score).
    assert len(candidates) == 3
    # Highest score first.
    assert candidates[0].eval_score >= candidates[-1].eval_score
    # Each candidate has a non-empty parent_version (the baseline hash).
    assert all(c.parent_version for c in candidates)
    # All are CANDIDATE status by default.
    assert all(c.status == PromptStatus.CANDIDATE for c in candidates)


def test_deterministic_runner_is_reproducible():
    runner = DeterministicRunner()
    adapter = _FixtureAdapter()
    a = runner.run(
        baseline_prompt="seed",
        examples=_examples(),
        adapter=adapter,
        seed=42,
    )
    b = runner.run(
        baseline_prompt="seed",
        examples=_examples(),
        adapter=adapter,
        seed=42,
    )
    assert [c.version_hash for c in a] == [c.version_hash for c in b]
    assert [c.eval_score for c in a] == [c.eval_score for c in b]


def test_deterministic_runner_empty_dataset_returns_empty():
    runner = DeterministicRunner()
    candidates = runner.run(
        baseline_prompt="seed",
        examples=[],
        adapter=_FixtureAdapter(),
        seed=0,
    )
    assert candidates == []


def test_deterministic_runner_filters_non_improvers():
    class _NoImproveAdapter:
        target = "ctx_assembly"

        def score(self, prompt_text, example):
            return 1.0  # Constant score => no candidate ever beats baseline.

        def mutate(self, prompt_text, examples, seed):
            return [prompt_text + "+a", prompt_text + "+ab"]

    candidates = DeterministicRunner().run(
        baseline_prompt="seed",
        examples=_examples(),
        adapter=_NoImproveAdapter(),
        seed=0,
    )
    assert candidates == []


def test_run_optimizer_persists_and_audits(tmp_path):
    store = PromptStore(tmp_path / "candidates")
    audit = AuditLog(tmp_path / "audit.jsonl")
    record, candidates = run_optimizer(
        runner=DeterministicRunner(),
        adapter=_FixtureAdapter(),
        examples=_examples(),
        baseline_prompt="seed",
        seed=0,
        archetype="orchestrator",
        prompt_version_filter="v3",
        store=store,
        audit_log=audit,
    )
    # Audit row appended.
    audit_records = list(audit.iter_records())
    assert len(audit_records) == 1
    assert audit_records[0].run_id == record.run_id
    assert audit_records[0].candidate_count == len(candidates)
    assert audit_records[0].dataset_size == 3
    # Candidates are on disk.
    on_disk = store.list("ctx_assembly")
    assert len(on_disk) == len(candidates)
    # All share the same run_id from the same run.
    assert all(c.optimizer_run_id == record.run_id for c in candidates)


def test_run_optimizer_run_id_threaded_through(tmp_path):
    """The audit row's run_id matches every candidate's optimizer_run_id,
    even when the runner doesn't generate its own.
    """
    store = PromptStore(tmp_path / "candidates")
    audit = AuditLog(tmp_path / "audit.jsonl")
    record, candidates = run_optimizer(
        runner=DeterministicRunner(),
        adapter=_FixtureAdapter(),
        examples=_examples(),
        baseline_prompt="seed",
        seed=0,
        store=store,
        audit_log=audit,
    )
    assert candidates  # we expect candidates here
    # All candidates share the same run_id as the audit record.
    assert all(c.optimizer_run_id == record.run_id for c in candidates)
    # And exactly one audit row carries that run_id.
    matched = [r for r in audit.iter_records() if r.run_id == record.run_id]
    assert len(matched) == 1


def test_run_optimizer_no_candidates_still_audits(tmp_path):
    """Even when no candidate beats baseline, an audit row must be appended."""
    class _NoImprove:
        target = "ctx_assembly"

        def score(self, prompt_text, example):
            return 1.0

        def mutate(self, prompt_text, examples, seed):
            return []

    audit = AuditLog(tmp_path / "audit.jsonl")
    record, candidates = run_optimizer(
        runner=DeterministicRunner(),
        adapter=_NoImprove(),
        examples=_examples(),
        baseline_prompt="seed",
        seed=0,
        store=PromptStore(tmp_path / "candidates"),
        audit_log=audit,
    )
    assert candidates == []
    assert record.candidate_count == 0
    assert len(list(audit.iter_records())) == 1
