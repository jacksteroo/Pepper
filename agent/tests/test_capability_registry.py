"""Tests for Phase 6.3 — CapabilityRegistry."""
from __future__ import annotations

import json
import os
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agent.capability_registry import (
    CapabilityRegistry,
    CapabilityStatus,
    SOURCE_ALIASES,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def registry():
    return CapabilityRegistry()


@pytest.fixture
def populated_registry():
    reg = CapabilityRegistry()
    reg._set("email_gmail", "Gmail", CapabilityStatus.AVAILABLE, accounts=["jack@example.com"])
    reg._set("email_yahoo", "Yahoo Mail", CapabilityStatus.NOT_CONFIGURED, detail="No credentials")
    reg._set("imessage", "iMessage", CapabilityStatus.AVAILABLE)
    reg._set("whatsapp", "WhatsApp", CapabilityStatus.PERMISSION_REQUIRED, detail="Full Disk Access required")
    reg._set("slack", "Slack", CapabilityStatus.AVAILABLE)
    reg._set("calendar_google", "Google Calendar", CapabilityStatus.AVAILABLE)
    reg._set("memory", "Memory", CapabilityStatus.AVAILABLE)
    reg._set("web_search", "Web Search", CapabilityStatus.NOT_CONFIGURED)
    return reg


# ── CapabilityStatus unit tests ────────────────────────────────────────────────

def test_status_enum_values():
    assert CapabilityStatus.AVAILABLE == "available"
    assert CapabilityStatus.NOT_CONFIGURED == "not_configured"
    assert CapabilityStatus.PERMISSION_REQUIRED == "permission_required"
    assert CapabilityStatus.TEMPORARILY_UNAVAILABLE == "temporarily_unavailable"
    assert CapabilityStatus.DISABLED == "disabled"


# ── Registry basic ops ─────────────────────────────────────────────────────────

def test_set_and_get(registry):
    registry._set("imessage", "iMessage", CapabilityStatus.AVAILABLE)
    cap = registry.get("imessage")
    assert cap is not None
    assert cap.display_name == "iMessage"
    assert cap.status == CapabilityStatus.AVAILABLE


def test_get_nonexistent_returns_none(registry):
    assert registry.get("nonexistent_source") is None


def test_get_status_unknown_key(registry):
    assert registry.get_status("nonexistent") == CapabilityStatus.NOT_CONFIGURED


def test_get_available_sources(populated_registry):
    available = populated_registry.get_available_sources()
    assert "email_gmail" in available
    assert "imessage" in available
    assert "slack" in available
    assert "calendar_google" in available
    assert "email_yahoo" not in available
    assert "whatsapp" not in available


def test_update_status(populated_registry):
    populated_registry.update_status("whatsapp", CapabilityStatus.AVAILABLE, "")
    assert populated_registry.get_status("whatsapp") == CapabilityStatus.AVAILABLE


def test_update_status_nonexistent_source_does_not_raise(registry):
    registry.update_status("does_not_exist", CapabilityStatus.DISABLED, "")


def test_all_sources_returns_copy(registry):
    registry._set("imessage", "iMessage", CapabilityStatus.AVAILABLE)
    sources = registry.all_sources()
    assert "imessage" in sources
    # Mutating the returned dict should not affect the registry
    sources["imessage"] = None
    assert registry.get("imessage") is not None


# ── Source-specific capability answers ────────────────────────────────────────

def test_answer_available_source(populated_registry):
    answer = populated_registry.answer_capability_query("gmail")
    assert "Yes" in answer
    assert "Gmail" in answer
    assert "jack@example.com" in answer


def test_answer_not_configured_source(populated_registry):
    answer = populated_registry.answer_capability_query("yahoo")
    assert "not configured" in answer.lower()


def test_answer_permission_required(populated_registry):
    answer = populated_registry.answer_capability_query("whatsapp")
    assert "permission" in answer.lower() or "Full Disk Access" in answer


def test_answer_email_alias_covers_both_gmail_and_yahoo(populated_registry):
    answer = populated_registry.answer_capability_query("email")
    assert "Gmail" in answer
    assert "Yahoo" in answer


def test_answer_unknown_source_hint(populated_registry):
    answer = populated_registry.answer_capability_query("notion")
    assert "notion" in answer.lower() or "registered" in answer.lower()


def test_answer_temporarily_unavailable(registry):
    registry._set("slack", "Slack", CapabilityStatus.TEMPORARILY_UNAVAILABLE, detail="503 error")
    answer = registry.answer_capability_query("slack")
    assert "temporarily unavailable" in answer.lower()
    assert "503 error" in answer


def test_answer_disabled(registry):
    registry._set("slack", "Slack", CapabilityStatus.DISABLED)
    answer = registry.answer_capability_query("slack")
    assert "disabled" in answer.lower()


# ── Generic capability answer ──────────────────────────────────────────────────

def test_generic_capability_lists_available_sources(populated_registry):
    answer = populated_registry.answer_generic_capability_query()
    assert "Gmail" in answer
    assert "iMessage" in answer
    assert "Slack" in answer
    assert "Google Calendar" in answer


def test_generic_capability_lists_unavailable_sources(populated_registry):
    answer = populated_registry.answer_generic_capability_query()
    assert "Yahoo Mail" in answer or "Web Search" in answer


def test_generic_capability_empty_registry():
    reg = CapabilityRegistry()
    answer = reg.answer_generic_capability_query()
    assert "No capabilities" in answer


# ── get_full_report ────────────────────────────────────────────────────────────

def test_get_full_report_structure(populated_registry):
    report = populated_registry.get_full_report()
    assert "email_gmail" in report
    entry = report["email_gmail"]
    assert entry["status"] == "available"
    assert "display_name" in entry
    assert "accounts" in entry


# ── SOURCE_ALIASES coverage ────────────────────────────────────────────────────

def test_source_aliases_cover_common_terms():
    for alias in ("email", "gmail", "yahoo", "imessage", "text", "whatsapp", "slack", "calendar", "filesystem"):
        assert alias in SOURCE_ALIASES, f"Missing alias: {alias}"


# ── populate() with mocked filesystem ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_populate_gmail_not_configured(tmp_path):
    config = MagicMock()
    config.BRAVE_API_KEY = None
    reg = CapabilityRegistry()
    # Point the config dir at tmp (no google_credentials.json there)
    with patch("agent.capability_registry._CONFIG_DIR", tmp_path):
        await reg.populate(config)
    assert reg.get_status("email_gmail") == CapabilityStatus.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_populate_gmail_configured_with_token(tmp_path):
    (tmp_path / "google_credentials.json").write_text("{}")
    (tmp_path / "google_token.json").write_text("{}")
    config = MagicMock()
    config.BRAVE_API_KEY = None
    reg = CapabilityRegistry()
    with patch("agent.capability_registry._CONFIG_DIR", tmp_path):
        await reg.populate(config)
    assert reg.get_status("email_gmail") == CapabilityStatus.AVAILABLE


@pytest.mark.asyncio
async def test_populate_yahoo_configured(tmp_path):
    creds = {"jack@yahoo.com": {"password": "xxx"}}
    (tmp_path / "yahoo_credentials.json").write_text(json.dumps(creds))
    config = MagicMock()
    config.BRAVE_API_KEY = None
    reg = CapabilityRegistry()
    with patch("agent.capability_registry._CONFIG_DIR", tmp_path):
        await reg.populate(config)
    assert reg.get_status("email_yahoo") == CapabilityStatus.AVAILABLE
    cap = reg.get("email_yahoo")
    assert "jack@yahoo.com" in cap.configured_accounts


@pytest.mark.asyncio
async def test_check_imessage_db_not_found():
    """When chat.db doesn't exist, status is not_configured."""
    reg = CapabilityRegistry()
    with patch.object(Path, "exists", return_value=False):
        await reg._check_imessage()
    assert reg.get_status("imessage") == CapabilityStatus.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_check_imessage_permission_denied():
    """When chat.db exists but is unreadable, status is permission_required."""
    reg = CapabilityRegistry()
    with patch.object(Path, "exists", return_value=True), \
         patch("os.access", return_value=False):
        await reg._check_imessage()
    assert reg.get_status("imessage") == CapabilityStatus.PERMISSION_REQUIRED


@pytest.mark.asyncio
async def test_check_imessage_uses_shared_client_path(tmp_path):
    """Registry should reuse the same resolved DB path as the iMessage client."""
    reg = CapabilityRegistry()
    db_path = tmp_path / "chat.db"
    db_path.write_text("stub")
    with patch("subsystems.communications.imessage_client.IMESSAGE_DB", db_path):
        await reg._check_imessage()
    assert reg.get_status("imessage") == CapabilityStatus.AVAILABLE


@pytest.mark.asyncio
async def test_populate_slack_configured():
    config = MagicMock()
    reg = CapabilityRegistry()
    with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test-token"}):
        await reg._check_slack()
    assert reg.get_status("slack") == CapabilityStatus.AVAILABLE


@pytest.mark.asyncio
async def test_populate_slack_not_configured():
    config = MagicMock()
    reg = CapabilityRegistry()
    with patch.dict(os.environ, {"SLACK_BOT_TOKEN": ""}, clear=False):
        # Remove SLACK_BOT_TOKEN if present
        env = {k: v for k, v in os.environ.items() if k != "SLACK_BOT_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            await reg._check_slack()
    assert reg.get_status("slack") == CapabilityStatus.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_populate_memory_always_available():
    config = MagicMock()
    config.BRAVE_API_KEY = None
    reg = CapabilityRegistry()
    await reg._check_memory()
    assert reg.get_status("memory") == CapabilityStatus.AVAILABLE


@pytest.mark.asyncio
async def test_populate_local_files_always_available():
    reg = CapabilityRegistry()
    await reg._check_local_filesystem()
    assert reg.get_status("local_files") == CapabilityStatus.AVAILABLE


@pytest.mark.asyncio
async def test_populate_web_search_configured():
    config = MagicMock()
    config.BRAVE_API_KEY = "test-key"
    reg = CapabilityRegistry()
    await reg._check_web_search(config)
    assert reg.get_status("web_search") == CapabilityStatus.AVAILABLE


@pytest.mark.asyncio
async def test_populate_web_search_not_configured():
    config = MagicMock()
    config.BRAVE_API_KEY = None
    reg = CapabilityRegistry()
    with patch.dict(os.environ, {}, clear=False):
        env_no_brave = {k: v for k, v in os.environ.items() if k != "BRAVE_API_KEY"}
        with patch.dict(os.environ, env_no_brave, clear=True):
            await reg._check_web_search(config)
    assert reg.get_status("web_search") == CapabilityStatus.NOT_CONFIGURED
