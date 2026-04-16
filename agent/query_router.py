"""
Phase 6.1 — Real Intent Router.

Classifies user messages into structured routing decisions before prompt
assembly or tool execution. Separates "what is the user asking?" from
"which tool should I call?" — two concerns the old code conflated.

The router:
  1. Deterministic rules handle obvious queries instantly (no LLM call)
  2. Falls through to GENERAL_CHAT for anything ambiguous
  3. Logs every decision under "query_route" for eval consumption

Router output is used by core.py to:
  - Answer capability-check queries from the CapabilityRegistry (short-circuit)
  - Extract entity targets for person-centric lookups
  - Log routing decisions so eval can compare intent vs actual tool calls
"""
from __future__ import annotations

import re
import structlog
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from agent.query_intents import (
    normalize_user_text,
    contains_any,
    EMAIL_QUERY_TERMS,
    IMESSAGE_QUERY_TERMS,
    WHATSAPP_QUERY_TERMS,
    SLACK_QUERY_TERMS,
    CALENDAR_QUERY_TERMS,
    ATTENTION_INTENT_TERMS,
    ACTION_ITEM_INTENT_TERMS,
    NON_EMAIL_CHANNEL_TERMS,
)

if TYPE_CHECKING:
    from agent.capability_registry import CapabilityRegistry

logger = structlog.get_logger()


# ── Enums ──────────────────────────────────────────────────────────────────────

class IntentType(str, Enum):
    CAPABILITY_CHECK = "capability_check"
    INBOX_SUMMARY = "inbox_summary"
    ACTION_ITEMS = "action_items"
    PERSON_LOOKUP = "person_lookup"
    CONVERSATION_LOOKUP = "conversation_lookup"
    SCHEDULE_LOOKUP = "schedule_lookup"
    CROSS_SOURCE_TRIAGE = "cross_source_triage"
    GENERAL_CHAT = "general_chat"
    UNKNOWN = "unknown"


class ActionMode(str, Enum):
    ANSWER_FROM_CONTEXT = "answer_from_context"
    CALL_TOOLS = "call_tools"
    ASK_CLARIFYING_QUESTION = "ask_clarifying_question"


# ── RoutingDecision dataclass ──────────────────────────────────────────────────

@dataclass
class RoutingDecision:
    intent_type: IntentType
    target_sources: list[str]        # e.g. ["email", "imessage"]
    action_mode: ActionMode
    time_scope: str = "default"      # "today", "this_week", "overnight", etc.
    entity_targets: list[str] = field(default_factory=list)  # person names
    needs_clarification: bool = False
    confidence: float = 1.0          # deterministic → 1.0; fallback → lower
    reasoning: str = ""              # logged for evals, never shown to user

    def includes_source(self, source: str) -> bool:
        return source in self.target_sources or "all" in self.target_sources

    def is_multi_source(self) -> bool:
        return len(self.target_sources) > 1


# ── Deterministic pattern tables ──────────────────────────────────────────────

# Specific capability check phrases
_CAPABILITY_RE = re.compile(
    r"\b(can you|do you|are you able to|have you got|do you have).*"
    r"\b(access|read|see|check|get|connect|use|look at)\b",
    re.IGNORECASE,
)
_CAPABILITY_TERMS = (
    "can you access", "do you have access", "can you read", "can you see",
    "can you check", "do you see", "are you connected", "do you connect",
    "can you get", "can you look at", "access to my", "can you use my",
    "do you have my",
)

# Generic "what can you do" phrases
_GENERIC_CAPABILITY_TERMS = (
    "what can you do", "what are your capabilities", "what can you access",
    "what do you have access to", "what sources do you", "what integrations",
    "what data do you", "what can you see", "what are you connected to",
)

# Cross-source triage phrases (implies multiple sources or unspecified)
_CROSS_SOURCE_TERMS = (
    "who do i owe", "who needs a reply", "owe replies", "owe a reply",
    "what needs my attention", "what am i missing", "anything important",
    "catch me up", "what came in", "what's new", "what is new",
    "anything overnight", "anything this morning", "anything urgent",
    "triage", "anything i should", "what should i know", "what do i need to know",
    "what's going on", "any updates", "anything to know",
    "check my messages", "my messages", "check messages",
    "any word from", "word from", "hear from anyone",
)

# Person-lookup regex patterns
# Optional "my" allows "Did my mom send" and "Did Sarah send" with the same pattern.
# Group 1 = modal verb, Group 2 = person name, Group 3 = communication verb.
_PERSON_DID_RE = re.compile(
    r"\b(did|has|have)\s+(?:my\s+)?(\w+(?:\s+\w+)?)\s+"
    r"(send|sent|message[ds]?|email[ds]?|text(?:ed)?|call(?:ed)?|reach(?:ed)?|reply|replied|respond(?:ed)?|write|written|get|gotten|hear|heard)",
    re.IGNORECASE,
)
_FROM_PERSON_RE = re.compile(
    r"\b(from|by)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b"
)
_ABOUT_PERSON_RE = re.compile(
    r"\b(about|regarding|hear from|word from)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b"
)

