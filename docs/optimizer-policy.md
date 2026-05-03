# Optimizer policy

This document is the canonical reference for the prompt-optimization
loop's gating policy: which targets the gate evaluates, which
thresholds they must clear, and how the bypass is supposed to be used.

The loop itself is described in [ADR-0007](adr/0007-optimizer-framework-gepa.md).
The implementing modules are under `agent/optimizer/`.

## What the gate does

The pre-commit hook
[`scripts/git-hooks/pre-commit-prompt-eval-gate`](../scripts/git-hooks/pre-commit-prompt-eval-gate)
detects staged file changes under `agent/prompts/<target>/<version>.json`
and shells out to `python -m agent.optimizer gate --paths <…>`.

For each changed prompt, the gate:

1. Refuses any prompt whose status is not `ACCEPTED` (CANDIDATE prompts
   live under `data/optimizer/candidates/`, not `agent/prompts/`).
2. Refuses any prompt whose `sanitization` field is non-empty
   (defence-in-depth — `PromptStore.put` already enforces this).
3. Looks up the per-target eval runner from
   `agent.optimizer.eval_gate.EVAL_RUNNERS`.
4. Runs the eval and compares the score to the target's threshold.
5. Exits non-zero if any prompt fails.

## Targets and thresholds

| target              | runner                                   | default threshold | rationale                                                  |
| ------------------- | ---------------------------------------- | ----------------- | ---------------------------------------------------------- |
| `router_classifier` | registered by #47                        | `0.85`            | Matches existing `pre-commit-router-eval` floor.           |
| `context_assembly`  | registered by #46                        | `0.65`            | Baseline-from-#30 retrieval Recall@5; #46 lands the runner.|
| `reflector_rubric`  | registered by #42 follow-up              | `0.70`            | Baseline-from-#42 rubric mean score.                       |

This PR ships the gate dispatcher and the threshold table. The
runners themselves land with the targets that need them — until then,
the gate fails closed for any prompt under `agent/prompts/<target>/`
because `EVAL_RUNNERS` has no entry for it. That's the correct safety
bias: prompts cannot land for a target whose evaluator does not yet
exist.

Env-var threshold overrides are validated to be finite floats in
`[0, 1]`; anything else (NaN, `-inf`, negative, `2.0`) is rejected.
Runner return values are validated to the same range.

Per-target overrides via env var:

```bash
PEPPER_GATE_THRESHOLD_ROUTER_CLASSIFIER=0.90  # raise the router floor for one commit
```

The env-var path is for tightening the floor on a per-commit basis
(e.g. before a release). To loosen it durably, edit
`DEFAULT_THRESHOLDS` in `agent/optimizer/eval_gate.py` and document
the change in this file.

Unknown targets fail closed: a prompt for a target with no registered
runner will block the commit. To add a new target, register an
`EvalRunner` via `agent.optimizer.eval_gate.register_runner` (see the
docstring) and add a row to the table above.

## Bypass

```bash
PEPPER_BYPASS_EVAL_GATE=1 git commit ...
```

The bypass requires both hooks to be installed (see "Installing the
hooks" below). The mechanism splits across two hooks because
`pre-commit` runs *before* Git writes the commit-message file for the
new commit, so the trailer cannot live there:

1. `pre-commit-prompt-eval-gate` reads the env var and short-circuits
   the eval, printing `gate: BYPASSED via PEPPER_BYPASS_EVAL_GATE=1`
   to stderr.
2. `prepare-commit-msg-eval-gate` runs after Git creates the message
   file, sees the env var, and idempotently appends a
   `Bypassed-Eval-Gate: yes` trailer so the bypass is visible in
   `git log` after the fact.

The bypass is for **emergency rollback only** — a known-good prompt
needs to land immediately while the eval set is broken or unavailable.
It is not for "the gate is annoying me today." Repeated use should
either result in a threshold change (documented here) or a fix to the
underlying eval runner.

## Promotion lifecycle

```text
candidate (data/optimizer/candidates/<target>/<v>.json)
  ↓  optimizer accepts via PromptStore.put(status=ACCEPTED)  ←─ sanitizer gate (hard)
accepted (agent/prompts/<target>/<v>.json)
  ↓  git add + git commit                                    ←─ this gate (per-target eval)
committed
  ↓  production telemetry triggers rollback (if needed)
rolled_back (status=ROLLED_BACK in same file)
```

The two gates are intentionally distinct:

- `PromptStore.put` blocks PII leakage at the moment of promotion.
- The pre-commit eval gate blocks regressions at the moment of commit.

Bypassing one does not bypass the other. There is no env var to bypass
the PII gate — that is a hard refusal.

## Adding a new target

1. Implement an `EvalRunner` (callable taking a `CandidatePrompt`,
   returning a `float` in `[0, 1]`, higher-is-better).
2. Register it from your module: `register_runner("my_target", my_runner)`.
3. Add a row to the threshold table above.
4. Add a default to `DEFAULT_THRESHOLDS` in `agent/optimizer/eval_gate.py`.
5. Update tests in `agent/tests/optimizer/test_eval_gate.py`.

## Installing the hooks

The hooks are not installed automatically — Pepper does not ship a
`pre-commit` framework dispatcher. Install both:

```bash
ln -sf ../../scripts/git-hooks/pre-commit-prompt-eval-gate .git/hooks/pre-commit
ln -sf ../../scripts/git-hooks/prepare-commit-msg-eval-gate .git/hooks/prepare-commit-msg
```

If you already have `pre-commit-router-eval` installed, both
pre-commit hooks need to fire. Either chain them via a small
dispatcher script or replace the single `pre-commit` symlink with a
script that runs both.
