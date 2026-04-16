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
        "search_images", "search_web", "get_driving_time",
    }

    block = build_capability_block()
    unknown = validate_prompt_tool_references(block, REGISTERED_TOOLS)
    assert unknown == [], (
        f"Capability block references nonexistent tools: {unknown}\n"
        "Update build_capability_block() to use the correct registered tool names."
    )
