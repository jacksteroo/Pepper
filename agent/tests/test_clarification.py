"""Tests for Phase 6.7 — clarifying-question path."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent.capability_registry import CapabilityRegistry, CapabilityStatus
from agent.query_router import RoutingDecision, IntentType, ActionMode


def _make_core():
    from agent.core import PepperCore
    config = MagicMock()
    config.LIFE_CONTEXT_PATH = "docs/LIFE_CONTEXT.md"
    config.OWNER_NAME = "Jack Chan"
    config.TIMEZONE = "UTC"
    config.DEFAULT_LOCAL_MODEL = "hermes-4.3-36b-tools:latest"
    config.DEFAULT_FRONTIER_MODEL = "local/hermes-4.3-36b-tools:latest"

    with patch("agent.core.ModelClient"), \
         patch("agent.core.MemoryManager"), \
         patch("agent.core.ToolRouter"):
        return PepperCore(config)


def test_clarification_all_sources_unavailable():
    core = _make_core()
    reg = CapabilityRegistry()
    reg._set("email_gmail", "Gmail", CapabilityStatus.NOT_CONFIGURED, detail="no creds")
    reg._set("email_yahoo", "Yahoo Mail", CapabilityStatus.NOT_CONFIGURED)
    reg._set("imessage", "iMessage", CapabilityStatus.PERMISSION_REQUIRED)
    core._capability_registry = reg

    decision = RoutingDecision(
        intent_type=IntentType.PERSON_LOOKUP,
        target_sources=["email", "imessage"],
        action_mode=ActionMode.CALL_TOOLS,
        needs_clarification=True,
        reasoning="all sources unavailable",
    )
    msg = core._format_clarification(decision)
    assert "can't reach" in msg.lower() or "can't" in msg
    # Should name at least one specific source's status
    assert "Gmail" in msg or "iMessage" in msg


def test_clarification_multiple_sources_need_choice():
    core = _make_core()
    reg = CapabilityRegistry()
    reg._set("email_gmail", "Gmail", CapabilityStatus.AVAILABLE, accounts=["j@x.com"])
    reg._set("imessage", "iMessage", CapabilityStatus.AVAILABLE)
    reg._set("whatsapp", "WhatsApp", CapabilityStatus.AVAILABLE)
    core._capability_registry = reg

    decision = RoutingDecision(
        intent_type=IntentType.PERSON_LOOKUP,
        target_sources=["email", "imessage", "whatsapp"],
        action_mode=ActionMode.CALL_TOOLS,
        needs_clarification=True,
        reasoning="ambiguous",
    )
    msg = core._format_clarification(decision)
    assert "email" in msg and "imessage" in msg
    assert "Which" in msg or "which" in msg


def test_clarification_no_sources_asks_channel():
    core = _make_core()
    core._capability_registry = CapabilityRegistry()
    decision = RoutingDecision(
        intent_type=IntentType.GENERAL_CHAT,
        target_sources=[],
        action_mode=ActionMode.CALL_TOOLS,
        needs_clarification=True,
        reasoning="no source hint",
    )
    msg = core._format_clarification(decision)
    assert "channel" in msg.lower() or "email" in msg.lower()


def test_clarification_status_phrase_covers_all_statuses():
    from agent.core import PepperCore
    for status in CapabilityStatus:
        phrase = PepperCore._status_phrase(status)
        assert isinstance(phrase, str) and len(phrase) > 0
