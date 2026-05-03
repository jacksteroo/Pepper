"""Pre-commit eval gate for versioned prompts.

Acceptance criteria from #48:

- Gate blocks bad prompts.
- Gate passes good prompts.
- Bypass requires explicit flag and logs to commit.
- Documented thresholds in ``docs/optimizer-policy.md``.

Wire-up
-------

The shell hook (``scripts/git-hooks/pre-commit-prompt-eval-gate``)
detects staged changes under ``agent/prompts/`` and shells out to:

    python -m agent.optimizer gate --paths <p1> <p2> ...

This module is the dispatcher: each prompt is resolved to its target
(via the directory layout ``agent/prompts/<target>/<version>.json``)
and the per-target eval runner is invoked. If any runner returns a
score below its target threshold, the gate exits non-zero.

Per-target evaluators are looked up from ``EVAL_RUNNERS``. Targets land
their runners in #46 (context-assembly) and #47 (router classifier);
this PR ships the dispatcher, the threshold table, and the existing
router runner that reuses ``agent.router_eval``.

Bypass
------

Set ``PEPPER_BYPASS_EVAL_GATE=1`` to skip the gate. The bypass is
logged to stderr loudly and the operator is expected to surface it in
the commit message; the shell hook injects a trailer. This is for
emergency rollback only — see ``docs/optimizer-policy.md``.

Thresholds
----------

Hard-coded defaults match ``docs/optimizer-policy.md``. Per-target
overrides via env vars: ``PEPPER_GATE_THRESHOLD_<TARGET_UPPER>=0.78``.
"""
from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import structlog

from agent.optimizer.sanitizer import scan as _sanitize_scan
from agent.optimizer.schema import CandidatePrompt, PromptStatus
from agent.optimizer.storage import _candidate_from_json  # internal but stable

logger = structlog.get_logger(__name__)

ACCEPTED_PROMPTS_DIR = Path("agent/prompts")
BYPASS_ENV_VAR = "PEPPER_BYPASS_EVAL_GATE"


@dataclass(frozen=True)
class GateResult:
    """One per (path, target) evaluation. ``passed`` collapses both the
    runner's success and the score-vs-threshold check."""

    path: Path
    target: str
    score: float
    threshold: float
    passed: bool
    notes: str = ""


# ── Threshold configuration ──────────────────────────────────────────────────

# Defaults documented in docs/optimizer-policy.md. Per-target override
# via PEPPER_GATE_THRESHOLD_<TARGET_UPPER> (e.g. PEPPER_GATE_THRESHOLD_ROUTER_CLASSIFIER=0.90).
DEFAULT_THRESHOLDS: dict[str, float] = {
    "router_classifier": 0.85,    # matches existing pre-commit-router-eval
    "context_assembly": 0.65,     # baseline-from-#30 retrieval Recall@5
    "reflector_rubric": 0.70,     # baseline-from-#42 rubric mean score
}

# Targets the gate knows about. Unknown targets fail closed (refuse to
# promote a prompt the gate cannot evaluate).
KNOWN_TARGETS: frozenset[str] = frozenset(DEFAULT_THRESHOLDS)


def threshold_for(target: str) -> float:
    """Resolve the floor score for a target.

    Order: env override → DEFAULT_THRESHOLDS → KeyError.

    Env-var overrides are validated to be finite floats in ``[0, 1]``.
    A stray ``-inf`` or negative override would silently pass every
    prompt; we fail loud instead.
    """
    env_key = f"PEPPER_GATE_THRESHOLD_{target.upper()}"
    if env_key in os.environ:
        try:
            v = float(os.environ[env_key])
        except ValueError as e:
            raise ValueError(
                f"{env_key}={os.environ[env_key]!r} is not a number",
            ) from e
        if not math.isfinite(v) or not 0.0 <= v <= 1.0:
            raise ValueError(
                f"{env_key}={v!r} must be a finite number in [0, 1]",
            )
        return v
    return DEFAULT_THRESHOLDS[target]


# ── Eval runners ─────────────────────────────────────────────────────────────

EvalRunner = Callable[[CandidatePrompt], float]
"""A runner returns a single higher-is-better score in [0, 1]. The gate
compares it to the target threshold; >= passes."""


# Module-level mutable registry — populated by register_runner.
#
# Empty by default. Each target registers its runner from its own
# module at import time:
#
#   - #47 lands `router_classifier` (rebuilds the router with the
#     candidate's prompt and runs `agent.router_eval.evaluate`).
#   - #46 lands `context_assembly` (runs the #30 retrieval eval
#     against the candidate prompt).
#   - #42 follow-up lands `reflector_rubric`.
#
# The gate fails closed for unregistered targets, which is the right
# safety bias: never promote a prompt for a target whose runner isn't
# yet available. ``register_runner`` is idempotent — re-registration
# replaces the previous callable.
EVAL_RUNNERS: dict[str, EvalRunner] = {}


def register_runner(target: str, runner: EvalRunner) -> None:
    """Register or replace an eval runner for ``target``.

    Targets call this at import-time from their own module so the
    gate's import surface stays narrow (no need to import #46's
    context-assembly stack from inside the eval_gate module itself).
    """
    EVAL_RUNNERS[target] = runner


# ── Path → target resolution ─────────────────────────────────────────────────


