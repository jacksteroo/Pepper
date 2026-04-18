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

# Compound-action indicators: when any of these follow a capability phrase,
# the user is asking Pepper to DO work, not just state its capabilities.
# "Can you read my email and tell me what's urgent?" = work request, not capability check.
_COMPOUND_ACTION_INDICATORS = (
    " and tell me", " and show me", " and show", " and list",
    " and find", " and summarize", " and give me", " and let me know",
    " and check", " and look", " and get",
    " to see", " to find out", " to check", " to get",
    " tell me what", " show me what", " let me know what",
    " what's urgent", " what is urgent", " what needs", " what should",
    " what's important", " what is important",
)

# Cross-sentence compound: "Do you have access to Slack? And can you show me?"
# Matches "and" + modal/request verb after a sentence boundary or mid-sentence.
_COMPOUND_MODAL_RE = re.compile(
    r"\band\s+(can|could|would|will|please|tell|show|give|let|list|find|check|look|get|summarize|summarise)\b",
    re.IGNORECASE,
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
    "on my plate", "most important", "give me a brief", "quick brief",
    "what needs my", "what should i focus", "what should i prioritize",
    "what's most important", "what is most important",
)

# Person-lookup regex patterns
# Optional "my" allows "Did my mom send" and "Did Sarah send" with the same pattern.
# Group 1 = modal verb, Group 2 = person name, Group 3 = communication verb.
_PERSON_DID_RE = re.compile(
    r"\b(did|has|have)\s+(?:my\s+)?(\w+(?:\s+\w+)?)\s+"
    r"(send|sent|message[ds]?|email[ds]?|text(?:ed)?|call(?:ed)?|reach(?:ed)?|reply|replied|respond(?:ed)?|write|written|get|gotten|hear|heard)",
    re.IGNORECASE,
)

# Common kinship / relation terms that appear in lowercase in natural speech.
# Matched case-insensitively in _*_KINSHIP_RE patterns below, but in a SEPARATE
# regex from the title-case name pattern so that [A-Z][a-z]+ stays case-SENSITIVE
# (otherwise "last", "the", etc. would be treated as person names).
_KINSHIP_PAT = (
    r"(?:mom|dad|mother|father|sister|brother|wife|husband"
    r"|son|daughter|grandma|grandpa|grandmother|grandfather"
    r"|aunt|uncle|boss|manager|partner)"
)

# Title-case names only — NO re.IGNORECASE so "last", "the", "any" don't match
_FROM_PERSON_RE = re.compile(
    r"\b(from|by)\s+(?:my\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b"
)
_FROM_KINSHIP_RE = re.compile(
    r"\b(?:from|by)\s+(?:my\s+)?" + _KINSHIP_PAT + r"\b",
    re.IGNORECASE,
)
_ABOUT_PERSON_RE = re.compile(
    r"\b(about|regarding|hear from|word from|heard from)\s+(?:my\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b"
)
_ABOUT_KINSHIP_RE = re.compile(
    r"\b(?:about|regarding|hear from|word from|heard from)\s+(?:my\s+)?" + _KINSHIP_PAT + r"\b",
    re.IGNORECASE,
)

# Possessive name patterns: "Mike's email", "Sarah's thread", "reply to Sarah's message"
# Title-case only so common words like "it's", "that's" don't match.
_POSSESSIVE_NAME_RE = re.compile(
    r"\b([A-Z][a-z]+)'s\s+"
    r"(email|emails|message|messages|thread|threads|reply|text|texts|note|notes|latest|recent|last)\b"
)
# Possessive kinship: "my mom's email", "my boss's thread"
_POSSESSIVE_KINSHIP_RE = re.compile(
    r"\b(?:my\s+)?" + _KINSHIP_PAT + r"['\u2019]s\s+"
    r"(email|emails|message|messages|thread|threads|reply|text|texts|note|notes|latest|recent|last)\b",
    re.IGNORECASE,
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
    (("past few days", "last few days", "over the last few days", "couple of days"), "past_few_days"),
    (("last hour", "past hour", "just now", "recently"), "last_hour"),
)

