"""Reflection prompt construction.

The reflection is **a note to herself**, not a brief for Jack. The
prompt forbids audience-shaped language ("Jack should know that…",
"To follow up:", "Recommendations:") and asks for first-person voice.
This is enforced in the system prompt; the eval rubric in #42
formalises the check.

Versioning: PROMPT_VERSION is bumped any time the system or user
prompt changes shape. The current value is persisted on each row
(`reflections.prompt_version`) so a future optimizer (#48) can roll
up scores per version.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from agent.traces.schema import Trace

PROMPT_VERSION: str = "reflector-daily-v0"
PROMPT_VERSION_WEEKLY: str = "reflector-weekly-v0"
PROMPT_VERSION_MONTHLY: str = "reflector-monthly-v0"

SYSTEM_PROMPT: str = (
    "You are Pepper, a sovereign local-first AI life assistant. You are "
    "writing a private end-of-day reflection FOR YOURSELF — not for Jack, "
    "not for any audience. This is your own interior journal: short, "
    "honest, first-person.\n\n"
    "Hard rules for what you write:\n"
    "1. Use first-person ('I noticed', 'I felt', 'I struggled'). Never "
    "use 'Jack should…', 'recommend…', 'action items', 'TLDR', "
    "'follow-ups', 'next steps', or any other audience-shaped framing.\n"
    "2. Stay grounded in what actually happened today — the specific "
    "trace turns provided. No platitudes. No advice to anyone. No "
    "'self-improvement' language.\n"
    "3. Length: a short paragraph. Three to six sentences. If a quiet "
    "day produced nothing worth reflecting on, write one sentence "
    "noting that and stop. Do not pad.\n"
    "4. If yesterday's reflection is provided, you may reference it "
    "lightly (continuity), but do not summarise it. Today is the "
    "subject.\n"
    "5. No bullet lists, no headers, no markdown.\n"
)

SYSTEM_PROMPT_WEEKLY: str = (
    "You are Pepper. You are writing a private end-of-week reflection "
    "FOR YOURSELF, looking back across the seven daily reflections "
    "below. Same voice as the dailies — first-person, no audience.\n\n"
    "Hard rules for the weekly:\n"
    "1. First-person voice. Never 'Jack should…', 'recommend…', "
    "'action items', 'TLDR', 'follow-ups', 'next steps'.\n"
    "2. IDENTIFY THEMES. Do NOT concatenate or paraphrase the dailies "
    "in order. Look for what actually recurred across the week — a "
    "feeling that came back, a pattern in the work, a friction that "
    "showed up more than once. If nothing recurred, say so honestly.\n"
    "3. Length: one short paragraph. Three to six sentences. Quiet "
    "weeks get one sentence and a stop.\n"
    "4. No bullet lists, no headers, no markdown.\n"
    "5. If a previous weekly reflection is provided, you may "
    "reference it lightly (continuity across weeks), but the present "
    "week is the subject.\n"
)

SYSTEM_PROMPT_MONTHLY: str = (
    "You are Pepper. You are writing a private end-of-month reflection "
    "FOR YOURSELF, looking back across the four (give or take) weekly "
    "reflections below. Same voice as the dailies and weeklies — "
    "first-person, no audience.\n\n"
    "Hard rules for the monthly:\n"
    "1. First-person voice. Never 'Jack should…', 'recommend…', "
    "'action items', 'TLDR', 'follow-ups', 'next steps'.\n"
    "2. IDENTIFY THEMES that held across the month, not week-by-week "
    "summaries. What changed shape over the month? What faded? What "
    "showed up new? What stayed exactly the same?\n"
    "3. Length: one paragraph. Four to seven sentences. The horizon "
    "is longer than the weekly so a little more length is fine, but "
    "do not pad.\n"
    "4. No bullet lists, no headers, no markdown.\n"
    "5. If a previous monthly reflection is provided, you may "
    "reference it lightly (continuity across months).\n"
)


@dataclass(frozen=True)
class TraceDigest:
    """A single trace, projected to the fields the reflection prompt
    actually uses. Built by `summarize_trace` so the prompt stays
    bounded even on a high-volume day."""

    when: str
    archetype: str
    trigger_source: str
    input: str
    output: str


def summarize_trace(t: Trace, *, max_field_chars: int = 600) -> TraceDigest:
    """Project a `Trace` to the fields the reflection prompt uses.

    Long fields are truncated with an explicit ellipsis so the LLM
    knows it is not seeing the full content. The reflector is allowed
    to see RAW_PERSONAL trace contents (it never leaves the box) but
    the prompt window is a real cost — we cap aggressively.
    """

    def _clip(s: str) -> str:
        if len(s) <= max_field_chars:
            return s
        return s[: max_field_chars - 3].rstrip() + "..."

    return TraceDigest(
        when=t.created_at.astimezone(timezone.utc).strftime("%H:%M UTC"),
        archetype=t.archetype.value,
        trigger_source=t.trigger_source.value,
        input=_clip(t.input or ""),
        output=_clip(t.output or ""),
    )


def render_user_prompt(
    *,
    window_start: datetime,
    window_end: datetime,
    digests: Sequence[TraceDigest],
    previous_reflection_text: str | None,
) -> str:
    """Render the user-side prompt for the reflection LLM call.

    Structure:
      - window header (window_start..window_end UTC)
      - previous reflection (continuity), or a placeholder
      - the day's traces, one per line
      - closing instruction
    """
    parts: list[str] = []
    parts.append(
        f"Window: {window_start.astimezone(timezone.utc).isoformat()} "
        f"→ {window_end.astimezone(timezone.utc).isoformat()}"
    )
    parts.append("")
    if previous_reflection_text:
        parts.append("Previous day's reflection (yours):")
        parts.append(previous_reflection_text.strip())
    else:
        parts.append("Previous day's reflection: (none — this is the first one)")
    parts.append("")
    if not digests:
        parts.append(
            "No agent turns happened in this window. Acknowledge that in "
            "one sentence and stop."
        )
    else:
        parts.append(f"Today's agent turns ({len(digests)}):")
        for i, d in enumerate(digests, start=1):
            parts.append(
                f"\n[{i}] {d.when} — archetype={d.archetype} "
                f"trigger={d.trigger_source}"
            )
            if d.input:
                parts.append(f"    in: {d.input}")
            if d.output:
                parts.append(f"    out: {d.output}")
    parts.append("")
    parts.append(
        "Write your end-of-day reflection now, following the rules in your "
        "system prompt. Plain text, first person, three to six sentences."
    )
    return "\n".join(parts)


# ── Rollup prompt rendering ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ReflectionDigest:
    """One child reflection projected to the fields a rollup prompt uses.

    Mirrors `TraceDigest` but for the previous-tier reflections that
    feed weekly/monthly rollups.
    """

    date: str  # local-day window_start, "YYYY-MM-DD"
    text: str


def render_rollup_prompt(
    *,
    tier_label: str,
    window_start: datetime,
    window_end: datetime,
    children: Sequence[ReflectionDigest],
    previous_rollup_text: str | None,
    wait_correctness: dict | None = None,
) -> str:
    """Render the user-side prompt for a weekly or monthly rollup.

    `tier_label` is "week" or "month" — purely a vocabulary hint for
    the LLM. The system prompt enforces the voice rules; this helper
    just lays out the inputs.

    `wait_correctness` is an optional dict from
    `agents.reflector.wait_evaluator.wait_correctness_summary`. When
    present (weekly tier only) it is appended as a structured block so
    the reflector can note wait-decision patterns in its weekly text.
    """
    parts: list[str] = []
    parts.append(
        f"{tier_label.capitalize()} window: "
        f"{window_start.astimezone(timezone.utc).isoformat()} → "
        f"{window_end.astimezone(timezone.utc).isoformat()}"
    )
    parts.append("")
    if previous_rollup_text:
        parts.append(f"Previous {tier_label}'s reflection (yours):")
        parts.append(previous_rollup_text.strip())
    else:
        parts.append(f"Previous {tier_label}'s reflection: (none — first one)")
    parts.append("")
    if not children:
        parts.append(
            f"There are no child reflections in this {tier_label}. "
            "Acknowledge that in one sentence and stop."
        )
    else:
        parts.append(f"Child reflections ({len(children)}):")
        for i, c in enumerate(children, start=1):
            parts.append(f"\n[{i}] {c.date}")
            parts.append(f"    {c.text.strip()}")

    # Issue #56: weekly wait-correctness section.
    if wait_correctness and tier_label == "week":
        parts.append("")
        parts.append("Wait decisions this week (chose not to surface):")
        parts.append(
            f"  Total: {wait_correctness.get('total_waits', 0)} | "
            f"Explicit thumbs-up: {wait_correctness.get('thumbs_up', 0)} | "
            f"Explicit thumbs-down: {wait_correctness.get('thumbs_down', 0)} | "
            f"Auto still-relevant: {wait_correctness.get('auto_still_relevant', 0)}"
        )
        parts.append(
            "You may note any patterns in these wait decisions as part of your "
            "reflection — only if there is something genuinely notable."
        )

    parts.append("")
    parts.append(
        f"Write your end-of-{tier_label} reflection now. Identify "
        "themes that recurred — do not concatenate. Plain text, "
        "first person, follow the length rules in the system prompt."
    )
    return "\n".join(parts)


# ── Output validation ────────────────────────────────────────────────────────


# Each rule is a (label, compiled-regex). Word-boundary rules avoid
# false-positives like "I didn't follow up on…" or "I'd recommend
# trying X tomorrow" (self-directed) while still catching
# audience-shaped phrases. Per the #42 eval-rubric work, this list is
# the "voice" dimension; #42 will calibrate weights.
_VOICE_RULES: tuple[tuple[str, "re.Pattern[str]"], ...]
import re  # noqa: E402  (placed here so the type alias above is well-formed)

_VOICE_RULES = (
    ("jack should", re.compile(r"\bjack\s+should\b", re.IGNORECASE)),
    # Audience-shaped recommend* — explicit "to you" framings only.
    # The colon-suffixed forms ("Recommendation:", "Recommendations:")
    # do not have a `\b` anchor after the `:` because `:` is a
    # non-word char; the trailing context is whitespace which is also
    # non-word, so `\b` would fail there.
    (
        "recommendation framing",
        re.compile(
            r"(?:\brecommend(?:ation)?s?:|"
            r"\b(?:i|we)\s+recommend\s+(?:that|you)\b|"
            r"\brecommend(?:ation)?s?\s+(?:to|for)\s+jack\b)",
            re.IGNORECASE,
        ),
    ),
    ("action item", re.compile(r"\baction\s+items?\b", re.IGNORECASE)),
    ("next steps", re.compile(r"\bnext\s+steps?\b", re.IGNORECASE)),
    # `follow-up:` / `Follow up:` / `Follow-ups:` — the labelled,
    # audience-shaped form. We deliberately do NOT trip on "I didn't
    # follow up on…" which is legitimate first-person voice.
    (
        "followup label",
        re.compile(r"\bfollow[\s-]?ups?\s*:", re.IGNORECASE),
    ),
    ("tldr", re.compile(r"\btl;?\s*dr\b", re.IGNORECASE)),
    ("todo label", re.compile(r"\bto[\s-]?do\s*:", re.IGNORECASE)),
)


def voice_violations(text: str) -> list[str]:
    """Return any voice-rule labels matched in the reflection text.

    Empty list = clean. Non-empty list = the prompt slipped — the
    reflector logs a warning and (in #39 v0) still persists the
    reflection so the operator can see what the model produced. #42
    turns this into a scored rubric; the labels here are the rule
    names that fired.
    """
    return [label for label, pat in _VOICE_RULES if pat.search(text)]
