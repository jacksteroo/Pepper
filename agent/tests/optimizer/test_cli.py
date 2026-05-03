"""Tests for ``agent/optimizer/__main__.py`` — CLI surface.

Exercises argument parsing and the ``show-candidates`` subcommand.
The full ``optimize`` subcommand requires a live database session and
is covered by the runner integration test above.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agent.optimizer.__main__ import (
    _build_arg_parser,
    _NullAdapter,
    parse_window,
)
from agent.optimizer.runners import DeterministicRunner, GepaRunner
from agent.optimizer.schema import TraceExample


def test_parse_window_supports_dhm():
    assert parse_window("7d") == timedelta(days=7)
    assert parse_window("12h") == timedelta(hours=12)
    assert parse_window("30m") == timedelta(minutes=30)


def test_parse_window_rejects_garbage():
    with pytest.raises(Exception):
        parse_window("forever")


def test_arg_parser_optimize_minimal():
    parser = _build_arg_parser()
    args = parser.parse_args([
        "optimize",
        "--target", "ctx_assembly",
        "--archetype", "orchestrator",
        "--window", "7d",
        "--baseline-prompt-file", "/tmp/p.txt",
    ])
    assert args.cmd == "optimize"
    assert args.target == "ctx_assembly"
    assert args.runner == "deterministic"  # default
    assert args.window == timedelta(days=7)


def test_arg_parser_gate():
    parser = _build_arg_parser()
    args = parser.parse_args([
        "gate",
        "--paths", "agent/prompts/x/abcd1234.json", "agent/prompts/y/deadbeef.json",
    ])
    assert args.cmd == "gate"
    assert len(args.paths) == 2


def test_cmd_gate_passes(monkeypatch, tmp_path, capsys):
    """Smoke: gate with a passing stub runner exits 0."""
    from agent.optimizer import eval_gate
    from agent.optimizer.__main__ import _cmd_gate

    root = tmp_path / "agent_prompts"
    root.mkdir()
    monkeypatch.setattr(eval_gate, "ACCEPTED_PROMPTS_DIR", root)
    eval_gate.register_runner("router_classifier", lambda c: 0.99)

    # Build a passing prompt file via the same fixture shape used in
    # test_eval_gate.
    from agent.optimizer.schema import CandidatePrompt, PromptStatus
    from agent.optimizer.storage import compute_version_hash
    from datetime import datetime, timezone
    text = "x"
    vh = compute_version_hash("router_classifier", text)
    target_dir = root / "router_classifier"
    target_dir.mkdir()
    p = target_dir / f"{vh}.json"
    cand = CandidatePrompt(
        target="router_classifier", version_hash=vh, parent_version="",
        optimizer_run_id="r", prompt_text=text, eval_score=0.5,
        status=PromptStatus.ACCEPTED,
        created_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
    )
    import json as _json
    p.write_text(_json.dumps({
        "target": cand.target, "version_hash": cand.version_hash,
        "parent_version": cand.parent_version,
        "optimizer_run_id": cand.optimizer_run_id,
        "prompt_text": cand.prompt_text, "eval_score": cand.eval_score,
        "status": cand.status.value,
        "created_at": cand.created_at.isoformat(),
        "sanitization": [],
    }))

    class A:
        cmd = "gate"
        paths = [p]
    monkeypatch.delenv("PEPPER_BYPASS_EVAL_GATE", raising=False)
    rc = _cmd_gate(A())
    out = capsys.readouterr()
    assert rc == 0, out.out + out.err


def test_cmd_gate_bypass_short_circuits(monkeypatch, capsys):
    from agent.optimizer.__main__ import _cmd_gate
    monkeypatch.setenv("PEPPER_BYPASS_EVAL_GATE", "1")

    class A:
        cmd = "gate"
        paths = [Path("nonexistent")]
    rc = _cmd_gate(A())
    err = capsys.readouterr().err
    assert rc == 0
    assert "BYPASSED" in err


def test_arg_parser_show_candidates():
    parser = _build_arg_parser()
    args = parser.parse_args([
        "show-candidates",
        "--target", "ctx_assembly",
    ])
    assert args.cmd == "show-candidates"


def test_null_adapter_score_returns_overlap():
    a = _NullAdapter("ctx_assembly")
    ex = TraceExample(
        trace_id="x",
        archetype="orchestrator",
        prompt_version="v1",
        input="hello world",
        output="hello there friend",
    )
    score = a.score("hello there", ex)
    # overlap = {hello, there} ∩ {hello, there, friend} = 2 / 3
    assert score == pytest.approx(2 / 3)


def test_null_adapter_mutate_is_deterministic():
    a = _NullAdapter("ctx_assembly")
    m1 = a.mutate("seed", [], 0)
    m2 = a.mutate("seed", [], 0)
    assert m1 == m2
    assert len(m1) == 3


def test_show_candidates_with_empty_dir(tmp_path, capsys):
    """Empty target dir prints the no-candidates message and exits 0."""
    from agent.optimizer.__main__ import _cmd_show_candidates

    class A:
        cmd = "show-candidates"
        target = "ctx_assembly"
        candidates_dir = tmp_path

    rc = _cmd_show_candidates(A())
    assert rc == 0
    assert "no candidates" in capsys.readouterr().out


def test_build_runner_supports_both():
    from agent.optimizer.__main__ import build_runner
    assert isinstance(build_runner("deterministic"), DeterministicRunner)
    # GEPA requires --reflection-lm pointing at a local LM (ADR-0007).
    assert isinstance(
        build_runner("gepa", reflection_lm="ollama/llama3"),
        GepaRunner,
    )
    with pytest.raises(Exception):
        build_runner("nonexistent")


def test_build_runner_gepa_requires_reflection_lm():
    from agent.optimizer.__main__ import build_runner
    with pytest.raises(Exception, match="reflection-lm"):
        build_runner("gepa")
    with pytest.raises(Exception, match="reflection-lm"):
        build_runner("gepa", reflection_lm=None)


def test_build_runner_gepa_rejects_remote_lm():
    """ADR-0007: trace content must not be sent to a frontier API."""
    from agent.optimizer.__main__ import build_runner
    with pytest.raises(Exception, match="local-model prefix|reflection_lm"):
        build_runner("gepa", reflection_lm="anthropic/claude-3-opus")
