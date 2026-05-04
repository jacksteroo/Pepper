"""Operator CLI for the continuity-of-self rubric (#57).

Usage::

    .venv/bin/python scripts/score_continuity.py --label baseline
    .venv/bin/python scripts/score_continuity.py --label end_of_epic

Reads the last 7 days of traces, runs `select_sample` + `score_window`,
and writes `eval_results/continuity_<date>.json`. The score is the
auto-detector baseline; the operator is expected to open the JSON and
fill in the language-level dimensions (1, 2, 4) by manual review or
LLM-judge — see `docs/continuity-of-self-rubric.md`.

Pure file I/O at the script layer; the heavy lifting lives in
`agents.reflector.continuity_eval` and is fully tested.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Add the repo root so the script runs without an install.
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from agents.reflector.continuity_eval import (  # noqa: E402
    DEFAULT_EVAL_RESULTS_DIR,
    SAMPLE_SIZE_DEFAULT,
    ScoringInputs,
    TraceForScoring,
    score_window,
    select_sample,
    write_result,
)


async def _fetch_traces(
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[TraceForScoring]:
    """Pull traces from the local DB into the scoring shape.

    Returns an empty list if the DB is not initialised — the operator
    sees a sample_size=0 result which is still useful as a sanity
    check that the harness ran.
    """
    try:
        from agent import db as _db
        from agent.config import settings
        from agent.traces import TraceRepository
    except Exception as exc:
        print(f"[continuity_eval] could not import trace store: {exc}")
        return []

    if _db._session_factory is None:
        try:
            await _db.init_db(settings)
        except Exception as exc:
            print(f"[continuity_eval] init_db failed: {exc}")
            return []

    factory = _db._session_factory
    if factory is None:
        return []

    async with factory() as session:
        repo = TraceRepository(session)
        rows = await repo.query(
            window_start=window_start,
            window_end=window_end,
            limit=1000,
        )

    out: list[TraceForScoring] = []
    for r in rows:
        out.append(
            TraceForScoring(
                trace_id=r.trace_id,
                created_at=r.created_at,
                input=r.input or "",
                output=r.output or "",
                trigger_source=str(r.trigger_source),
                tools_called=list(r.tools_called or []),
                user_reaction=r.user_reaction,
                assembled_context=r.assembled_context or {},
            )
        )
    return out


async def _fetch_thumbs_counts(*, window_start: datetime) -> tuple[int, int]:
    """Read the explicit-thumbs counts from `wait_feedback`.

    Conservative: returns (0, 0) on any failure so the auto-detector
    still produces a reasonable score, just without thumbs adjustment.
    """
    try:
        from agent import db as _db
        from agents.reflector.wait_evaluator import (
            SIGNAL_THUMBS,
            WaitFeedbackRepository,
        )
    except Exception:
        return 0, 0
    factory = _db._session_factory
    if factory is None:
        return 0, 0
    up = down = 0
    async with factory() as session:
        repo = WaitFeedbackRepository(session)
        records = await repo.list_recent(days=7)
        for r in records:
            if r.signal_type != SIGNAL_THUMBS:
                continue
            if r.created_at < window_start:
                continue
            if r.signal_value >= 0.5:
                up += 1
            else:
                down += 1
    return up, down


async def _amain(args: argparse.Namespace) -> int:
    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=args.days)

    traces = await _fetch_traces(
        window_start=window_start, window_end=window_end
    )
    sample = select_sample(
        traces, sample_size=args.sample_size, seed=args.seed
    )
    thumbs_up, thumbs_down = await _fetch_thumbs_counts(window_start=window_start)
    result = score_window(
        sample,
        window_start=window_start,
        window_end=window_end,
        inputs=ScoringInputs(
            explicit_thumbs_up=thumbs_up,
            explicit_thumbs_down=thumbs_down,
        ),
        notes=args.label,
    )
    out_path = write_result(
        result,
        out_dir=DEFAULT_EVAL_RESULTS_DIR,
        prefix=f"continuity_{args.label}",
    )
    print(f"[continuity_eval] wrote {out_path}")
    print(f"  sample_size={result.sample_size}")
    print(f"  total={result.total}")
    for d, v in result.mean_per_dimension.items():
        print(f"  {d}: {v}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument(
        "--label",
        required=True,
        choices=["baseline", "end_of_epic", "weekly", "ad_hoc"],
        help=(
            "Phase label written into the result JSON's `notes` field "
            "and used as the file-name prefix."
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Trace window in days (default 7).",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=SAMPLE_SIZE_DEFAULT,
        help=f"Stratified sample size (default {SAMPLE_SIZE_DEFAULT}).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional random seed for reproducibility.",
    )
    args = parser.parse_args()
    rc = asyncio.run(_amain(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
