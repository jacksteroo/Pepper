"""Bootstrap strategies — seed the table on first run.

Reads ``data/life_context.md`` (if present) and extracts a small set
of Pepper-specific strategies.  When the file is absent, uses a
hardcoded list of sensible defaults.  All rows are marked
``created_by='bootstrap'`` and ``status='active'``.

The bootstrap is idempotent: if any active bootstrap strategy already
exists in the DB, the function is a no-op.  This prevents re-seeding
on every restart while still seeding on a fresh DB.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

import structlog

from agent.strategies.models import StrategyRow

logger = structlog.get_logger(__name__)

# Fallback strategies used when life_context.md is absent.
_DEFAULT_STRATEGIES: list[str] = [
    (
        "When Jack asks about schedule conflicts, check calendar data first "
        "before asking clarifying questions."
    ),
    (
        "For email triage, prioritize by urgency plus sender relationship, "
        "not just recency."
    ),
    (
        "When multiple options are possible, present the recommended option "
        "first with brief reasoning, then list alternatives."
    ),
    (
        "Don't repeat context Jack already provided in the same conversation."
    ),
    (
        "For travel logistics, always check open loops in life context "
        "before answering."
    ),
    (
        "Keep responses concise: lead with the direct answer, then add "
        "supporting detail only if it changes the decision."
    ),
    (
        "When a task has a clear owner (Jack or someone else), name them "
        "explicitly rather than leaving it ambiguous."
    ),
    (
        "For health and fitness queries, cite the specific data source "
        "(Apple Health, Oura, etc.) rather than speaking in generalities."
    ),
    (
        "Surface time-sensitive items (deadlines, expiring offers, "
        "overdue replies) proactively without being asked."
    ),
    (
        "When Jack corrects a factual error, update the relevant context "
        "via update_life_context rather than only acknowledging in-turn."
    ),
]


def _extract_from_life_context(life_context_path: str) -> list[str]:
    """Extract strategy hints from life_context.md heuristics section.

    Scans for a 'Heuristics' or 'Preferences' section and returns any
    bullet points found there, up to 10 items.  Falls back to defaults
    if the section is missing or the file is unreadable.
    """
    path = Path(life_context_path)
    if not path.exists():
        return []

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("bootstrap_life_context_read_failed", error=str(exc))
        return []

    # Look for a Heuristics or Preferences section.
    found_section = False
    bullets: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        # Detect section headers containing relevant keywords.
        if stripped.startswith("#") and any(
            kw in stripped.lower()
            for kw in ("heuristic", "preference", "behavior", "style")
        ):
            found_section = True
            continue
        # Stop at the next section header.
        if found_section and stripped.startswith("#"):
            break
        if found_section and stripped.startswith(("-", "*", "•")):
            bullet_text = stripped.lstrip("-*• ").strip()
            if len(bullet_text) > 10:
                bullets.append(bullet_text)
            if len(bullets) >= 10:
                break

    return bullets


def build_bootstrap_rows(
    life_context_path: Optional[str] = None,
) -> list[StrategyRow]:
    """Return a list of StrategyRows to seed from bootstrap.

    No DB interaction — just builds the objects so the caller can
    insert them inside its own session.
    """
    texts: list[str] = []

    if life_context_path:
        texts = _extract_from_life_context(life_context_path)

    if not texts:
        texts = _DEFAULT_STRATEGIES

    rows: list[StrategyRow] = []
    for text in texts:
        row = StrategyRow(
            strategy_id=uuid.uuid4(),
            text=text,
            version=1,
            parent_strategy_id=None,
            created_by="bootstrap",
            confidence=0.5,
            status="active",
        )
        rows.append(row)

    return rows


async def maybe_bootstrap(
    repository: object,
    session: object,
    life_context_path: Optional[str] = None,
) -> int:
    """Seed strategies if the table is empty.

    Returns the number of rows inserted (0 if already seeded).

    Idempotent: only inserts when ``query_all_active`` returns an empty
    list, so repeated calls on a populated DB are no-ops.
    """
    from agent.strategies.repository import StrategyRepository

    repo: StrategyRepository = repository  # type: ignore[assignment]
    existing = await repo.query_all_active()
    if existing:
        logger.debug("bootstrap_skipped", existing_count=len(existing))
        return 0

    rows = build_bootstrap_rows(life_context_path)
    for row in rows:
        await repo.append(row)

    logger.info("bootstrap_complete", inserted=len(rows))
    return len(rows)
