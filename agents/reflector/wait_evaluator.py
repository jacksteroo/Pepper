"""Nightly evaluator for completed wait-window assessments — Issue #56.

For each wait-trace whose `until` window has passed (or is > 24 h old with
no `until`), we compute two automatic signals:

  WAS_STILL_RELEVANT
    Any trace within ±24 h of `until` (or ±24 h of created_at when there
    is no `until`) whose embedding cosine similarity exceeds SIMILARITY_THRESHOLD
    is treated as evidence the situation still warranted surfacing.
    similarity = 1 - cosine_distance (pgvector uses distance, lower = closer).

  BROUGHT_UP_BY_JACK
    Any trace within 7 days of the wait, with trigger_source='user' and
    similarity ≥ SIMILARITY_THRESHOLD, suggests Jack independently surfaced
    the topic. If yes → wait was correct. If no → ambiguous.

These are *signals*, not automated learning. The reflector summarises
patterns; Jack adjusts strategies manually.

Persistence is JSON-file-backed for v0 (data/wait_feedback.json, gitignored).
The file is a list of WaitFeedback records serialised as dicts.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import structlog

logger = structlog.get_logger(__name__)

# Cosine similarity threshold for "same topic" detection.
# pgvector returns cosine DISTANCE (0 = identical, 2 = opposite).
# distance < (1 - threshold) → similarity > threshold.
SIMILARITY_THRESHOLD: float = 0.7
SIMILARITY_DISTANCE_CAP: float = 1.0 - SIMILARITY_THRESHOLD  # 0.3

# Window around `until` (or wait created_at) to look for related traces.
RELEVANCE_WINDOW_HOURS: int = 24

# Window to look for Jack bringing up the topic independently.
JACK_WINDOW_DAYS: int = 7

# Default feedback store path.
_DEFAULT_FEEDBACK_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "wait_feedback.json"
)


@dataclass
class WaitFeedback:
    """Feedback record for a single wait-trace evaluation."""

    wait_trace_id: str
    signal_type: str  # "was_still_relevant" | "brought_up_by_jack" | "user_thumbs"
    signal_value: bool  # True = correct wait, False = incorrect/missed
    confidence: float  # 0.0–1.0; 1.0 for explicit user signals
    evaluated_at: str  # ISO8601 UTC
    notes: str = ""


def _load_feedback(path: str) -> list[dict]:
    """Load persisted feedback records from disk. Returns [] on missing/corrupt."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
        return []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_feedback(records: list[dict], path: str) -> None:
    """Persist feedback records to disk atomically."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("wait_feedback_save_failed", error=str(exc)[:200])
        try:
            os.unlink(tmp)
        except OSError:
            pass


async def evaluate_completed_waits(
    session,
    traces_repo,
    since: datetime,
    feedback_path: str = _DEFAULT_FEEDBACK_PATH,
) -> list[WaitFeedback]:
    """Evaluate all wait-traces created since `since` whose window has passed.

    `session` is an AsyncSession (passed in by the reflector nightly pass).
    `traces_repo` is a TraceRepository instance bound to that session.

    Returns newly-created WaitFeedback records (also persisted to disk).
    """
    now = datetime.now(timezone.utc)

    # 1. Load all traces in the window to find wait-traces.
    # We use with_payload=True to get tools_called + assembled_context.
    all_traces = await traces_repo.query(
        since=since,
        until=now,
        limit=500,
        with_payload=True,
    )

    wait_traces = [
        t for t in all_traces
        if (t.assembled_context or {}).get("is_wait") is True
    ]

    if not wait_traces:
        return []

    # 2. Load existing feedback so we don't re-evaluate the same trace.
    existing = _load_feedback(feedback_path)
    already_evaluated: set[str] = {
        rec.get("wait_trace_id", "") for rec in existing
        if rec.get("signal_type") in ("was_still_relevant", "brought_up_by_jack")
    }

    new_feedbacks: list[WaitFeedback] = []

    for wt in wait_traces:
        if wt.trace_id in already_evaluated:
            continue

        # Extract `until` from the wait tool args if present.
        wait_args = _extract_wait_args(wt)
        until_str = wait_args.get("until")
        reason = wait_args.get("reason", "")

        # Determine the pivot point: `until` if parseable, else created_at.
        pivot = _parse_until(until_str, wt.created_at)

        # Only evaluate if the window has passed (pivot + 24 h < now).
        if pivot + timedelta(hours=RELEVANCE_WINDOW_HOURS) > now:
            continue

        # Skip waits with no embedding — can't compare topics.
        if wt.embedding is None:
            logger.debug("wait_evaluator_skip_no_embedding", trace_id=wt.trace_id)
            continue

        # 3. Find traces near the pivot to check relevance.
        relevance_window_start = pivot - timedelta(hours=RELEVANCE_WINDOW_HOURS)
        relevance_window_end = pivot + timedelta(hours=RELEVANCE_WINDOW_HOURS)

        nearby = await traces_repo.query(
            since=relevance_window_start,
            until=relevance_window_end,
            limit=50,
            with_payload=False,
        )
        # For similarity we need embeddings; re-fetch with payload.
        # Only fetch ids that have embeddings to avoid wasted round-trips.
        nearby_ids_with_embedding = [
            t.trace_id for t in nearby
            if t.trace_id != wt.trace_id
        ]

        was_relevant = False
        for tid in nearby_ids_with_embedding[:20]:  # cap at 20 to avoid N+1 storms
            candidate = await traces_repo.get_by_id(tid)
            if candidate is None or candidate.embedding is None:
                continue
            dist = _cosine_distance(wt.embedding, candidate.embedding)
            if dist < SIMILARITY_DISTANCE_CAP:
                was_relevant = True
                break

        fb_relevant = WaitFeedback(
            wait_trace_id=wt.trace_id,
            signal_type="was_still_relevant",
            signal_value=was_relevant,
            confidence=0.6,
            evaluated_at=now.isoformat(),
            notes=f"reason={reason!r} until={until_str!r}",
        )
        new_feedbacks.append(fb_relevant)

        # 4. Check if Jack independently brought up the topic within 7 days.
        jack_window_end = wt.created_at + timedelta(days=JACK_WINDOW_DAYS)
        jack_window_end = min(jack_window_end, now)

        from agent.traces.schema import TriggerSource
        jack_traces = await traces_repo.query(
            trigger_source=TriggerSource.USER,
            since=wt.created_at,
            until=jack_window_end,
            limit=100,
            with_payload=False,
        )
        jack_ids = [t.trace_id for t in jack_traces if t.trace_id != wt.trace_id]

        brought_up = False
        for tid in jack_ids[:30]:
            candidate = await traces_repo.get_by_id(tid)
            if candidate is None or candidate.embedding is None:
                continue
            dist = _cosine_distance(wt.embedding, candidate.embedding)
            if dist < SIMILARITY_DISTANCE_CAP:
                brought_up = True
                break

        fb_brought = WaitFeedback(
            wait_trace_id=wt.trace_id,
            signal_type="brought_up_by_jack",
            signal_value=brought_up,
            confidence=0.5,  # ambiguous if False
            evaluated_at=now.isoformat(),
            notes=f"7d window; reason={reason!r}",
        )
        new_feedbacks.append(fb_brought)

        logger.info(
            "wait_evaluated",
            trace_id=wt.trace_id,
            was_relevant=was_relevant,
            brought_up=brought_up,
        )

    if new_feedbacks:
        combined = existing + [asdict(fb) for fb in new_feedbacks]
        _save_feedback(combined, feedback_path)
        logger.info("wait_feedback_saved", n_new=len(new_feedbacks), path=feedback_path)

    return new_feedbacks


def record_user_thumbs(
    wait_trace_id: str,
    user_signal: str,  # "correct" | "incorrect"
    notes: str = "",
    feedback_path: str = _DEFAULT_FEEDBACK_PATH,
) -> WaitFeedback:
    """Record an explicit user thumbs up/down for a wait-trace.

    This is the POST /api/wait-feedback handler's persistence step.
    """
    fb = WaitFeedback(
        wait_trace_id=wait_trace_id,
        signal_type="user_thumbs",
        signal_value=(user_signal == "correct"),
        confidence=1.0,
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        notes=notes,
    )
    existing = _load_feedback(feedback_path)
    existing.append(asdict(fb))
    _save_feedback(existing, feedback_path)
    logger.info(
        "wait_user_thumbs_recorded",
        trace_id=wait_trace_id,
        signal=user_signal,
    )
    return fb


def load_wait_feedback(
    feedback_path: str = _DEFAULT_FEEDBACK_PATH,
) -> list[dict]:
    """Return all persisted feedback records."""
    return _load_feedback(feedback_path)


def wait_correctness_summary(
    since: datetime,
    feedback_path: str = _DEFAULT_FEEDBACK_PATH,
) -> dict:
    """Compute weekly correctness summary for the reflector rollup.

    Returns counts and pattern breakdown for wait-feedback signals
    recorded since `since`.
    """
    records = _load_feedback(feedback_path)
    since_iso = since.isoformat()

    relevant = [r for r in records if r.get("evaluated_at", "") >= since_iso]

    thumbs_up = sum(
        1 for r in relevant
        if r.get("signal_type") == "user_thumbs" and r.get("signal_value") is True
    )
    thumbs_down = sum(
        1 for r in relevant
        if r.get("signal_type") == "user_thumbs" and r.get("signal_value") is False
    )
    auto_relevant = sum(
        1 for r in relevant
        if r.get("signal_type") == "was_still_relevant" and r.get("signal_value") is True
    )
    total_waits = len({
        r.get("wait_trace_id") for r in relevant
        if r.get("signal_type") in ("was_still_relevant", "user_thumbs")
    })

    return {
        "total_waits": total_waits,
        "thumbs_up": thumbs_up,
        "thumbs_down": thumbs_down,
        "auto_still_relevant": auto_relevant,
        "records": len(relevant),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_wait_args(trace) -> dict:
    """Pull the wait tool's args from a trace's tools_called list."""
    for call in trace.tools_called or []:
        if isinstance(call, dict) and call.get("name") == "wait":
            return call.get("args") or {}
    return {}


def _parse_until(until_str: Optional[str], fallback: datetime) -> datetime:
    """Parse the `until` field to a datetime, falling back to `fallback`."""
    if not until_str:
        return fallback
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(until_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    # Natural language or unparseable — treat as fallback
    return fallback


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Compute cosine distance between two equal-length vectors."""
    if len(a) != len(b):
        return 2.0  # maximum distance
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0.0 or mag_b == 0.0:
        return 2.0
    similarity = dot / (mag_a * mag_b)
    return 1.0 - similarity
