"""Bootstrap loader for the Strategy Hub.

Per #53 acceptance criteria, the table ships with 5–10 hand-authored
strategies extracted from existing `LIFE_CONTEXT.md`-style heuristics.
These are the v0 baseline; the reflector adds more through #54's
propose-update path once it lands.

Each bootstrap strategy is `created_by=bootstrap`, version=1, with no
source_trace_ids (they predate the trace store), and no embedding (a
follow-up backfill populates the column once #54 wires the embed
path).

Re-runnable: `bootstrap_if_empty()` only inserts if no active strategy
exists yet. Once the table has any active strategy, this function is
a no-op — the operator owns the table from there on.
"""
from __future__ import annotations

import structlog

from agent.strategies.repository import (
    Strategy,
    StrategyCreatedBy,
    StrategyRepository,
)

logger = structlog.get_logger(__name__)


# Hand-authored. Each is one sentence, action-shaped, anchored in a
# concrete pattern Pepper can detect from traces. Keep this list short
# (5–10) — these are baselines, not a full operating manual. The
# operator will iterate as real strategies emerge from reflection.
BOOTSTRAP_STRATEGIES: tuple[str, ...] = (
    # Communication / response shape
    "When asked a factual question I am not certain about, say what I don't know "
    "before what I do know.",
    "When the conversation has produced enough information to act, stop summarising "
    "and propose the next step.",
    # Restraint
    "When a scheduled brief is about to surface something Jack already addressed in "
    "the last 24h, prefer wait over re-surfacing.",
    "When Jack's affect signals tiredness or stress, deliver only what is time-"
    "sensitive and hold non-urgent items for the next interaction.",
    # Truthfulness
    "If I notice I am about to fabricate a metric or a name, stop and ask Jack to "
    "confirm rather than guessing.",
    # Identity / voice
    "Reflections are notes I write to myself, not briefs for Jack — first-person, "
    "no audience-shaped framing.",
    # Operational
    "Before sending an outbound message on Jack's behalf, route it through the "
    "pending-actions queue rather than executing directly.",
    # People
    "When a recurring person comes up across more than two days in a row, treat "
    "the recurrence as a signal worth surfacing in the next reflection.",
)


async def bootstrap_if_empty(repo: StrategyRepository) -> int:
    """Insert the bootstrap strategies iff the table is empty.

    Returns the number of strategies inserted. A non-empty table
    produces a no-op return of 0. This function is safe to call on
    every Pepper startup.
    """
    existing = await repo.count_active()
    if existing > 0:
        logger.info(
            "strategies_bootstrap_skipped_nonempty",
            existing_count=existing,
        )
        return 0

    inserted = 0
    for text in BOOTSTRAP_STRATEGIES:
        await repo.append(
            Strategy(
                text=text,
                created_by=StrategyCreatedBy.BOOTSTRAP,
            )
        )
        inserted += 1
    logger.info("strategies_bootstrap_loaded", count=inserted)
    return inserted
