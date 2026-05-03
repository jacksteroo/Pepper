"""CLI entrypoint: ``python -m agent.optimizer optimize --target ... --window 7d``.

Operator-triggered. Runs the optimizer end-to-end against traces in the
given window and persists candidate prompts. Promotion (candidate →
accepted) is the eval gate's concern (#48); this CLI never auto-applies.

Usage:

    python -m agent.optimizer optimize \
        --target context_assembly \
        --archetype orchestrator \
        --window 7d \
        --baseline-prompt-file path/to/baseline.txt \
        [--prompt-version v3] \
        [--seed 0] \
        [--runner gepa|deterministic]

The ``deterministic`` runner is the smoke-test path — it does not
require GEPA installed. Production runs use ``gepa``.

Why this is ``python -m`` and not a top-level ``pepper`` CLI: #45's
spec mentions ``pepper optimize ...`` but the repo has no ``pepper``
binary. Adding a top-level CLI is out of scope for #45; the tracked
follow-up is captured in the PR description.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import structlog

from agent.optimizer.audit import AuditLog
from agent.optimizer.datasets import build_from_repo
from agent.optimizer.runners import (
    DeterministicRunner,
    GepaRunner,
    OptimizerAdapter,
    OptimizerRunner,
    run_optimizer,
)
from agent.optimizer.schema import TraceExample
from agent.optimizer.storage import PromptStore
from agent.traces.schema import Archetype

logger = structlog.get_logger(__name__)


_WINDOW_RE = re.compile(r"^(\d+)([dhm])$")

# Matches sanitizer.MAX_SCAN_BYTES — a 256KB cap is generous for any
# real prompt and keeps operator footguns (`/dev/zero`, multi-GB log
# files) from OOM-ing the optimizer.
MAX_BASELINE_PROMPT_BYTES: int = 256 * 1024


def parse_window(s: str) -> timedelta:
    """Parse ``7d``, ``12h``, ``30m`` into a ``timedelta``."""
    m = _WINDOW_RE.match(s)
    if not m:
        raise argparse.ArgumentTypeError(
            f"window must match {_WINDOW_RE.pattern!r}, got {s!r}",
        )
    n = int(m.group(1))
    unit = m.group(2)
    return {
        "d": timedelta(days=n),
        "h": timedelta(hours=n),
        "m": timedelta(minutes=n),
    }[unit]


class _NullAdapter:
    """Stand-in adapter used by the CLI when no target adapter is registered.

    Targets (#46, #47) register real adapters under
    ``agent/optimizer/adapters/<target>.py``. Until those land the CLI
    uses this adapter for the deterministic smoke path so the
    end-to-end wiring is exercisable.
    """

    def __init__(self, target: str) -> None:
        self.target = target

    def score(self, prompt_text: str, example: TraceExample) -> float:
        # Length-normalised lexical overlap with the example output.
        # Not meaningful as a real metric — exists only so the smoke
        # path produces non-trivial scores.
        if not example.output:
            return 0.0
        prompt_tokens = set(prompt_text.lower().split())
        out_tokens = set(example.output.lower().split())
        if not out_tokens:
            return 0.0
        return len(prompt_tokens & out_tokens) / max(len(out_tokens), 1)

    def mutate(self, prompt_text: str, examples, seed: int) -> list[str]:
        # Three deterministic mutations: append a "be concise" tail,
        # prepend a "follow the format below" head, and a no-op control.
        return [
            prompt_text + "\n\nBe concise.",
            "Follow the format demonstrated below.\n\n" + prompt_text,
            prompt_text + "\n",
        ]


def build_runner(name: str, *, reflection_lm: str | None = None) -> OptimizerRunner:
    if name == "gepa":
        if not reflection_lm:
            raise argparse.ArgumentTypeError(
                "--runner=gepa requires --reflection-lm (e.g. 'ollama/llama3'); "
                "see ADR-0007 for the local-only invariant.",
            )
        return GepaRunner(reflection_lm=reflection_lm)
    if name == "deterministic":
        return DeterministicRunner()
    raise argparse.ArgumentTypeError(f"unknown runner: {name!r}")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent.optimizer",
        description="Pepper prompt-optimization CLI (Epic 05, #45).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    opt = sub.add_parser("optimize", help="Run the optimizer over a trace window.")
    opt.add_argument("--target", required=True,
                     help="Target name, e.g. 'context_assembly' or 'router_classifier'.")
    opt.add_argument("--archetype", required=True,
                     choices=[a.value for a in Archetype],
                     help="Trace archetype to filter on.")
    opt.add_argument("--window", required=True, type=parse_window,
                     help="Lookback window (e.g. '7d', '12h', '30m').")
    opt.add_argument("--baseline-prompt-file", required=True, type=Path,
                     help="Path to a text file containing the baseline prompt.")
    opt.add_argument("--prompt-version", default=None,
                     help="Filter traces by prompt_version (default: any).")
    opt.add_argument("--seed", type=int, default=0)
    opt.add_argument("--runner", choices=["gepa", "deterministic"],
                     default="deterministic",
                     help="Optimizer runner. Default 'deterministic' for hermetic smoke runs.")
    opt.add_argument("--reflection-lm", default=None,
                     help="Local LM identifier for GEPA's reflection step "
                          "(e.g. 'ollama/llama3'). Required for --runner=gepa. "
                          "Per ADR-0007, must point at a local model — frontier "
                          "API identifiers are rejected at runner construction.")
    opt.add_argument("--candidates-dir", type=Path, default=None,
                     help="Override PromptStore base dir (default: data/optimizer/candidates).")
    opt.add_argument("--audit-log", type=Path, default=None,
                     help="Override audit log path (default: data/optimizer/audit.jsonl).")
    opt.add_argument("--limit", type=int, default=200,
                     help="Max traces to pull (default: 200, capped by repo).")

    inspect = sub.add_parser("show-candidates",
                             help="List candidates for a target.")
    inspect.add_argument("--target", required=True)
    inspect.add_argument("--candidates-dir", type=Path, default=None)

    gate = sub.add_parser(
        "gate",
        help="Run the pre-commit eval gate over a list of versioned-prompt paths.",
    )
    gate.add_argument(
        "--paths", nargs="+", required=True, type=Path,
        help="Paths under agent/prompts/<target>/<version>.json to evaluate.",
    )

    return parser


async def _cmd_optimize(args, *, adapter_factory=None) -> int:
    """Run an optimization pass.

    ``adapter_factory`` is injected by tests to swap the real adapter in
    for the ``_NullAdapter`` smoke fallback. Production callers leave
    it as ``None``.
    """
    if not args.baseline_prompt_file.exists():
        print(f"baseline prompt file not found: {args.baseline_prompt_file}",
              file=sys.stderr)
        return 2
    # Cap baseline prompt size to defend against `--baseline-prompt-file
    # /dev/zero` style operator footguns. Real prompts are well under
    # 64KB; legitimate optimization-target sizes never approach this.
    baseline_size = args.baseline_prompt_file.stat().st_size
    if baseline_size > MAX_BASELINE_PROMPT_BYTES:
        print(
            f"baseline prompt file too large: {baseline_size} bytes "
            f"> {MAX_BASELINE_PROMPT_BYTES}",
            file=sys.stderr,
        )
        return 2
    baseline_prompt = args.baseline_prompt_file.read_text()

    archetype = Archetype(args.archetype)
    window_until = datetime.now(timezone.utc)
    window_since: Optional[datetime] = window_until - args.window

    # Adapter resolution — see _NullAdapter docstring.
    if adapter_factory is not None:
        adapter: OptimizerAdapter = adapter_factory(args.target)
    else:
        print(
            f"warn: no target adapter registered for {args.target!r}; "
            "using _NullAdapter (smoke path only — not a real metric)",
            file=sys.stderr,
        )
        adapter = _NullAdapter(args.target)

    # Local import so the CLI module is importable even without the DB layer
    # (e.g. running --help in a stripped environment).
    from agent.db import get_session  # noqa: PLC0415
    from agent.traces.repository import TraceRepository  # noqa: PLC0415

    async with get_session() as session:
        repo = TraceRepository(session)
        examples = await build_from_repo(
            repo,
            archetype=archetype,
            prompt_version=args.prompt_version,
            since=window_since,
            until=window_until,
            limit=args.limit,
        )

    runner = build_runner(args.runner, reflection_lm=args.reflection_lm)
    store = PromptStore(args.candidates_dir) if args.candidates_dir else PromptStore()
    audit = AuditLog(args.audit_log) if args.audit_log else AuditLog()

    record, candidates = run_optimizer(
        runner=runner,
        adapter=adapter,
        examples=examples,
        baseline_prompt=baseline_prompt,
        seed=args.seed,
        archetype=archetype.value,
        prompt_version_filter=args.prompt_version or "",
        window_since=window_since,
        window_until=window_until,
        store=store,
        audit_log=audit,
    )
    print(
        f"optimizer run {record.run_id}: "
        f"{record.candidate_count} candidates "
        f"(dataset_size={record.dataset_size}, "
        f"runner={record.runner_class}, seed={record.seed})",
    )
    if record.error:
        print(f"  error: {record.error}", file=sys.stderr)
        return 1
    for c in candidates[:5]:
        flag = " [PII?]" if c.sanitization else ""
        print(f"  {c.version_hash}  score={c.eval_score:.4f}{flag}")
    return 0


def _cmd_show_candidates(args) -> int:
    store = PromptStore(args.candidates_dir) if args.candidates_dir else PromptStore()
    candidates = store.list(args.target)
    if not candidates:
        print(f"(no candidates for target {args.target!r})")
        return 0
    for c in candidates:
        flag = " [PII?]" if c.sanitization else ""
        print(
            f"{c.version_hash}  status={c.status.value}  "
            f"score={c.eval_score:.4f}  parent={c.parent_version or '(seed)'}{flag}",
        )
    return 0


def _cmd_gate(args) -> int:
    """Run the pre-commit eval gate.

    Exit codes:
      0 — every path passes its target threshold (or bypass is set).
      1 — at least one path failed.
      2 — gate misuse (no paths, etc).
    """
    from agent.optimizer.eval_gate import (  # noqa: PLC0415 — local import to keep CLI surface light
        BYPASS_ENV_VAR,
        bypassed,
        evaluate_paths,
    )

    if not args.paths:
        print("gate: no paths supplied", file=sys.stderr)
        return 2

    if bypassed():
        print(
            f"gate: BYPASSED via {BYPASS_ENV_VAR}=1 — "
            "operator must justify in commit message",
            file=sys.stderr,
        )
        return 0

    results = evaluate_paths(list(args.paths))
    failed = [r for r in results if not r.passed]
    for r in results:
        flag = "PASS" if r.passed else "FAIL"
        line = (
            f"gate: {flag}  target={r.target}  score={r.score:.4f}  "
            f"threshold={r.threshold:.4f}  path={r.path}"
        )
        if r.notes:
            line += f"  ({r.notes})"
        print(line, file=sys.stderr if not r.passed else sys.stdout)
    if failed:
        print(
            f"gate: {len(failed)} of {len(results)} prompts failed; "
            f"bypass with {BYPASS_ENV_VAR}=1 only for emergency rollback",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.cmd == "optimize":
        return asyncio.run(_cmd_optimize(args))
    if args.cmd == "show-candidates":
        return _cmd_show_candidates(args)
    if args.cmd == "gate":
        return _cmd_gate(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
