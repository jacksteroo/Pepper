"""Tests for Phase 6.1 — QueryRouter."""
from __future__ import annotations

import pytest

from agent.query_router import (
    ActionMode,
    IntentType,
    QueryRouter,
    RoutingDecision,
    _extract_entity_targets,
    _infer_target_sources,
    _infer_time_scope,
)


@pytest.fixture
def router():
    return QueryRouter()


# ── RoutingDecision helpers ────────────────────────────────────────────────────

def test_routing_decision_includes_source():
    d = RoutingDecision(
        intent_type=IntentType.INBOX_SUMMARY,
        target_sources=["email", "slack"],
        action_mode=ActionMode.CALL_TOOLS,
    )
    assert d.includes_source("email")
    assert d.includes_source("slack")
    assert not d.includes_source("whatsapp")


def test_routing_decision_includes_all():
    d = RoutingDecision(
        intent_type=IntentType.CAPABILITY_CHECK,
        target_sources=["all"],
        action_mode=ActionMode.ANSWER_FROM_CONTEXT,
    )
    assert d.includes_source("email")
    assert d.includes_source("calendar")


def test_routing_decision_is_multi_source():
    single = RoutingDecision(
        intent_type=IntentType.SCHEDULE_LOOKUP,
        target_sources=["calendar"],
        action_mode=ActionMode.CALL_TOOLS,
    )
    multi = RoutingDecision(
        intent_type=IntentType.CROSS_SOURCE_TRIAGE,
        target_sources=["email", "imessage", "slack"],
        action_mode=ActionMode.CALL_TOOLS,
    )
    assert not single.is_multi_source()
    assert multi.is_multi_source()


# ── _infer_time_scope ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("message,expected_scope", [
    ("What came in overnight?", "overnight"),
    ("Anything from last night?", "overnight"),
    ("What do I have today?", "today"),
    ("This morning's emails", "today"),
    ("What happened yesterday?", "yesterday"),
    ("Recap for this week", "this_week"),
    ("Anything from the past week?", "this_week"),
    ("Just say hi", "default"),
])
def test_infer_time_scope(message, expected_scope):
    assert _infer_time_scope(message) == expected_scope


# ── _extract_entity_targets ────────────────────────────────────────────────────

def test_extract_entity_from_did_send():
    targets = _extract_entity_targets("Did Sarah send anything?")
    assert "Sarah" in targets


def test_extract_entity_from_person():
    targets = _extract_entity_targets("Any messages from John Smith?")
    assert "John Smith" in targets or "John" in targets


def test_extract_entity_no_person():
    targets = _extract_entity_targets("What's on my calendar today?")
    assert targets == []


def test_extract_entity_deduplicates():
    targets = _extract_entity_targets("Did Sarah text? Any word from Sarah?")
    assert targets.count("Sarah") == 1


# ── _infer_target_sources ──────────────────────────────────────────────────────

def test_infer_email_source():
    assert "email" in _infer_target_sources("Check my Gmail inbox")


def test_infer_email_shadowed_by_whatsapp():
    # "text" in WhatsApp query should not trigger email
    assert "email" not in _infer_target_sources("Any WhatsApp messages?")


def test_infer_imessage_source():
    assert "imessage" in _infer_target_sources("Any texts from mom?")


def test_infer_no_imessage_source_for_context():
    assert "imessage" not in _infer_target_sources("what's my life context?")


def test_infer_whatsapp_source():
    assert "whatsapp" in _infer_target_sources("Check my WhatsApp groups")


def test_infer_slack_source():
    assert "slack" in _infer_target_sources("Any updates in Slack?")


def test_infer_calendar_source():
    assert "calendar" in _infer_target_sources("What meetings do I have today?")


def test_infer_multiple_sources():
    sources = _infer_target_sources("Check both my email and Slack")
    assert "email" in sources
    assert "slack" in sources