# "since Thursday", "since Monday", etc. — dynamic day-of-week anchor.
_SINCE_DOW_RE = re.compile(
    r"\bsince\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
# "in the last N hours/days", "over the last N hours/days"
_LAST_N_RE = re.compile(
    r"\b(?:in|over|for)\s+the\s+last\s+(\d+)\s+(hour|hours|day|days|week|weeks)\b",
    re.IGNORECASE,
)
# "before my 3pm", "before my 3:30", "before my meeting", "before my call"
_BEFORE_EVENT_RE = re.compile(
    r"\bbefore\s+my\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|meeting|call|appointment|next\s+\w+)\b",
    re.IGNORECASE,
)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _infer_time_scope(text: str) -> str:
    normalized = normalize_user_text(text)
    for phrases, scope in _TIME_SCOPE_TABLE:
        if any(p in normalized for p in phrases):
            return scope

    # Dynamic scopes — emit a stable token the downstream code can reason about
    m = _SINCE_DOW_RE.search(text)
    if m:
        return f"since_{m.group(1).lower()}"
    m = _LAST_N_RE.search(text)
    if m:
        n = m.group(1)
        unit = m.group(2).lower().rstrip("s")
        return f"last_{n}_{unit}"
    m = _BEFORE_EVENT_RE.search(text)
    if m:
        anchor = m.group(1).lower().replace(" ", "_")
        return f"before_{anchor}"

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

    # _FROM_PERSON_RE: group(2) is the title-case name (case-sensitive pattern)
    for m in _FROM_PERSON_RE.finditer(text):
        _add(m.group(2))

    # _FROM_KINSHIP_RE: no capture groups — the whole match is the kinship term;
    # extract the actual kinship word as the entity (the word after "from/by [my]")
    for m in _FROM_KINSHIP_RE.finditer(text):
        # Pull just the kinship word from the match string
        word = m.group(0).split()[-1]
        _add(word)

    # _ABOUT_PERSON_RE: group(2) is the title-case name
    for m in _ABOUT_PERSON_RE.finditer(text):
        _add(m.group(2))

    # _ABOUT_KINSHIP_RE: same kinship extraction as _FROM_KINSHIP_RE
    for m in _ABOUT_KINSHIP_RE.finditer(text):
        word = m.group(0).split()[-1]
        _add(word)

    # Possessive patterns: "Mike's email", "Sarah's thread"
    for m in _POSSESSIVE_NAME_RE.finditer(text):
        _add(m.group(1))

    # Possessive kinship: "my mom's email" — extract the kinship term itself
    for m in _POSSESSIVE_KINSHIP_RE.finditer(text):
        # The kinship word is the token ending with "'s" or "'s"
        for tok in m.group(0).split():
            if "'" in tok or "\u2019" in tok:
                _add(tok.split("'")[0].split("\u2019")[0])
                break

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
        recent_user_messages: list[str] | None = None,
    ) -> RoutingDecision:
        """Return a RoutingDecision for the given message.

        recent_user_messages: previous user turns (most-recent last) used to
        inherit source context for short follow-ups like "anything urgent?"
        after an email question.
        """
        normalized = normalize_user_text(user_message)
        time_scope = _infer_time_scope(user_message)
        entity_targets = _extract_entity_targets(user_message)

        is_compound = (
            contains_any(normalized, _COMPOUND_ACTION_INDICATORS)
            or bool(_COMPOUND_MODAL_RE.search(user_message))
        )

        # ── 1. Generic capability check ────────────────────────────────────────
        if contains_any(normalized, _GENERIC_CAPABILITY_TERMS) and not is_compound:
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
        # Skip when the capability phrase is paired with a concrete action request
        # ("Can you read my email and tell me what's urgent?" = work request, not a
        # capability question). Let those fall through to the normal routing rules.
        if not is_compound and (
            _CAPABILITY_RE.search(user_message) or contains_any(normalized, _CAPABILITY_TERMS)
        ):
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
            reason = "cross-source triage phrase"
            confidence = 1.0
            # Phase 6.5: if no explicit source and the prior turn named one,
            # inherit it instead of fanning out to every channel.
            if not sources and recent_user_messages and len(user_message.split()) <= 6:
                for prior in reversed(recent_user_messages[-2:]):
                    prior_sources = _infer_target_sources(prior)
                    if prior_sources:
                        sources = prior_sources
                        reason = "cross-source triage phrase (carried over)"
                        confidence = 0.7
                        # Downgrade to INBOX_SUMMARY since we have a specific source
                        intent_type = IntentType.INBOX_SUMMARY
                        d = RoutingDecision(
                            intent_type=intent_type,
                            target_sources=sources,
                            action_mode=ActionMode.CALL_TOOLS,
                            time_scope=time_scope,
                            entity_targets=entity_targets,
                            reasoning=reason,
                            confidence=confidence,
                        )
                        self._log(user_message, d)
                        return self._apply_registry(d, capability_registry)

            if not sources:
                sources = ["email", "imessage", "whatsapp", "slack", "calendar"]
            d = RoutingDecision(
                intent_type=IntentType.CROSS_SOURCE_TRIAGE,
                target_sources=sources,
                action_mode=ActionMode.CALL_TOOLS,
                time_scope=time_scope,
                entity_targets=entity_targets,
                reasoning=reason,
                confidence=confidence,
            )
            self._log(user_message, d)
            return self._apply_registry(d, capability_registry)

        # ── 4. Person-centric lookup ───────────────────────────────────────────
        if entity_targets and (
            _PERSON_DID_RE.search(user_message)
            or _FROM_PERSON_RE.search(user_message)
            or _FROM_KINSHIP_RE.search(user_message)
            or _ABOUT_KINSHIP_RE.search(user_message)
            or _POSSESSIVE_NAME_RE.search(user_message)
            or _POSSESSIVE_KINSHIP_RE.search(user_message)
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
            return self._apply_registry(d, capability_registry)

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
            return self._apply_registry(d, capability_registry)

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
            return self._apply_registry(d, capability_registry)

        # ── 7 & 8. Source-targeted queries ────────────────────────────────────
        inferred = _infer_target_sources(user_message)

        # Phase 6.5: carry-over from recent turns.
        # "Anything urgent?" after an email question should inherit "email".
        # Only kicks in when the current turn has no explicit source terms and
        # is short/attention-shaped — longer queries are assumed to stand alone.
        carried_over = False
        if not inferred and recent_user_messages and len(user_message.split()) <= 8:
            attention_shaped = (
                contains_any(normalized, ATTENTION_INTENT_TERMS)
                or contains_any(normalized, _CROSS_SOURCE_TERMS)
                or normalized.startswith("any ")
                or "what about" in normalized
                or "and" == normalized.strip()
            )
            if attention_shaped:
                for prior in reversed(recent_user_messages[-2:]):
                    prior_sources = _infer_target_sources(prior)
                    if prior_sources:
                        inferred = prior_sources
                        carried_over = True
                        break

        if inferred:
            # "Any texts?", "Any emails?" → the word "any" at the start signals a
            # summary request even though it's not in ATTENTION_INTENT_TERMS.
            # Compound work requests ("read email and tell me what's urgent")
            # are summary-shaped by definition — the user wants filtered output,
            # not the raw conversation list.
            is_attention = (
                contains_any(normalized, ATTENTION_INTENT_TERMS)
                or normalized.startswith("any ")
                or is_compound
            )
            if is_attention:
                intent = IntentType.INBOX_SUMMARY
                reason = "attention-intent + source terms"
            else:
                intent = IntentType.CONVERSATION_LOOKUP
                reason = "source terms matched"
            if carried_over:
                reason = f"{reason} (carried over)"
            d = RoutingDecision(
                intent_type=intent,
                target_sources=inferred,
                action_mode=ActionMode.CALL_TOOLS,
                time_scope=time_scope,
                entity_targets=entity_targets,
                reasoning=reason,
                confidence=0.7 if carried_over else 1.0,
            )
            self._log(user_message, d)
            return self._apply_registry(d, capability_registry)

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
    def _apply_registry(
        decision: RoutingDecision,
        registry: "CapabilityRegistry | None",
    ) -> RoutingDecision:
        """Narrow target_sources to only live-available ones.

        If a source hint maps to a registry entry that is NOT available, drop it.
        If all hints are unavailable, set needs_clarification=True with a
        reasoning string the LLM/UI can render as a precise explanation
        instead of a generic apology.
        """
        if registry is None or not decision.target_sources:
            return decision
        if decision.target_sources == ["all"] or decision.target_sources == ["unknown"]:
            return decision

        from agent.capability_registry import CapabilityStatus, SOURCE_ALIASES

        reachable: list[str] = []
        dropped: list[tuple[str, str]] = []  # (hint, status)
        for hint in decision.target_sources:
            keys = SOURCE_ALIASES.get(hint.lower(), [hint])
            # Hint is reachable if ANY mapped registry key is available.
            statuses = []
            any_available = False
            for key in keys:
                status = registry.get_status(key)
                statuses.append(status.value)
                if status == CapabilityStatus.AVAILABLE:
                    any_available = True
                    break
            if any_available:
                reachable.append(hint)
            else:
                # Only record as "dropped" if the registry actually knew about it.
                if any(registry.get(k) for k in keys):
                    dropped.append((hint, statuses[0] if statuses else "unknown"))
                else:
                    # No registry entry — keep the hint so routing is unchanged
                    # for sources the registry doesn't track.
                    reachable.append(hint)

        if not reachable:
            decision.needs_clarification = True
            decision.reasoning = (
                f"{decision.reasoning}; all sources unavailable "
                f"({', '.join(f'{h}={s}' for h, s in dropped)})"
            )
            # Keep original sources so the clarifying question can list them.
            return decision

        if dropped:
            decision.target_sources = reachable
            decision.reasoning = (
                f"{decision.reasoning}; narrowed to reachable "
                f"(dropped: {', '.join(h for h, _ in dropped)})"
            )
        return decision

    def route_multi(
        self,
        user_message: str,
        capability_registry: "CapabilityRegistry | None" = None,
        recent_user_messages: list[str] | None = None,
    ) -> list[RoutingDecision]:
        """Split a multi-intent query into a list of RoutingDecisions.

        Splits on " and " / "; " only when each side independently parses as a
        distinct source or entity target. Queries that don't split cleanly
        return a single-element list wrapping the normal route() result.
        """
        # Only try splits when there's a plausible conjunction.
        has_and = re.search(r"\s+and\s+", user_message, re.IGNORECASE) is not None
        has_semi = ";" in user_message
        if not (has_and or has_semi):
            return [self.route(user_message, capability_registry, recent_user_messages)]

        # Split on ";" first, then on " and " within each chunk.
        chunks: list[str] = []
        for semi_chunk in re.split(r";\s*", user_message):
            if not semi_chunk.strip():
                continue
            parts = re.split(r"\s+and\s+", semi_chunk, flags=re.IGNORECASE)
            chunks.extend(p.strip() for p in parts if p.strip())

        if len(chunks) < 2:
            return [self.route(user_message, capability_registry, recent_user_messages)]

        # Each chunk must independently hit a source OR contain an entity target
        # for the split to be worthwhile. Otherwise "check email and tell me" —
        # where "tell me" is a bare action — should stay as a single intent.
        decisions: list[RoutingDecision] = []
        for chunk in chunks:
            sources = _infer_target_sources(chunk)
            entities = _extract_entity_targets(chunk)
            if not sources and not entities:
                return [self.route(user_message, capability_registry, recent_user_messages)]
            decisions.append(self.route(chunk, capability_registry, recent_user_messages))

        # Reject a split if every decision landed in GENERAL_CHAT — that means the
        # split was meaningless and the whole message should be routed as one.
        if all(d.intent_type == IntentType.GENERAL_CHAT for d in decisions):
            return [self.route(user_message, capability_registry, recent_user_messages)]

        logger.info(
            "query_route_multi",
            n_intents=len(decisions),
            intents=[d.intent_type.value for d in decisions],
            message_preview=user_message[:100],
        )
        return decisions

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
