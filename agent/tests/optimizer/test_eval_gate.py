"""Tests for ``agent/optimizer/eval_gate.py`` — pre-commit gate logic.

Hermetic — no shell, no git, no real eval runners. The router runner is
swapped for a stub via ``register_runner``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.optimizer import eval_gate
from agent.optimizer.eval_gate import (
    BYPASS_ENV_VAR,
    DEFAULT_THRESHOLDS,
    bypassed,
    evaluate_paths,
    register_runner,
    target_from_path,
    threshold_for,
)
from agent.optimizer.schema import CandidatePrompt, PromptStatus
from agent.optimizer.storage import compute_version_hash


# ── Fixtures ────────────────────────────────────────────────────────────────


def _write_prompt(
    base: Path,
    *,
    target: str,
    text: str = "hello",
    status: PromptStatus = PromptStatus.ACCEPTED,
    sanitization: list[str] | None = None,
    version_hash: str | None = None,
) -> Path:
    vh = version_hash or compute_version_hash(target, text)
    candidate = CandidatePrompt(
        target=target,
        version_hash=vh,
        parent_version="",
        optimizer_run_id="run-1",
        prompt_text=text,
        eval_score=0.42,
        status=status,
        created_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
        sanitization=sanitization or [],
    )
    target_dir = base / target
    target_dir.mkdir(parents=True, exist_ok=True)
    p = target_dir / f"{vh}.json"
    p.write_text(json.dumps({
        "target": candidate.target,
        "version_hash": candidate.version_hash,
        "parent_version": candidate.parent_version,
        "optimizer_run_id": candidate.optimizer_run_id,
        "prompt_text": candidate.prompt_text,
        "eval_score": candidate.eval_score,
        "status": candidate.status.value,
        "created_at": candidate.created_at.isoformat(),
        "sanitization": candidate.sanitization,
    }))
    return p


@pytest.fixture
def prompts_root(tmp_path, monkeypatch):
    """Point `agent/prompts/` at a tmp dir for the duration of one test."""
    root = tmp_path / "agent_prompts"
    root.mkdir()
    monkeypatch.setattr(eval_gate, "ACCEPTED_PROMPTS_DIR", root)
    return root


@pytest.fixture(autouse=True)
def reset_runners():
    """Snapshot/restore the EVAL_RUNNERS registry between tests so a
    test that registers a stub doesn't leak into the next test."""
    saved = dict(eval_gate.EVAL_RUNNERS)
    yield
    eval_gate.EVAL_RUNNERS.clear()
    eval_gate.EVAL_RUNNERS.update(saved)


# ── target_from_path ───────────────────────────────────────────────────────


def test_target_from_path_extracts_target(prompts_root):
    p = _write_prompt(prompts_root, target="ctx_assembly")
    assert target_from_path(p) == "ctx_assembly"


def test_target_from_path_rejects_non_prompt_layout(prompts_root, tmp_path):
    bogus = tmp_path / "random.json"
    bogus.write_text("{}")
    assert target_from_path(bogus) is None


def test_target_from_path_rejects_nested_layout(prompts_root):
    nested = prompts_root / "ctx_assembly" / "deeper" / "x.json"
    nested.parent.mkdir(parents=True)
    nested.write_text("{}")
    assert target_from_path(nested) is None


# ── threshold_for ──────────────────────────────────────────────────────────


def test_threshold_default_per_target():
    for target, default in DEFAULT_THRESHOLDS.items():
        assert threshold_for(target) == default


def test_threshold_env_override(monkeypatch):
    monkeypatch.setenv("PEPPER_GATE_THRESHOLD_ROUTER_CLASSIFIER", "0.99")
    assert threshold_for("router_classifier") == 0.99


def test_threshold_unknown_target_raises():
    with pytest.raises(KeyError):
        threshold_for("does_not_exist")


def test_threshold_env_override_rejects_negative(monkeypatch):
    """Stray negative override would silently pass everything."""
    monkeypatch.setenv("PEPPER_GATE_THRESHOLD_ROUTER_CLASSIFIER", "-0.1")
    with pytest.raises(ValueError, match="must be a finite number"):
        threshold_for("router_classifier")


def test_threshold_env_override_rejects_inf(monkeypatch):
    monkeypatch.setenv("PEPPER_GATE_THRESHOLD_ROUTER_CLASSIFIER", "-inf")
    with pytest.raises(ValueError, match="must be a finite number"):
        threshold_for("router_classifier")


def test_threshold_env_override_rejects_above_one(monkeypatch):
    monkeypatch.setenv("PEPPER_GATE_THRESHOLD_ROUTER_CLASSIFIER", "1.5")
    with pytest.raises(ValueError, match="must be a finite number in"):
        threshold_for("router_classifier")


def test_threshold_env_override_rejects_garbage(monkeypatch):
    monkeypatch.setenv("PEPPER_GATE_THRESHOLD_ROUTER_CLASSIFIER", "yes please")
    with pytest.raises(ValueError, match="not a number"):
        threshold_for("router_classifier")


# ── evaluate_paths ─────────────────────────────────────────────────────────


def test_evaluate_passes_above_threshold(prompts_root):
    p = _write_prompt(prompts_root, target="router_classifier")
    register_runner("router_classifier", lambda c: 0.95)
    [r] = evaluate_paths([p])
    assert r.passed
    assert r.target == "router_classifier"
    assert r.score == 0.95
    assert r.threshold == DEFAULT_THRESHOLDS["router_classifier"]


