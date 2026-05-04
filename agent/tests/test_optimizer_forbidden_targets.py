"""Locks the FORBIDDEN_TARGETS guard in `agent.optimizer.eval_gate`.

Per ADR-0008 and #52, the optimizer must never tune the identity
prompt. We enforce this by:

1. The hard set `FORBIDDEN_TARGETS` containing `"identity"`.
2. `register_runner` raises if a registration would shadow a
   forbidden target.

If a future contributor changes either, this test fails loudly.
"""
from __future__ import annotations

import pytest

from agent.optimizer.eval_gate import (
    FORBIDDEN_TARGETS,
    register_runner,
)


def test_identity_is_forbidden() -> None:
    assert "identity" in FORBIDDEN_TARGETS, (
        "ADR-0008 forbids the optimizer from tuning the identity prompt; "
        "removing 'identity' from FORBIDDEN_TARGETS requires a new ADR."
    )


def test_register_runner_refuses_forbidden_target() -> None:
    def _runner(_):
        return 1.0

    with pytest.raises(ValueError, match="FORBIDDEN_TARGETS"):
        register_runner("identity", _runner)


def test_evaluate_paths_refuses_forbidden_target_explicitly(tmp_path) -> None:
    """A hand-edited prompt under `agent/prompts/identity/` must produce
    a FORBIDDEN-shaped error message at gate-time, not the generic
    "no eval runner" fall-through. The two failure modes both fail
    closed, but the FORBIDDEN message is self-documenting.
    """
    from pathlib import Path

    from agent.optimizer.eval_gate import evaluate_paths

    # Synthesise a path that target_from_path() will resolve to "identity".
    # We don't actually need the file to exist — `target_from_path`
    # operates on the path string. To be safe though, we build the
    # directory structure under tmp_path and monkey the ACCEPTED_PROMPTS_DIR.
    import agent.optimizer.eval_gate as eg

    fake_root = tmp_path / "prompts"
    target_dir = fake_root / "identity"
    target_dir.mkdir(parents=True)
    fake_path = target_dir / "v1.json"
    fake_path.write_text("{}", encoding="utf-8")

    orig_root = eg.ACCEPTED_PROMPTS_DIR
    eg.ACCEPTED_PROMPTS_DIR = fake_root
    try:
        results = evaluate_paths([fake_path])
    finally:
        eg.ACCEPTED_PROMPTS_DIR = orig_root

    assert len(results) == 1
    r = results[0]
    assert r.passed is False
    assert r.target == "identity"
    assert "FORBIDDEN" in r.notes
    assert "ADR-0008" in r.notes
