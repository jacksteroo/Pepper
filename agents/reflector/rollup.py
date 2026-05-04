"""Weekly + monthly rollups (#40).

Hierarchical reflection: weekly summarises the seven daily reflections
in a local-week window; monthly summarises the weekly reflections in
a local-month window. Same voice rules and persistence shape as the
daily reflector (#39); the schema added in #39 already carries the
`tier`, `parent_reflection_ids`, and `metadata_` columns these need.

Triggers come from APScheduler in core via the same NOTIFY mechanism
as the daily, but on dedicated channels to keep cadence dispatch
explicit and to dodge the Sunday 23:55 race the daily already
occupies:

  - `reflector_weekly_trigger`  — Monday 00:15 local (covers prev week)
  - `reflector_monthly_trigger` — 1st of month 00:15 local (covers prev month)

The payload is a `YYYY-MM-DD` date in local TZ. For weekly, the
scheduler sends YESTERDAY's date (the Sunday at the end of the week
the rollup covers); for monthly, the scheduler sends the 1st of the
new month and the rollup interprets that as "previous calendar
month."

Voice rules carry over from #39: the rollup persists the violation
labels into `metadata_.voice_violations` so #42's eval rubric can
score the rollup tier without re-running the regex.

PRIVACY: `chat_fn` is injected to keep this module testable; in
production it MUST come from `agents.reflector.main._make_rollup_adapters`,
which closes over a boot-validated Ollama URL. Constructing a
`chat_fn` from any other source would defeat the local-only
guarantee on RAW_PERSONAL reflection content.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

import structlog

from agents.reflector import store as rstore
from agents.reflector.prompt import (
    PROMPT_VERSION_MONTHLY,
    PROMPT_VERSION_WEEKLY,
    SYSTEM_PROMPT_MONTHLY,
    SYSTEM_PROMPT_WEEKLY,
    ReflectionDigest,
    render_rollup_prompt,
    voice_violations,
)

logger = structlog.get_logger(__name__)

# Hard cap on rollup text length, mirrors MAX_REFLECTION_TEXT_CHARS
# in main.py. Defined here to avoid importing main into rollup (which
# would create a circular import once main imports rollup).
MAX_ROLLUP_TEXT_CHARS: int = 24_000

# Bound the number of children we feed the LLM. Weekly is bounded by
# 7; monthly by ~5. We cap at the next-tier worst case so a backfill
# run does not silently overflow the prompt.
MAX_CHILDREN_PER_ROLLUP: int = 32


# ── Window resolution ────────────────────────────────────────────────────────


def weekly_window_for_payload(
    payload: str, *, tz: ZoneInfo, now: datetime
) -> tuple[datetime, datetime]:
    """Resolve the [Mon 00:00, next Mon 00:00) calendar-week window in UTC.

    Trigger fires Monday 00:15 local with `payload =` yesterday's
    date (the Sunday at the end of the week we roll up). For any
    `payload` date, we anchor on the Monday of that date's
    calendar week — i.e. the most recent Monday on or before
    `parsed` — and walk seven days forward.

    This is the calendar-week answer regardless of which weekday
    `parsed` is, so a backfill or manual replay with an arbitrary
    date still produces a Mon..Sun window aligned to that date's
    own ISO week.
    """
    parsed = _parse_payload_date(payload, tz=tz, now=now)
    monday = parsed - timedelta(days=parsed.weekday())
    local_start = datetime.combine(monday, time.min, tzinfo=tz)
    local_end = local_start + timedelta(days=7)
    return _to_utc_clipped(local_start, local_end, now)


def monthly_window_for_payload(
    payload: str, *, tz: ZoneInfo, now: datetime
) -> tuple[datetime, datetime]:
    """Resolve the [first-of-prev-month, first-of-this-month) window in UTC.

    Trigger fires on the 1st of month N+1 with payload = that day's
    `YYYY-MM-DD`. The month we roll up is month N — i.e. everything
    strictly before the trigger date. Window is `[first-of-N 00:00
    local, first-of-(N+1) 00:00 local)` converted to UTC and clipped
    to `now`.
    """
    parsed = _parse_payload_date(payload, tz=tz, now=now)
    if parsed.day == 1:
        first_of_this = parsed
    else:
        # Defensive fallback: align to the first of `parsed`'s month.
        first_of_this = parsed.replace(day=1)
    if first_of_this.month == 1:
        first_of_prev = first_of_this.replace(year=first_of_this.year - 1, month=12)
    else:
        first_of_prev = first_of_this.replace(month=first_of_this.month - 1)
    local_start = datetime.combine(first_of_prev, time.min, tzinfo=tz)
    local_end = datetime.combine(first_of_this, time.min, tzinfo=tz)
    return _to_utc_clipped(local_start, local_end, now)


def _parse_payload_date(payload: str, *, tz: ZoneInfo, now: datetime) -> date:
    """Parse a `YYYY-MM-DD` date; fall back to yesterday in `tz`."""
    try:
        return date.fromisoformat(payload.strip())
    except (ValueError, AttributeError):
        logger.warning("rollup_payload_unparseable", payload=payload[:64])
        local_now = now.astimezone(tz)
        return (local_now - timedelta(days=1)).date()


def _to_utc_clipped(
    local_start: datetime, local_end: datetime, now: datetime
) -> tuple[datetime, datetime]:
    window_start = local_start.astimezone(timezone.utc)
    window_end = min(local_end.astimezone(timezone.utc), now)
    return window_start, window_end


# ── Child digest projection ──────────────────────────────────────────────────


def _digest_children(
    children: Sequence[rstore.Reflection], *, tz: ZoneInfo
) -> list[ReflectionDigest]:
    """Project child reflections to (date, text) for the rollup prompt."""
    digests: list[ReflectionDigest] = []
    for child in children:
        local_start = child.window_start.astimezone(tz)
        digests.append(
            ReflectionDigest(
                date=local_start.date().isoformat(),
                text=child.text,
            )
        )
    return digests


# ── Rollup runners ───────────────────────────────────────────────────────────


async def _run_rollup(
    *,
    tier: str,
    tier_label: str,
    system_prompt: str,
    prompt_version: str,
    parent_tier: str,
    window_start: datetime,
    window_end: datetime,
    session_factory,
    chat_fn,
    embed_fn,
    chat_model: str,
    chat_timeout_s: float,
    embed_timeout_s: float,
    text_cap: int,
    tz: ZoneInfo,
) -> Optional[rstore.Reflection]:
    """Shared rollup pipeline. Returns the persisted Reflection or None.

    `chat_fn` and `embed_fn` are pluggable so tests can stub them
    without monkey-patching httpx. They share the shape of the
    helpers used in `agents.reflector.main`.
    """
    async with session_factory() as session:
        repo = rstore.ReflectionRepository(session)
        # Children are reflections of the parent tier whose
        # `window_start` falls inside [window_start, window_end). We
        # filter on `window_start` (not `created_at`) so a daily that
        # was backfilled or written late still counts toward its
        # actual week / month.
        children = await repo.query_by_window(
            tier=parent_tier,
            window_start_gte=window_start,
            window_start_lt=window_end,
            limit=MAX_CHILDREN_PER_ROLLUP,
        )
        children = list(children)
        previous = await repo.latest(tier=tier)

    digests = _digest_children(children, tz=tz)

    # Issue #56: include wait-correctness summary in weekly rollup prompts.
    _wait_correctness: dict | None = None
    if tier == rstore.TIER_WEEKLY:
        try:
            from agents.reflector.wait_evaluator import wait_correctness_summary
            _wait_correctness = wait_correctness_summary(since=window_start)
        except Exception:
            pass  # best-effort; missing wait data must not break rollup

    user_prompt = render_rollup_prompt(
        tier_label=tier_label,
        window_start=window_start,
        window_end=window_end,
        children=digests,
        previous_rollup_text=previous.text if previous is not None else None,
        wait_correctness=_wait_correctness,
    )

    logger.info(
        "rollup_pass_starting",
        tier=tier,
        n_children=len(digests),
        previous_id=previous.reflection_id if previous else None,
    )

    text, model_used = await chat_fn(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=chat_model,
        timeout_s=chat_timeout_s,
    )
    if not text:
        logger.warning("rollup_empty_response", tier=tier, n_children=len(digests))
        return None

    original_len = len(text)
    if original_len > text_cap:
        logger.warning(
            "rollup_text_truncated",
            tier=tier,
            original_len=original_len,
            cap=text_cap,
        )
        text = text[: text_cap - 3].rstrip() + "..."

    violations = voice_violations(text)
    if violations:
        logger.warning(
            "rollup_voice_violations", tier=tier, phrases=violations
        )

    embedding = await embed_fn(
        text=text,
        model=rstore.REFLECTION_EMBEDDING_MODEL_DEFAULT,
        timeout_s=embed_timeout_s,
    )

    metadata = {
        "voice_violations": violations,
        "original_text_len": original_len,
        "child_count": len(digests),
        "child_tier": parent_tier,
    }

    reflection = rstore.Reflection(
        text=text,
        window_start=window_start,
        window_end=window_end,
        tier=tier,
        previous_reflection_id=previous.reflection_id if previous else None,
        parent_reflection_ids=[c.reflection_id for c in children] or None,
        trace_count=sum(c.trace_count for c in children),
        model_used=model_used,
        prompt_version=prompt_version,
        embedding=embedding,
        embedding_model_version=(
            rstore.REFLECTION_EMBEDDING_MODEL_DEFAULT if embedding is not None else None
        ),
        metadata_=metadata,
    )

    async with session_factory() as session:
        repo = rstore.ReflectionRepository(session)
        try:
            await repo.append(reflection)
        except rstore.DuplicateReflectionError as exc:
            logger.warning(
                "rollup_duplicate_skipped",
                tier=exc.tier,
                window_start=exc.window_start.isoformat(),
            )
            return None

    return reflection


async def run_weekly_rollup(
    *,
    payload: str,
    session_factory,
    chat_fn,
    embed_fn,
    chat_model: str,
    chat_timeout_s: float,
    embed_timeout_s: float,
    text_cap: int = MAX_ROLLUP_TEXT_CHARS,
    tz: ZoneInfo,
    now: Optional[datetime] = None,
) -> Optional[rstore.Reflection]:
    now = now or datetime.now(timezone.utc)
    window_start, window_end = weekly_window_for_payload(payload, tz=tz, now=now)
    return await _run_rollup(
        tier=rstore.TIER_WEEKLY,
        tier_label="week",
        system_prompt=SYSTEM_PROMPT_WEEKLY,
        prompt_version=PROMPT_VERSION_WEEKLY,
        parent_tier=rstore.TIER_DAILY,
        window_start=window_start,
        window_end=window_end,
        session_factory=session_factory,
        chat_fn=chat_fn,
        embed_fn=embed_fn,
        chat_model=chat_model,
        chat_timeout_s=chat_timeout_s,
        embed_timeout_s=embed_timeout_s,
        text_cap=text_cap,
        tz=tz,
    )


async def run_monthly_rollup(
    *,
    payload: str,
    session_factory,
    chat_fn,
    embed_fn,
    chat_model: str,
    chat_timeout_s: float,
    embed_timeout_s: float,
    text_cap: int = MAX_ROLLUP_TEXT_CHARS,
    tz: ZoneInfo,
    now: Optional[datetime] = None,
) -> Optional[rstore.Reflection]:
    now = now or datetime.now(timezone.utc)
    window_start, window_end = monthly_window_for_payload(payload, tz=tz, now=now)
    return await _run_rollup(
        tier=rstore.TIER_MONTHLY,
        tier_label="month",
        system_prompt=SYSTEM_PROMPT_MONTHLY,
        prompt_version=PROMPT_VERSION_MONTHLY,
        parent_tier=rstore.TIER_WEEKLY,
        window_start=window_start,
        window_end=window_end,
        session_factory=session_factory,
        chat_fn=chat_fn,
        embed_fn=embed_fn,
        chat_model=chat_model,
        chat_timeout_s=chat_timeout_s,
        embed_timeout_s=embed_timeout_s,
        text_cap=text_cap,
        tz=tz,
    )
