"""Phase 6.7 — Commitment follow-through.

`CommitmentExtractor` captures commitments into recall memory
(`COMMITMENT: I'll reply to Sarah tonight` etc.). Before Phase 6.7 those
entries sat in memory and were surfaced only when the user happened to ask.

This module re-surfaces them at the time they're due. The scheduler fires
three times per day (morning / afternoon / evening); each slot matches a
subset of commitment time cues.

Heuristic mapping (commitment text → which daily slot surfaces it):

  Morning (~8am):   "today", "this morning", "by EOD" (first nudge), unscoped
  Afternoon (~5pm): "by EOD", "end of day", "this afternoon", "before EOD"
  Evening (~10pm):  "tonight", "by tonight", "tomorrow" (prep nudge)

Already-surfaced items are not re-nagged within the same day — tracked in an
in-memory set keyed on the commitment's memory id + surfacing date. This state
does not persist across restarts (see ROADMAP.md deferred items for durable
commitment tracking).
"""
from __future__ import annotations

import re
import structlog
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

logger = structlog.get_logger()


_TODAY_CUES = ("today", "this morning", "this afternoon")
_EOD_CUES = ("by eod", "end of day", "before eod", "eod")
_TONIGHT_CUES = ("tonight", "this evening", "by tonight")
_TOMORROW_CUES = ("tomorrow", "first thing tomorrow")


@dataclass
class DueCommitment:
    memory_id: int
    text: str
    slot: str  # "morning" | "afternoon" | "evening"
    cue: str   # which phrase matched, for debugging
    created_at: str


class CommitmentFollowup:
    """Re-surface unresolved commitments at the time they're due."""

    def __init__(self, pepper_core) -> None:
        self.pepper = pepper_core
        # Tracks (memory_id, date) pairs already surfaced so we don't spam.
        self._surfaced: set[tuple[int, str]] = set()

    def _current_slot(self) -> str:
        tz = ZoneInfo(self.pepper.config.TIMEZONE)
        hour = datetime.now(tz).hour
        if hour < 12:
            return "morning"
        if hour < 20:
            return "afternoon"
        return "evening"

    def _classify_text(self, text: str, slot: str) -> str | None:
        """Return the matching cue phrase if `text` is due in `slot`, else None.

        `text` is the raw commitment string (already stripped of "COMMITMENT:").
        Unscoped commitments (no time cue) surface only in the morning slot so
        a vague promise gets exactly one nudge per day.
        """
        lower = text.lower()

        def match(cues: tuple[str, ...]) -> str | None:
            for c in cues:
                if c in lower:
                    return c
            return None

        if slot == "morning":
            hit = match(_TODAY_CUES) or match(_EOD_CUES)
            if hit:
                return hit
            # Unscoped commitment: first-nudge-in-morning policy.
            has_any_cue = (
                match(_TODAY_CUES) or match(_EOD_CUES)
                or match(_TONIGHT_CUES) or match(_TOMORROW_CUES)
            )
            if not has_any_cue:
                return "unscoped"
            return None
        if slot == "afternoon":
            return match(_EOD_CUES) or match(_TODAY_CUES)
        if slot == "evening":
            return match(_TONIGHT_CUES) or match(_TOMORROW_CUES)
        return None

    async def find_due_commitments(self, limit: int = 40) -> list[DueCommitment]:
        """Return the commitments that should be re-surfaced in the current slot.

        Semantics: pull all COMMITMENT entries from recall memory, drop
        [RESOLVED] ones, classify against the current time slot, skip items
        already surfaced today, return the remainder newest-first.
        """
        slot = self._current_slot()
        try:
            # Open commitments span all time; #29's default 30-day recency
            # tilt would bury old-but-still-open promises. Disable it here.
            raw = await self.pepper.memory.search_recall(
                "COMMITMENT", limit=limit, time_window_days=None
            )
        except Exception as e:
            logger.warning("commitment_followup_search_failed", error=str(e))
            return []

        today_key = date.today().isoformat()
        due: list[DueCommitment] = []

        for item in raw:
            content = (item.get("content") or "").strip()
            if not content.upper().startswith("COMMITMENT:"):
                continue
            if "[RESOLVED]" in content.upper():
                continue

            body = re.sub(r"^COMMITMENT:\s*", "", content, flags=re.IGNORECASE).strip()
            cue = self._classify_text(body, slot)
            if not cue:
                continue

            mem_id = item.get("id")
            if mem_id is None:
                continue
            key = (int(mem_id), today_key)
            if key in self._surfaced:
                continue
            self._surfaced.add(key)

            due.append(DueCommitment(
                memory_id=int(mem_id),
                text=body,
                slot=slot,
                cue=cue,
                created_at=item.get("created_at", ""),
            ))

        logger.info(
            "commitment_followup_due",
            slot=slot,
            due_count=len(due),
        )
        return due

    @staticmethod
    def format_followup_message(items: list[DueCommitment]) -> str:
        """Format the list into a short user-facing nudge."""
        if not items:
            return ""
        if len(items) == 1:
            return f"Follow-up reminder: {items[0].text}"
        lines = [f"Follow-up reminders ({len(items)} open):"]
        for it in items[:8]:
            snippet = it.text if len(it.text) <= 140 else it.text[:137] + "…"
            lines.append(f"• {snippet}")
        if len(items) > 8:
            lines.append(f"…and {len(items) - 8} more")
        return "\n".join(lines)