def test_evaluate_blocks_below_threshold(prompts_root):
    p = _write_prompt(prompts_root, target="router_classifier")
    register_runner("router_classifier", lambda c: 0.50)
    [r] = evaluate_paths([p])
    assert not r.passed
    assert "below threshold" in r.notes


def test_evaluate_passes_at_exact_threshold(prompts_root):
    """`>=` not `>`: exact threshold must pass."""
    p = _write_prompt(prompts_root, target="router_classifier")
    threshold = DEFAULT_THRESHOLDS["router_classifier"]
    register_runner("router_classifier", lambda c: threshold)
    [r] = evaluate_paths([p])
    assert r.passed


def test_evaluate_rejects_inf_score(prompts_root):
    """An `inf`-returning runner would silently auto-pass; reject it."""
    p = _write_prompt(prompts_root, target="router_classifier")
    register_runner("router_classifier", lambda c: float("inf"))
    [r] = evaluate_paths([p])
    assert not r.passed
    assert "out-of-range" in r.notes


def test_evaluate_rejects_negative_score(prompts_root):
    p = _write_prompt(prompts_root, target="router_classifier")
    register_runner("router_classifier", lambda c: -0.5)
    [r] = evaluate_paths([p])
    assert not r.passed
    assert "out-of-range" in r.notes


def test_evaluate_rejects_symlink(prompts_root, tmp_path):
    """Symlinks under agent/prompts/ are refused — operator-introduced."""
    real = _write_prompt(prompts_root, target="router_classifier")
    link = prompts_root / "router_classifier" / "deadbeef00000000.json"
    if link.exists():
        link.unlink()
    link.symlink_to(real)
    register_runner("router_classifier", lambda c: 0.99)
    [r] = evaluate_paths([link])
    assert not r.passed
    assert "symlink" in r.notes.lower()


def test_evaluate_rescans_for_pii_even_with_clean_field(prompts_root, monkeypatch):
    """Hand-edited file with sanitization=[] but PII in prompt_text
    must still be blocked. Defence-in-depth against bypass of
    PromptStore.put."""
    p = _write_prompt(
        prompts_root,
        target="router_classifier",
        text="Call user@example.com to confirm.",
        sanitization=[],  # operator wrote a clean field
    )
    register_runner("router_classifier", lambda c: 0.99)
    [r] = evaluate_paths([p])
    assert not r.passed
    assert "pii" in r.notes.lower() or "email" in r.notes.lower()


def test_evaluate_rejects_non_accepted_status(prompts_root):
    p = _write_prompt(
        prompts_root,
        target="router_classifier",
        status=PromptStatus.CANDIDATE,
    )
    register_runner("router_classifier", lambda c: 1.0)
    [r] = evaluate_paths([p])
    assert not r.passed
    assert "status" in r.notes.lower()


def test_evaluate_rejects_accepted_with_sanitization(prompts_root):
    """Defence-in-depth: PromptStore.put should already block this, but
    a hand-edited file could bypass storage."""
    p = _write_prompt(
        prompts_root,
        target="router_classifier",
        sanitization=["life_context token: 'pepperton'"],
    )
    register_runner("router_classifier", lambda c: 1.0)
    [r] = evaluate_paths([p])
    assert not r.passed
    assert "pii findings" in r.notes.lower()


def test_evaluate_unknown_target_fails_closed(prompts_root):
    p = _write_prompt(prompts_root, target="brand_new_target")
    [r] = evaluate_paths([p])
    assert not r.passed
    assert "no eval runner" in r.notes.lower()


def test_evaluate_corrupt_file_fails_closed(prompts_root):
    target_dir = prompts_root / "router_classifier"
    target_dir.mkdir()
    p = target_dir / "deadbeef00000000.json"
    p.write_text("{not valid json")
    [r] = evaluate_paths([p])
    assert not r.passed
    assert "failed to load" in r.notes.lower()


def test_evaluate_path_outside_prompts_fails_closed(tmp_path):
    bogus = tmp_path / "bogus.json"
    bogus.write_text("{}")
    [r] = evaluate_paths([bogus])
    assert not r.passed
    assert r.target == "(unknown)"


def test_evaluate_multiple_paths_all_results_returned(prompts_root):
    register_runner("router_classifier", lambda c: 0.95)
    p1 = _write_prompt(prompts_root, target="router_classifier", text="A",
                       version_hash="aaaa000000000001")
    p2 = _write_prompt(prompts_root, target="router_classifier", text="B",
                       version_hash="aaaa000000000002")
    results = evaluate_paths([p1, p2])
    assert len(results) == 2
    assert all(r.passed for r in results)


# ── bypass ──────────────────────────────────────────────────────────────────


def test_bypass_default_false(monkeypatch):
    monkeypatch.delenv(BYPASS_ENV_VAR, raising=False)
    assert bypassed() is False


def test_bypass_set_true(monkeypatch):
    monkeypatch.setenv(BYPASS_ENV_VAR, "1")
    assert bypassed() is True


def test_bypass_only_on_exact_one(monkeypatch):
    monkeypatch.setenv(BYPASS_ENV_VAR, "true")
    assert bypassed() is False  # we accept "1" only — no fuzzy truthy