def test_infer_no_sources_for_general_chat():
    assert _infer_target_sources("How do I make pasta?") == []


# ── Generic capability checks ──────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "What can you do?",
    "What are your capabilities?",
    "What can you access?",
    "What do you have access to?",
    "What data do you have?",
    "What can you see?",
])
def test_generic_capability_check(router, message):
    d = router.route(message)
    assert d.intent_type == IntentType.CAPABILITY_CHECK
    assert d.target_sources == ["all"]
    assert d.action_mode == ActionMode.ANSWER_FROM_CONTEXT


# ── Specific capability checks ─────────────────────────────────────────────────

@pytest.mark.parametrize("message,expected_source", [
    ("Can you read my email?", "email"),
    ("Do you have access to my Gmail?", "email"),
    ("Can you see my iMessages?", "imessage"),
    ("Can you check my texts?", "imessage"),
    ("Do you have access to my WhatsApp?", "whatsapp"),
    ("Can you see my Slack?", "slack"),
    ("Can you read my calendar?", "calendar"),
])
def test_specific_capability_check(router, message, expected_source):
    d = router.route(message)
    assert d.intent_type == IntentType.CAPABILITY_CHECK
    assert d.action_mode == ActionMode.ANSWER_FROM_CONTEXT
    assert expected_source in d.target_sources or d.target_sources == ["unknown"]


def test_filesystem_capability_check(router):
    d = router.route("Do you have access to docs/LIFE_CONTEXT.md?")
    assert d.intent_type == IntentType.CAPABILITY_CHECK
    assert d.target_sources == ["filesystem"]
    assert d.action_mode == ActionMode.ANSWER_FROM_CONTEXT


# ── Cross-source triage ────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "Who do I owe replies to?",
    "What needs my attention?",
    "What am I missing?",
    "Catch me up",
    "What came in this morning?",
    "Anything important I should know?",
    "Anything urgent?",
    "Any updates?",
])
def test_cross_source_triage(router, message):
    d = router.route(message)
    assert d.intent_type == IntentType.CROSS_SOURCE_TRIAGE
    assert d.action_mode == ActionMode.CALL_TOOLS
    assert len(d.target_sources) > 1


def test_cross_source_triage_defaults_to_all_comms(router):
    d = router.route("What am I missing today?")
    assert "email" in d.target_sources or len(d.target_sources) > 1


# ── Person-centric lookups ─────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "Did Sarah send anything?",
    "Any messages from John?",
    "Has mom replied?",
    "Did Mike text me?",
    "Any word from David?",
])
def test_person_lookup_routing(router, message):
    d = router.route(message)
    assert d.intent_type == IntentType.PERSON_LOOKUP
    assert d.action_mode == ActionMode.CALL_TOOLS
    assert len(d.entity_targets) > 0


def test_person_lookup_entity_extraction(router):
    d = router.route("Did Sarah reply to my email?")
    assert "Sarah" in d.entity_targets


# ── Schedule lookups ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "What do I have today?",
    "Any meetings this week?",
    "What's on my calendar?",
    "Am I free tomorrow afternoon?",
    "What appointments do I have?",
])
def test_schedule_lookup(router, message):
    d = router.route(message)
    assert d.intent_type == IntentType.SCHEDULE_LOOKUP
    assert "calendar" in d.target_sources
    assert d.action_mode == ActionMode.CALL_TOOLS


# ── Action items ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "What do I need to follow up on?",
    "Any action items?",
    "Who needs a response from me?",
    "What needs a reply?",
    "What do I owe people?",
    "What's on my to-do list?",
])
def test_action_items_routing(router, message):
    d = router.route(message)
    assert d.intent_type == IntentType.ACTION_ITEMS
    assert d.action_mode == ActionMode.CALL_TOOLS


