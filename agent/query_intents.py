"""Shared query-intent helpers for source-specific tools.

These helpers centralize the common trigger semantics Pepper uses across
email, calendar, and messaging sources. Each subsystem should only need to
declare its source aliases plus any truly source-specific phrases.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

EMAIL_QUERY_TERMS = (
    "email",
    "emails",
    "inbox",
    "gmail",
    "yahoo",
    "mail",
    "unread",
    "did i get",
    "any emails",
    "check my email",
    "from my inbox",
    "sent me an email",
)

IMESSAGE_QUERY_TERMS = (
    "imessage",
    "text",
    "texts",
    "texted",
    "texting",
    "sms",
    "did i get a text",
    "any texts",
    "who texted",
)

WHATSAPP_QUERY_TERMS = (
    "whatsapp",
    "whats app",
    "wa ",
    " wa,",
    "family group",
    "friend group",
    "family chat",
    "friend chat",
    "group chat",
)

SLACK_QUERY_TERMS = (
    "slack",
    "channel",
    "workspace",
    "work chat",
    "team chat",
    "due friday",
    "due monday",
    "by eod",
    "end of day",
)

CALENDAR_QUERY_TERMS = (
    "calendar",
    "schedule",
    "meeting",
    "meetings",
    "event",
    "events",
    "appointment",
    "appointments",
    "availability",
    "free",
    "busy",
    "what do i have",
    "what's on",
    "what is on",
    "when am i",
    "do i have anything",
)

ATTENTION_INTENT_TERMS = (
    "recent",
    "latest",
    "new",
    "unread",
    "summary",
    "summarize",
    "summarise",
    "recap",
    "overview",
    "catch me up",
    "need to know",
    "need to be aware",
    "aware of",
    "what should i know",
    "anything i should know",
    "what do i need to know",
    "who needs a reply",
    "need a reply",
    "needs a reply",
)

ACTION_ITEM_INTENT_TERMS = (
    "action item",
    "action items",
    "follow up",
    "follow-up",
    "todo",
    "to do",
    "need to reply",
    "needs a reply",
    "needs reply",
    "need a response",
    "needs a response",
    "what do i owe",
    "what am i missing",
    "what needs my attention",
)

SEARCH_INTENT_TERMS = (
    "search",
    "find",
    "look for",
    "lookup",
)

NON_EMAIL_CHANNEL_TERMS = (
    "whatsapp",
    "imessage",
    "text",
    "texts",
    "sms",
    "slack",
)

_RECENT_HOURS_HINTS: tuple[tuple[tuple[str, ...], int], ...] = (
    (("overnight", "last night", "this morning"), 12),
    (("today",), 24),
    (("yesterday",), 36),
    (("this week",), 168),
)

_CALENDAR_DAY_HINTS: tuple[tuple[tuple[str, ...], int], ...] = (
    (("today", "tonight"), 1),
    (("tomorrow",), 2),
    (("next week",), 14),
)


def normalize_user_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def contains_any(text: str, phrases: Iterable[str]) -> bool:
    normalized = normalize_user_text(text)
    return any(phrase.lower() in normalized for phrase in phrases)


def is_search_request(user_message: str) -> bool:
    return contains_any(user_message, SEARCH_INTENT_TERMS)


def is_source_query(
    user_message: str,
    source_terms: Iterable[str],
    *,
    extra_terms: Iterable[str] = (),
    disallowed_terms: Iterable[str] = (),
) -> bool:
    if disallowed_terms and contains_any(user_message, disallowed_terms):
        if not contains_any(user_message, source_terms):
            return False
    return contains_any(user_message, tuple(source_terms) + tuple(extra_terms))


def is_attention_request(
    user_message: str,
    source_terms: Iterable[str],
    *,
    extra_terms: Iterable[str] = (),
    disallowed_terms: Iterable[str] = (),
) -> bool:
    if not is_source_query(
        user_message,
        source_terms,
        disallowed_terms=disallowed_terms,
    ):
        return False
    if is_search_request(user_message):
        return False
    return contains_any(user_message, tuple(ATTENTION_INTENT_TERMS) + tuple(extra_terms))


def is_action_item_request(
    user_message: str,
    source_terms: Iterable[str],
    *,
    extra_terms: Iterable[str] = (),
    disallowed_terms: Iterable[str] = (),
) -> bool:
    if not is_source_query(
        user_message,
        source_terms,
        disallowed_terms=disallowed_terms,
    ):
        return False
    return contains_any(user_message, tuple(ACTION_ITEM_INTENT_TERMS) + tuple(extra_terms))


def infer_recent_hours(user_message: str, default: int = 24) -> int:
    normalized = normalize_user_text(user_message)
    for phrases, hours in _RECENT_HOURS_HINTS:
        if any(phrase in normalized for phrase in phrases):
            return hours
    return default


def infer_calendar_days(user_message: str, default: int = 7) -> int:
    normalized = normalize_user_text(user_message)
    for phrases, days in _CALENDAR_DAY_HINTS:
        if any(phrase in normalized for phrase in phrases):
            return days
    return default