def target_from_path(path: Path) -> Optional[str]:
    """Return the target name for ``agent/prompts/<target>/<version>.json``.

    Returns None for paths outside that layout — the shell hook
    pre-filters, so this is mostly a defence-in-depth check.
    """
    p = Path(path).resolve()
    accepted_root = ACCEPTED_PROMPTS_DIR.resolve()
    try:
        rel = p.relative_to(accepted_root)
    except ValueError:
        return None
    if len(rel.parts) != 2 or not rel.parts[1].endswith(".json"):
        return None
    return rel.parts[0]


def load_candidate(path: Path) -> CandidatePrompt:
    """Load a CandidatePrompt from a versioned-prompt JSON file."""
    return _candidate_from_json(json.loads(Path(path).read_text()))


# ── The gate ────────────────────────────────────────────────────────────────


def evaluate_paths(paths: list[Path]) -> list[GateResult]:
    """Run the gate over a list of changed prompt paths.

    Returns one GateResult per evaluated path. Paths whose target is
    unknown to the gate produce a failing result (fail-closed).

    The gate refuses to evaluate prompts whose ``status`` is not
    ``ACCEPTED`` — the gate exists to gate promotion, and a CANDIDATE
    file landing in ``agent/prompts/`` (committed) is itself a policy
    violation that should fail the commit.
    """
    out: list[GateResult] = []
    for raw in paths:
        path = Path(raw)

        # Refuse symlinks under agent/prompts/ — they have no
        # legitimate use here and would let the gate JSON-parse a file
        # outside the prompt store. The store itself never creates
        # symlinks, so any symlinked path is operator-introduced.
        if path.is_symlink():
            out.append(GateResult(
                path=path, target="(unknown)",
                score=0.0, threshold=1.0, passed=False,
                notes="path is a symlink; refused (use real files only)",
            ))
            continue

        target = target_from_path(path)
        if target is None:
            out.append(GateResult(
                path=path, target="(unknown)",
                score=0.0, threshold=1.0, passed=False,
                notes="path outside agent/prompts/<target>/<version>.json layout",
            ))
            continue

        try:
            candidate = load_candidate(path)
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
            out.append(GateResult(
                path=path, target=target,
                score=0.0, threshold=1.0, passed=False,
                notes=f"failed to load candidate: {type(e).__name__}: {e}",
            ))
            continue

        if candidate.status != PromptStatus.ACCEPTED:
            out.append(GateResult(
                path=path, target=target,
                score=0.0, threshold=1.0, passed=False,
                notes=(
                    f"prompt has status={candidate.status.value!r}; "
                    "only ACCEPTED prompts may live under agent/prompts/. "
                    "Promote via the optimizer flow, do not edit directly."
                ),
            ))
            continue

        # PII gate (defence-in-depth): check both the on-disk
        # ``sanitization`` field AND re-scan the prompt text. The
        # storage layer already refuses ACCEPTED + non-empty
        # sanitization, but a hand-edited file could set
        # ``sanitization: []`` while leaving PII in ``prompt_text`` —
        # re-scanning catches that.
        live_findings = _sanitize_scan(candidate.prompt_text)
        if candidate.sanitization or live_findings:
            combined = list(dict.fromkeys(candidate.sanitization + live_findings))
            out.append(GateResult(
                path=path, target=target,
                score=0.0, threshold=1.0, passed=False,
                notes=(
                    "ACCEPTED prompt has PII findings (recorded or live-rescan): "
                    f"{combined!r}"
                ),
            ))
            continue

        if target not in EVAL_RUNNERS:
            out.append(GateResult(
                path=path, target=target,
                score=0.0, threshold=1.0, passed=False,
                notes=(
                    f"no eval runner registered for target {target!r}. "
                    f"Known targets: {sorted(EVAL_RUNNERS)!r}. "
                    "Register one before promoting prompts for this target."
                ),
            ))
            continue

        try:
            threshold = threshold_for(target)
        except KeyError:
            out.append(GateResult(
                path=path, target=target,
                score=0.0, threshold=1.0, passed=False,
                notes=(
                    f"no threshold defined for target {target!r}. "
                    f"Set PEPPER_GATE_THRESHOLD_{target.upper()} or add a default "
                    "in eval_gate.DEFAULT_THRESHOLDS."
                ),
            ))
            continue

        try:
            score = float(EVAL_RUNNERS[target](candidate))
        except Exception as e:  # pragma: no cover — defensive
            out.append(GateResult(
                path=path, target=target,
                score=0.0, threshold=threshold, passed=False,
                notes=f"eval runner raised: {type(e).__name__}: {e}",
            ))
            continue

        # A misbehaving runner returning ``inf`` would silently
        # auto-pass any threshold; NaN sneaks past `>=` only by
        # always being False, which fails closed (acceptable). Reject
        # any non-finite or out-of-range score loudly.
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            out.append(GateResult(
                path=path, target=target,
                score=score, threshold=threshold, passed=False,
                notes=f"runner returned out-of-range score {score!r}; expected finite [0, 1]",
            ))
            continue

        passed = score >= threshold
        out.append(GateResult(
            path=path, target=target,
            score=score, threshold=threshold, passed=passed,
            notes="" if passed else f"score {score:.4f} below threshold {threshold:.4f}",
        ))
    return out


def bypassed() -> bool:
    """True iff the operator set the bypass env var."""
    return os.environ.get(BYPASS_ENV_VAR, "0") == "1"
