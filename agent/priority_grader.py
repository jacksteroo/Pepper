"""Phase 6.7 — Priority grader v1 (non-learning).

Tags items surfaced in inbox summaries and cross-source triage with one of:

  urgent    — time-critical or from a VIP; user likely wants to act now
  important — from a real relationship or substantive work, not time-critical
  defer     — worth knowing about but not worth acting on right now
  ignore    — clearly noise (newsletters, receipts, marketing)

Signals used (all already collected by Pepper):
  - Explicit urgency keywords in the body/subject ("urgent", "asap", "today")
  - Calendar proximity — meeting referenced within N hours bumps to urgent
  - VIP list from life-context ("important people" section)
  - Communication health — "quiet" contacts (not heard from in a while) get
    an important bump when they reach out
  - Sender patterns — noreply@, newsletter/marketing domains → ignore

Deliberately NOT used in v1:
  - Adaptive learning from the user's response latency per contact.
    That's a follow-up once this static grader is trusted (see ROADMAP.md).

API:
  grader = PriorityGrader(vips=["sarah", "mike"], quiet_contacts=["dave"])
  grader.grade(item) → "urgent" | "important" | "defer" | "ignore"

`item` is a dict with any of the following keys (all optional):
  from, sender, subject, preview, body, text, timestamp, channel
"""
from __future__ import annotations

import re
import structlog
from dataclasses import dataclass

logger = structlog.get_logger()


_URGENT_TERMS = (
    "urgent", "asap", "right now", "immediately", "today",
    "eod", "end of day", "critical", "emergency", "deadline",
    "before the meeting", "before our meeting", "before my",
    "by noon", "by tomorrow", "by end of",
)
_IMPORTANT_TERMS = (
    "please review", "need your input", "can you look",
    "thoughts on", "follow up", "following up", "circle back",
    "blocker", "blocked on", "signing", "sign off",
)
_NOISE_SENDER_PATTERNS = (
    "noreply@", "no-reply@", "donotreply@", "do-not-reply@",
    "newsletter", "notifications@", "alerts@", "mailer@",
    "marketing@", "promo@", "digest@", "updates@", "support+",
    "@mail.",   # marketing bulk-email subdomains: team@mail.product.com
    "@email.",  # marketing bulk-email subdomains: info@email.product.com
    "@ebay.com", "@nextdoor.com", "@amazon.com", "@paypal.com",
)
_NOISE_SUBJECT_TERMS = (
    "unsubscribe", "weekly digest", "newsletter", "your receipt",
    "order confirmation", "shipping update", "shipping label",
    "update on your shipping", "sale ", "% off",
    "limited time", "last chance", "black friday",
    "discover ", "smarter ways", "ways to use",  # feature-promo marketing patterns
    "started a conversation",  # community notification platforms (Nextdoor, etc.)
)


@dataclass
class GradeInput:
    sender: str = ""
    subject: str = ""
    preview: str = ""
    channel: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "GradeInput":
        return cls(
            sender=str(d.get("from") or d.get("sender") or ""),
            subject=str(d.get("subject") or ""),
            preview=str(
                d.get("preview") or d.get("body") or d.get("text")
                or d.get("snippet") or ""
            ),
            channel=str(d.get("channel") or ""),
        )


class PriorityGrader:
    """Rule-based priority tagger. Synchronous — no I/O."""

    def __init__(
        self,
        vips: list[str] | None = None,
        quiet_contacts: list[str] | None = None,
        upcoming_event_soon: bool = False,
    ) -> None:
        self._vips = [v.lower() for v in (vips or []) if v]
        self._quiet = [q.lower() for q in (quiet_contacts or []) if q]
        self._event_soon = upcoming_event_soon

    # ── Signal helpers ─────────────────────────────────────────────────────────

    def _is_noise(self, g: GradeInput) -> bool:
        sender_lower = g.sender.lower()
        if any(p in sender_lower for p in _NOISE_SENDER_PATTERNS):
            return True
        subject_lower = g.subject.lower()
        if any(p in subject_lower for p in _NOISE_SUBJECT_TERMS):
            return True
        return False

    def _is_vip(self, g: GradeInput) -> bool:
        if not self._vips:
            return False
        sender_lower = g.sender.lower()
        for v in self._vips:
            if v in sender_lower:
                return True
        return False

    def _is_quiet(self, g: GradeInput) -> bool:
        if not self._quiet:
            return False
        sender_lower = g.sender.lower()
        for q in self._quiet:
            if q in sender_lower:
                return True
        return False

    @staticmethod
    def _has_term(haystack: str, terms: tuple[str, ...]) -> bool:
        h = haystack.lower()
        return any(t in h for t in terms)

    # ── Public grading ─────────────────────────────────────────────────────────

    def grade(self, item: dict) -> str:
        """Return one of: urgent | important | defer | ignore."""
        g = GradeInput.from_dict(item)
        combined = f"{g.subject} {g.preview}"

        # Noise short-circuit — bots and marketing never beat a VIP check.
        is_noise = self._is_noise(g)
        is_vip = self._is_vip(g)

        if is_noise and not is_vip:
            return "ignore"

        urgent_keyword = self._has_term(combined, _URGENT_TERMS)
        if urgent_keyword or (self._event_soon and is_vip):
            return "urgent"

        if is_vip:
            return "important"

        if self._is_quiet(g):
            # Quiet contact reaching out is typically worth seeing even if
            # nothing in the text screams urgent.
            return "important"

        if self._has_term(combined, _IMPORTANT_TERMS):
            return "important"

        return "defer"

    def grade_batch(self, items: list[dict]) -> list[tuple[dict, str]]:
        """Grade a list of items, returning (item, tag) pairs in priority order."""
        tagged = [(it, self.grade(it)) for it in items]
        rank = {"urgent": 0, "important": 1, "defer": 2, "ignore": 3}
        tagged.sort(key=lambda p: rank.get(p[1], 99))
        return tagged


# ── Life-context / VIP extraction helpers ─────────────────────────────────────

_VIP_SECTION_RE = re.compile(
    r"(?:important\s+people|vips?|key\s+contacts)[^\n]*\n(.+?)(?:\n\n|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_NAME_LINE_RE = re.compile(r"^\s*[-*]\s*([A-Z][A-Za-z.\s'-]{1,40}?)(?=\s*[:\-—]|$)", re.MULTILINE)


def extract_vips_from_life_context(life_context: str) -> list[str]:
    """Pull plain names from an 'Important People' / 'VIPs' section.

    Looks for a section header, then bullet lines starting with a capitalized
    name. Matches bullets like:
      - Sarah Smith: wife
      - Mike
      * Dr. Patel — cardiologist

    Returns lowercase names / fragments for matching against sender fields.
    """
    if not life_context:
        return []
    m = _VIP_SECTION_RE.search(life_context)
    if not m:
        return []
    section = m.group(1)
    names = [n.strip() for n in _NAME_LINE_RE.findall(section)]
    return [n.lower() for n in names if n]