# ── Inbox / message summary ────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "Summarize my emails",
    "What's in my inbox?",
    "Give me an overview of recent texts",
    "Recap my WhatsApp messages",
    "What's the latest from Slack?",
])
def test_inbox_summary_routing(router, message):
    d = router.route(message)
    assert d.intent_type in (IntentType.INBOX_SUMMARY, IntentType.CONVERSATION_LOOKUP)
    assert d.action_mode == ActionMode.CALL_TOOLS
    assert len(d.target_sources) > 0


# ── Conversation lookup ────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "Show me my email from last week",
    "Get my iMessages",
    "Check Slack",
])
def test_conversation_lookup_routing(router, message):
    d = router.route(message)
    assert d.intent_type in (
        IntentType.CONVERSATION_LOOKUP,
        IntentType.INBOX_SUMMARY,
        IntentType.SCHEDULE_LOOKUP,
        IntentType.ACTION_ITEMS,
    )
    assert d.action_mode == ActionMode.CALL_TOOLS


def test_filesystem_lookup_routing(router):
    d = router.route("What is in /data/messages folder?")
    assert d.intent_type == IntentType.CONVERSATION_LOOKUP
    assert d.target_sources == ["filesystem"]
    assert d.action_mode == ActionMode.CALL_TOOLS


# ── General chat fallback ──────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "Hello!",
    "Thanks",
    "How do I make sourdough bread?",
    "What's the capital of France?",
    "Write me a poem about autumn",
])
def test_general_chat_fallback(router, message):
    d = router.route(message)
    assert d.intent_type == IntentType.GENERAL_CHAT
    assert d.action_mode == ActionMode.ANSWER_FROM_CONTEXT
    assert d.target_sources == []
    assert d.confidence < 1.0


# ── Compound capability check (P1 fix) ────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "Can you read my email and tell me what's urgent?",
    "Can you check my texts and show me the important ones?",
    "Can you get my emails and find anything from Sarah?",
    "Do you have access to Slack? And can you show me the latest?",
    "Can you read my email to see what needs a reply?",
])
def test_compound_capability_routes_to_work_not_status(router, message):
    """Compound requests should do the work, not return a capability status."""
    d = router.route(message)
    assert d.intent_type != IntentType.CAPABILITY_CHECK, (
        f"Message: '{message}'\n"
        "Expected a work intent (not CAPABILITY_CHECK) for a compound request.\n"
        f"Got: {d.intent_type.value} — reasoning: {d.reasoning}"
    )
    assert d.action_mode == ActionMode.CALL_TOOLS, (
        f"Compound request should have action_mode=call_tools, got {d.action_mode.value}"
    )


# ── Kinship / lowercase person targets (P2 fix) ────────────────────────────────

@pytest.mark.parametrize("message,expected_entity", [
    ("Any messages from mom?", "mom"),
    ("Any word from mom?", "mom"),
    ("Any news from dad?", "dad"),
    ("Did mom send anything?", "mom"),
    ("Has my wife replied?", "wife"),
    ("Anything from my husband?", "husband"),
    ("Hear from boss lately?", "boss"),
])
def test_kinship_terms_extracted_as_entities(router, message, expected_entity):
    """Lowercase kinship / relation terms must be recognized as person targets."""
    d = router.route(message)
    entities_lower = [e.lower() for e in d.entity_targets]
    assert expected_entity in entities_lower, (
        f"Message: '{message}'\n"
        f"Expected entity '{expected_entity}' in {d.entity_targets}"
    )
    assert d.intent_type == IntentType.PERSON_LOOKUP, (
        f"Message: '{message}'\n"
        f"Expected PERSON_LOOKUP, got {d.intent_type.value}"
    )


