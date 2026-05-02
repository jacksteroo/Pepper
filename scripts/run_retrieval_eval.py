#!/usr/bin/env python3
"""Local retrieval-eval runner — Epic 02, issue #30.

Runs the gitignored real eval set against the live memory DB, prints
aggregate metrics, and writes them to `eval_results/`. The script never
logs query text or per-query results outside of the local terminal.

Usage:
    .venv/bin/python scripts/run_retrieval_eval.py --mode baseline
    .venv/bin/python scripts/run_retrieval_eval.py --mode after --tag bm25
    .venv/bin/python scripts/run_retrieval_eval.py --mode gate
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path

_TAG_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Allow running as `python scripts/run_retrieval_eval.py` from anywhere by
# putting the repo root on sys.path before importing agent.* modules.
_REPO_ROOT_FOR_IMPORTS = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT_FOR_IMPORTS) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_FOR_IMPORTS))

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_SET_PATH = REPO_ROOT / "agent" / "tests" / "retrieval_eval_set.jsonl"
EVAL_RESULTS_DIR = REPO_ROOT / "eval_results"
BASELINE_JSON = EVAL_RESULTS_DIR / "baseline.json"


def _render_markdown(report, mode: str, tag: str | None) -> str:
    title = f"Retrieval eval — {mode}" + (f" ({tag})" if tag else "")
    lines = [
        f"# {title}",
        "",
        f"- Date: {date.today().isoformat()}",
        f"- Total queries: {report.total_queries}",
        f"- MRR: {report.mrr:.4f}",
        "",
        "## Recall@K (overall)",
        "",
        "| K | Recall |",
        "|---|--------|",
    ]
    for k in sorted(report.recall_at_k):
        lines.append(f"| {k} | {report.recall_at_k[k]:.4f} |")
    lines.extend(["", "## Per-category", "", "| Category | n | Recall@5 | MRR |", "|---|---|---|---|"])
    for cat in sorted(report.per_category):
        row = report.per_category[cat]
        lines.append(
            f"| {cat} | {int(row.get('n', 0))} | "
            f"{row.get('recall@5', 0.0):.4f} | {row.get('mrr', 0.0):.4f} |"
        )
    lines.append("")
    return "\n".join(lines)


async def _build_memory_retriever():
    """Adapt MemoryManager.search_recall into the eval Retriever protocol.

    TODO(epic02): once #27 (BM25), #28 (RRF), and #29 (recency) land,
    add a `--retriever-mode` argument and dispatch to the corresponding
    MemoryManager method here so before/after deltas can be measured
    without redeploying.
    """
    from agent.config import settings  # noqa: WPS433
    from agent.db import get_session_factory, init_db
    from agent.llm import LLMClient
    from agent.memory import MemoryManager

    await init_db(settings)
    llm = LLMClient(settings)
    mm = MemoryManager(llm_client=llm, db_session_factory=get_session_factory())

    async def retrieve(eval_query, k: int) -> list[int]:
        results = await mm.search_recall(eval_query.query, limit=k)
        return [int(r["id"]) for r in results]

    return retrieve


async def _run(mode: str, tag: str | None, threshold_pp: float) -> int:
    from agent.tests.retrieval_eval import compare_to_baseline, load_eval_set, run_eval

    if not EVAL_SET_PATH.exists():
        print(
            f"missing {EVAL_SET_PATH.relative_to(REPO_ROOT)} — copy from "
            f"retrieval_eval_set.example.jsonl and populate locally.",
            file=sys.stderr,
        )
        return 2

    eval_set = load_eval_set(EVAL_SET_PATH)
    retriever = await _build_memory_retriever()
    report = await run_eval(eval_set, retriever)

    EVAL_RESULTS_DIR.mkdir(exist_ok=True)
    md = _render_markdown(report, mode=mode, tag=tag)
    print(md)

    if mode == "baseline":
        BASELINE_JSON.write_text(json.dumps(report.to_dict(), indent=2) + "\n")
        out_md = EVAL_RESULTS_DIR / f"retrieval_baseline_{date.today().isoformat()}.md"
        out_md.write_text(md + "\n")
        print(f"\nwrote {BASELINE_JSON.relative_to(REPO_ROOT)} and {out_md.relative_to(REPO_ROOT)}")
        return 0

    if mode == "after":
        slug = tag or "after"
        out_md = EVAL_RESULTS_DIR / f"retrieval_after_{slug}_{date.today().isoformat()}.md"
        out_md.write_text(md + "\n")
        print(f"\nwrote {out_md.relative_to(REPO_ROOT)}")
        return 0

    if mode == "gate":
        if not BASELINE_JSON.exists():
            print(f"missing {BASELINE_JSON.relative_to(REPO_ROOT)} — run --mode baseline first.", file=sys.stderr)
            return 2
        baseline = json.loads(BASELINE_JSON.read_text())
        verdict = compare_to_baseline(report, baseline, min_recall_at_5_lift_pp=threshold_pp)
        print("\n## Gate verdict\n")
        print(json.dumps(verdict, indent=2))
        return 0 if verdict["passes_gate"] else 1

    print(f"unknown mode: {mode}", file=sys.stderr)
    return 2


def main() -> int:
    from agent.tests.retrieval_eval import DEFAULT_GATE_THRESHOLD_PP

    parser = argparse.ArgumentParser(description="Run the retrieval eval.")
    parser.add_argument("--mode", choices=("baseline", "after", "gate"), required=True)
    parser.add_argument(
        "--tag",
        default=None,
        help="label for --mode after (e.g. bm25, rrf, recency); "
        "alphanumerics/dash/underscore only",
    )
    parser.add_argument(
        "--threshold-pp",
        type=float,
        default=DEFAULT_GATE_THRESHOLD_PP,
        help="Recall@5 lift in percentage points required to pass --mode gate",
    )
    args = parser.parse_args()
    if args.tag is not None and not _TAG_RE.match(args.tag):
        print(
            f"--tag must match {_TAG_RE.pattern} (got {args.tag!r}); "
            "filenames are written under eval_results/ and must not contain "
            "path separators.",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(_run(args.mode, args.tag, args.threshold_pp))


if __name__ == "__main__":
    raise SystemExit(main())