# Known data source names that should not be treated as person entities
_SOURCE_NAME_BLOCKLIST = frozenset({
    "slack", "gmail", "yahoo", "whatsapp", "imessage", "telegram", "email",
    "sms", "calendar", "google", "facebook", "instagram", "twitter", "linkedin",
    "notion", "github", "jira", "linear",
})

# Time scope mapping
_TIME_SCOPE_TABLE: tuple[tuple[tuple[str, ...], str], ...] = (
    (("overnight", "last night"), "overnight"),
    (("this morning",), "today"),
    (("today", "tonight"), "today"),
    (("yesterday",), "yesterday"),
    (("this week", "past week", "last week"), "this_week"),
    (("last hour", "past hour", "just now", "recently"), "last_hour"),
)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _infer_time_scope(text: str) -> str:
    normalized = normalize_user_text(text)
    for phrases, scope in _TIME_SCOPE_TABLE:
        if any(p in normalized for p in phrases):
            return scope
    return "default"


_STOP_WORDS = frozenset({
    "i", "me", "my", "you", "your", "we", "our", "they", "it", "the",
    "a", "an", "this", "that", "these", "those",
    # Communication verbs that look like names in some regex captures
    "sent", "send", "text", "texted", "replied", "reply", "called", "messaged",
    "emailed", "heard", "hear", "gotten", "wrote", "write",
})


def _extract_entity_targets(text: str) -> list[str]:
    """Extract likely person/contact names from a message.

    Uses fixed group indices per regex so the correct capture group is always
    used regardless of m.lastindex. Filters out known data source names and
    stop words so "from Slack" doesn't produce an entity of "Slack".
    """
    targets: list[str] = []

    def _add(name: str) -> None:
        n = name.strip()
        if n and n.lower() not in _STOP_WORDS and n.lower() not in _SOURCE_NAME_BLOCKLIST:
            targets.append(n)

    # _PERSON_DID_RE: group(2) is always the person name
    for m in _PERSON_DID_RE.finditer(text):
        _add(m.group(2))

    # _FROM_PERSON_RE: group(2) is always the person name
    for m in _FROM_PERSON_RE.finditer(text):
        _add(m.group(2))

    # _ABOUT_PERSON_RE: group(2) is always the person name
    for m in _ABOUT_PERSON_RE.finditer(text):
        _add(m.group(2))

    return list(dict.fromkeys(targets))  # deduplicate, preserve order


# Terms that unambiguously mean "email" — used to override channel suppression
_EXPLICIT_EMAIL_TERMS = ("email", "emails", "gmail", "yahoo", "inbox", "unread")


def _infer_target_sources(text: str) -> list[str]:
    """Infer which data sources a message is targeting based on keyword presence.

    Email suppression: when a non-email channel (WhatsApp, iMessage, Slack) is
    mentioned alongside broad email terms (e.g. "mail"), suppress email to avoid
    false positives like "WhatsApp messages" matching "messages" → email.
    However, when the user EXPLICITLY says "email", "Gmail", "inbox", etc. in the
    same message, include email regardless of the other channels present.
    """
    normalized = normalize_user_text(text)
    sources: list[str] = []

    if contains_any(normalized, EMAIL_QUERY_TERMS):
        has_non_email = contains_any(normalized, NON_EMAIL_CHANNEL_TERMS)
        has_explicit_email = contains_any(normalized, _EXPLICIT_EMAIL_TERMS)
        if has_explicit_email or not has_non_email:
            sources.append("email")

    if contains_any(normalized, IMESSAGE_QUERY_TERMS):
        sources.append("imessage")

    if contains_any(normalized, WHATSAPP_QUERY_TERMS):
        sources.append("whatsapp")

    if contains_any(normalized, SLACK_QUERY_TERMS):
        sources.append("slack")

    if contains_any(normalized, CALENDAR_QUERY_TERMS):
        sources.append("calendar")

    return sources


# ── QueryRouter ────────────────────────────────────────────────────────────────