@pytest.mark.parametrize("message", [
    "Show me my email from last week",
    "Any updates from the team?",
    "Any word from the team?",
])
def test_non_person_from_clauses_do_not_extract_entities(router, message):
    """Generic phrases like 'from the team' or 'from last week' must not produce entity targets."""
    d = router.route(message)
    # lowercase non-name words like "team", "last" should not appear in entity_targets
    entities_lower = [e.lower() for e in d.entity_targets]
    assert "last" not in entities_lower, f"'last' should not be an entity: {d.entity_targets}"
    assert "team" not in entities_lower, f"'team' should not be an entity: {d.entity_targets}"
    assert "week" not in entities_lower, f"'week' should not be an entity: {d.entity_targets}"


# ── Logging (smoke test) ───────────────────────────────────────────────────────

def test_router_logs_decision(router, caplog):
    import logging
    with caplog.at_level(logging.INFO):
        router.route("What are your capabilities?")
    # The routing decision should be logged (structlog → stdlib bridge in tests)
    # Just verify it doesn't raise


# ── Prompt/tool contract validation ───────────────────────────────────────────

def test_validate_prompt_tool_references_no_stale_names():
    """Regression: capability block must not reference nonexistent tool names."""
    from agent.life_context import build_capability_block, validate_prompt_tool_references

    # The full set of actually registered tool names in core.py
    REGISTERED_TOOLS = {
        "save_memory", "search_memory", "update_life_context",
        "get_upcoming_events", "get_calendar_events_range", "list_calendars",
        "get_recent_emails", "search_emails", "get_email_unread_counts",
        "get_email_action_items", "get_email_summary",
        "get_recent_imessages", "get_imessage_conversation", "search_imessages",
        "get_recent_whatsapp_chats", "get_whatsapp_chat", "get_whatsapp_messages",
        "search_whatsapp", "get_whatsapp_groups",
        "search_slack", "get_slack_channel_messages", "get_slack_deadlines",
        "list_slack_channels",
        "get_contact_profile", "find_quiet_contacts", "search_contacts",
        "get_comms_health_summary", "get_overdue_responses",
        "get_relationship_balance_report",
        "search_images", "search_web", "get_driving_time", "inspect_local_path",
    }

    block = build_capability_block()
    unknown = validate_prompt_tool_references(block, REGISTERED_TOOLS)
    assert unknown == [], (
        f"Capability block references nonexistent tools: {unknown}\n"
        "Update build_capability_block() to use the correct registered tool names."
    )


# ── Phase 6.5 — Router hardening ──────────────────────────────────────────────

class _FakeCapability:
    def __init__(self, status):
        self.status = status
        self.detail = ""
        self.display_name = "fake"

    @property
    def source(self):
        return "fake"


class _FakeRegistry:
    """Minimal stand-in for CapabilityRegistry used by routing tests."""

    def __init__(self, statuses: dict):
        # statuses maps registry-key → CapabilityStatus
        self._statuses = statuses

    def get_status(self, source: str):
        from agent.capability_registry import CapabilityStatus
        return self._statuses.get(source, CapabilityStatus.NOT_CONFIGURED)

    def get(self, source: str):
        if source in self._statuses:
            return _FakeCapability(self._statuses[source])
        return None


# — Possessive entity extraction ——————————————————————————————————————————

def test_extract_possessive_name():
    targets = _extract_entity_targets("Mike's latest email")
    assert "Mike" in targets


def test_extract_possessive_kinship():
    targets = _extract_entity_targets("reply to my mom's message")
    assert "mom" in [t.lower() for t in targets]


def test_extract_possessive_contraction_not_a_name():
    # "it's" and "that's" must NOT match the possessive pattern
    targets = _extract_entity_targets("it's urgent")
    assert targets == []


def test_possessive_name_routes_as_person_lookup(router):
    d = router.route("Mike's latest email")
    assert d.intent_type == IntentType.PERSON_LOOKUP
    assert "Mike" in d.entity_targets


# — Relative / event-relative time scopes —————————————————————————————————