class QueryRouter:
    """Map user messages to structured RoutingDecisions.

    Deterministic — synchronous, no I/O, no LLM. Designed to run inline in
    the main chat loop before any tool calls or prompt assembly.

    Rule priority (first match wins):
      1. Generic capability check
      2. Specific capability check
      3. Cross-source triage
      4. Person-centric lookup
      5. Schedule/calendar lookup
      6. Action items
      7. Inbox / message summary (attention + source terms)
      8. Conversation lookup (source terms without attention)
      9. General chat (fallback)
    """

    def route(
        self,
        user_message: str,
        capability_registry: "CapabilityRegistry | None" = None,
    ) -> RoutingDecision:
        """Return a RoutingDecision for the given message."""
        normalized = normalize_user_text(user_message)
        time_scope = _infer_time_scope(user_message)
        entity_targets = _extract_entity_targets(user_message)

        # ── 1. Generic capability check ────────────────────────────────────────
        if contains_any(normalized, _GENERIC_CAPABILITY_TERMS):
            d = RoutingDecision(
                intent_type=IntentType.CAPABILITY_CHECK,
                target_sources=["all"],
                action_mode=ActionMode.ANSWER_FROM_CONTEXT,
                time_scope=time_scope,
                entity_targets=entity_targets,
                reasoning="generic capability query",
            )
            self._log(user_message, d)
            return d

        # ── 2. Specific capability check ───────────────────────────────────────
        if _CAPABILITY_RE.search(user_message) or contains_any(normalized, _CAPABILITY_TERMS):
            sources = _infer_target_sources(user_message) or ["unknown"]
            d = RoutingDecision(
                intent_type=IntentType.CAPABILITY_CHECK,
                target_sources=sources,
                action_mode=ActionMode.ANSWER_FROM_CONTEXT,
                time_scope=time_scope,
                entity_targets=entity_targets,
                reasoning="capability-check pattern",
            )
            self._log(user_message, d)
            return d

        # ── 3. Cross-source triage ─────────────────────────────────────────────
        # Skip when entity_targets are present — person-lookup (rule 4) handles
        # "Any word from David?" better than triage handles it.
        if contains_any(normalized, _CROSS_SOURCE_TERMS) and not entity_targets:
            sources = _infer_target_sources(user_message)
            if not sources:
                sources = ["email", "imessage", "whatsapp", "slack"]
            d = RoutingDecision(
                intent_type=IntentType.CROSS_SOURCE_TRIAGE,
                target_sources=sources,
                action_mode=ActionMode.CALL_TOOLS,
                time_scope=time_scope,
                entity_targets=entity_targets,
                reasoning="cross-source triage phrase",
            )
            self._log(user_message, d)
            return d

        # ── 4. Person-centric lookup ───────────────────────────────────────────
        if entity_targets and (
            _PERSON_DID_RE.search(user_message)
            or _FROM_PERSON_RE.search(user_message)
            or contains_any(normalized, ("hear from", "heard from", "word from"))
        ):
            sources = _infer_target_sources(user_message)
            if not sources:
                sources = ["email", "imessage", "whatsapp", "slack"]
            d = RoutingDecision(
                intent_type=IntentType.PERSON_LOOKUP,
                target_sources=sources,
                action_mode=ActionMode.CALL_TOOLS,
                time_scope=time_scope,
                entity_targets=entity_targets,
                reasoning="person-lookup pattern",
            )
            self._log(user_message, d)
            return d

        # ── 5. Schedule / calendar lookup ─────────────────────────────────────
        if contains_any(normalized, CALENDAR_QUERY_TERMS):
            d = RoutingDecision(
                intent_type=IntentType.SCHEDULE_LOOKUP,
                target_sources=["calendar"],
                action_mode=ActionMode.CALL_TOOLS,
                time_scope=time_scope,
                entity_targets=entity_targets,
                reasoning="calendar terms",
            )
            self._log(user_message, d)
            return d

        # ── 6. Action items ────────────────────────────────────────────────────
        if contains_any(normalized, ACTION_ITEM_INTENT_TERMS):
            sources = _infer_target_sources(user_message)
            if not sources:
                sources = ["email", "imessage", "whatsapp", "slack"]
            d = RoutingDecision(
                intent_type=IntentType.ACTION_ITEMS,
                target_sources=sources,
                action_mode=ActionMode.CALL_TOOLS,
                time_scope=time_scope,
                entity_targets=entity_targets,
                reasoning="action-item terms",
            )
            self._log(user_message, d)
            return d

        # ── 7 & 8. Source-targeted queries ────────────────────────────────────
        inferred = _infer_target_sources(user_message)
        if inferred:
            # "Any texts?", "Any emails?" → the word "any" at the start signals a
            # summary request even though it's not in ATTENTION_INTENT_TERMS.
            is_attention = contains_any(normalized, ATTENTION_INTENT_TERMS) or normalized.startswith("any ")
            if is_attention:
                intent = IntentType.INBOX_SUMMARY
                reason = "attention-intent + source terms"
            else:
                intent = IntentType.CONVERSATION_LOOKUP
                reason = "source terms matched"
            d = RoutingDecision(
                intent_type=intent,
                target_sources=inferred,
                action_mode=ActionMode.CALL_TOOLS,
                time_scope=time_scope,
                entity_targets=entity_targets,
                reasoning=reason,
            )
            self._log(user_message, d)
            return d

        # ── 9. General chat (fallback) ─────────────────────────────────────────
        d = RoutingDecision(
            intent_type=IntentType.GENERAL_CHAT,
            target_sources=[],
            action_mode=ActionMode.ANSWER_FROM_CONTEXT,
            time_scope=time_scope,
            entity_targets=entity_targets,
            confidence=0.6,
            reasoning="no specific intent signals",
        )
        self._log(user_message, d)
        return d

    @staticmethod
    def _log(user_message: str, decision: RoutingDecision) -> None:
        logger.info(
            "query_route",
            intent=decision.intent_type.value,
            sources=decision.target_sources,
            action_mode=decision.action_mode.value,
            time_scope=decision.time_scope,
            entity_targets=decision.entity_targets,
            confidence=decision.confidence,
            reasoning=decision.reasoning,
            message_preview=user_message[:100],
        )