@pytest.mark.parametrize("message,expected", [
    ("anything since Thursday?", "since_thursday"),
    ("show me anything since monday", "since_monday"),
    ("emails in the last 4 hours", "last_4_hour"),
    ("over the last 3 days", "last_3_day"),
    ("what do I have before my 3pm?", "before_3pm"),
    ("before my meeting", "before_meeting"),
    ("anything over the last few days?", "past_few_days"),
])
def test_relative_time_scopes(message, expected):
    assert _infer_time_scope(message) == expected


# — Short conversation carry-over ——————————————————————————————————————————

def test_carry_over_inherits_email(router):
    prior = ["Did anything important come in by email today?"]
    d = router.route("anything urgent?", recent_user_messages=prior)
    assert "email" in d.target_sources
    assert "carried over" in d.reasoning.lower()


def test_carry_over_does_not_fire_for_fresh_unrelated_query(router):
    prior = ["Did anything important come in by email today?"]
    d = router.route("how do I reverse a list in python?", recent_user_messages=prior)
    # Fresh unrelated question: no email carry-over
    assert "email" not in d.target_sources


def test_carry_over_does_not_overwrite_explicit_source(router):
    prior = ["any emails?"]
    d = router.route("any texts?", recent_user_messages=prior)
    # Explicit "texts" → imessage, not email
    assert "imessage" in d.target_sources
    assert "email" not in d.target_sources


# — Registry filtering (registry-aware routing) ——————————————————————————

def test_registry_narrows_to_available_sources(router):
    from agent.capability_registry import CapabilityStatus
    reg = _FakeRegistry({
        "email_gmail": CapabilityStatus.AVAILABLE,
        "email_yahoo": CapabilityStatus.NOT_CONFIGURED,
        "imessage": CapabilityStatus.PERMISSION_REQUIRED,
    })
    # Asking for texts when imessage needs permission AND email is available:
    # a single-source texts query should flag needs_clarification.
    d = router.route("any texts?", capability_registry=reg)
    assert d.needs_clarification is True
    # Original target preserved so UI can render the gap
    assert "imessage" in d.target_sources


def test_registry_keeps_reachable_sources(router):
    from agent.capability_registry import CapabilityStatus
    reg = _FakeRegistry({
        "email_gmail": CapabilityStatus.AVAILABLE,
        "slack": CapabilityStatus.AVAILABLE,
        "imessage": CapabilityStatus.PERMISSION_REQUIRED,
    })
    d = router.route("check my email and slack", capability_registry=reg)
    assert "email" in d.target_sources
    assert "slack" in d.target_sources
    assert d.needs_clarification is False


def test_registry_drops_unavailable_keeps_available(router):
    from agent.capability_registry import CapabilityStatus
    reg = _FakeRegistry({
        "email_gmail": CapabilityStatus.AVAILABLE,
        "slack": CapabilityStatus.NOT_CONFIGURED,
    })
    d = router.route("any updates from email or slack?", capability_registry=reg)
    # slack is dropped; email remains
    assert "email" in d.target_sources
    assert "slack" not in d.target_sources
    assert d.needs_clarification is False


# — Multi-intent split ——————————————————————————————————————————————————

def test_route_multi_single_intent_passthrough(router):
    decisions = router.route_multi("any emails today?")
    assert len(decisions) == 1
    assert decisions[0].intent_type in {
        IntentType.INBOX_SUMMARY, IntentType.CONVERSATION_LOOKUP,
    }


def test_route_multi_splits_clear_conjunction(router):
    decisions = router.route_multi("any emails and what's on my calendar?")
    assert len(decisions) == 2
    intents = {d.intent_type for d in decisions}
    assert IntentType.SCHEDULE_LOOKUP in intents


def test_route_multi_does_not_split_action_tail(router):
    # "check email and tell me what's urgent" — single compound intent, not a split
    decisions = router.route_multi("check email and tell me what's urgent")
    assert len(decisions) == 1


def test_route_multi_splits_on_semicolon(router):
    decisions = router.route_multi("any emails; what's on my calendar")
    assert len(decisions) == 2
