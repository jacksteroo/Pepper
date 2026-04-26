from __future__ import annotations

import pytest

from agent import accounts, email_tools


def test_get_google_auth_account_uses_override(monkeypatch):
    monkeypatch.setattr(
        accounts,
        "get_email_accounts",
        lambda: [
            {"id": "business_name", "label": "Business Name", "type": "gmail", "google_account": "business"},
        ],
    )

    assert accounts.get_google_auth_account("business_name") == "business"
    assert accounts.get_google_auth_account("personal") == "personal"


def test_get_client_maps_configured_account_to_google_auth_account(monkeypatch):
    monkeypatch.setattr(email_tools, "_discover_gmail_accounts", lambda: ["personal", "work", "business"])
    monkeypatch.setattr(email_tools, "_imap_account_ids", lambda: ["yahoo"])
    monkeypatch.setattr(accounts, "get_google_auth_account", lambda account_id: "business" if account_id == "business_name" else account_id)

    captured: list[str] = []

    def fake_get_gmail_client(account_name: str):
        captured.append(account_name)
        return {"client": account_name}

    monkeypatch.setattr(email_tools, "_get_gmail_client", fake_get_gmail_client)

    client = email_tools._get_client("business_name")

    assert client == {"client": "business"}
    assert captured == ["business"]


@pytest.mark.asyncio
async def test_execute_get_recent_emails_preserves_configured_account_id(monkeypatch):
    monkeypatch.setattr(email_tools, "_all_accounts", lambda: ["business_name"])

    class FakeClient:
        def get_recent_messages(self, count: int, hours: int):
            return [
                {
                    "date": "Wed, 15 Apr 2026 08:00:00 +0000",
                    "from": "Sender <sender@example.com>",
                    "subject": "Test Subject",
                    "snippet": "Need your input",
                    "account": "business",
                    "unread": True,
                }
            ]

    monkeypatch.setattr(email_tools, "_get_client", lambda account_name: FakeClient())

    result = await email_tools.execute_get_recent_emails({"account": "all", "count": 5, "hours": 24})

    assert result["count"] == 1
    assert "Account: business_name" in result["emails"][0]


@pytest.mark.asyncio
async def test_execute_get_email_action_items_flags_actionable_messages(monkeypatch):
    actionable = {
        "date": "Wed, 15 Apr 2026 08:00:00 +0000",
        "from": "Boss <boss@example.com>",
        "subject": "Please review and reply today",
        "snippet": "Can you confirm the numbers by EOD?",
        "account": "work",
        "unread": True,
    }
    newsletter = {
        "date": "Wed, 15 Apr 2026 09:00:00 +0000",
        "from": "News <news@example.com>",
        "subject": "Weekly newsletter",
        "snippet": "unsubscribe here",
        "account": "personal",
        "unread": True,
    }

    async def fake_recent(args):
        return {
            "items": [actionable, newsletter],
            "count": 2,
            "emails": [],
            "summary": "2 email(s)",
        }

    monkeypatch.setattr(email_tools, "execute_get_recent_emails", fake_recent)

    result = await email_tools.execute_get_email_action_items({"account": "all"})

    assert result["count"] == 1
    assert result["action_items"][0]["account"] == "work"
    assert "requests review" in result["action_items"][0]["reasons"] or "asks you to do something" in result["action_items"][0]["reasons"]


@pytest.mark.asyncio
async def test_maybe_get_email_context_includes_action_items(monkeypatch):
    async def fake_unread_counts(args):
        return {"counts": {"personal": 2}, "total_unread": 2}

    async def fake_action_items(args):
        return {
            "action_items": [
                {"formatted": "[Personal] Please review — from Alex. Why: unread, requests review."}
            ],
            "count": 1,
        }

    monkeypatch.setattr(email_tools, "execute_get_email_unread_counts", fake_unread_counts)
    monkeypatch.setattr(email_tools, "execute_get_email_action_items", fake_action_items)

    context = await email_tools.maybe_get_email_context("any action items from my personal email?")

    assert "Email unread counts:" in context
    assert "Likely email action items" in context
    assert "Please review" in context


@pytest.mark.asyncio
async def test_maybe_get_email_context_includes_recent_account_scoped_emails(monkeypatch):
    async def fake_unread_counts(args):
        return {"counts": {"yahoo": 3}, "total_unread": 3}

    async def fake_recent(args):
        assert args["account"] == "yahoo"
        return {
            "items": [
                {
                    "from": "Sender <sender@example.com>",
                    "subject": "Security alert",
                    "unread": True,
                    "account": "yahoo",
                }
            ],
            "count": 1,
            "emails": [],
            "summary": "1 email(s)",
        }

    monkeypatch.setattr(email_tools, "execute_get_email_unread_counts", fake_unread_counts)
    monkeypatch.setattr(email_tools, "execute_get_recent_emails", fake_recent)

    context = await email_tools.maybe_get_email_context("anything from yahoo email?")

    assert "Email unread counts:" in context
    assert "Recent emails from Yahoo:" in context
    assert "Security alert [UNREAD]" in context


@pytest.mark.asyncio
async def test_maybe_get_email_context_account_scope_not_phrase_list_driven(monkeypatch):
    async def fake_unread_counts(args):
        return {"counts": {"yahoo": 3}, "total_unread": 3}

    async def fake_recent(args):
        assert args["account"] == "yahoo"
        return {
            "items": [
                {
                    "from": "Sender <sender@example.com>",
                    "subject": "Overnight update",
                    "unread": False,
                    "account": "yahoo",
                }
            ],
            "count": 1,
            "emails": [],
            "summary": "1 email(s)",
        }

    monkeypatch.setattr(email_tools, "execute_get_email_unread_counts", fake_unread_counts)
    monkeypatch.setattr(email_tools, "execute_get_recent_emails", fake_recent)

    context = await email_tools.maybe_get_email_context("what landed in yahoo overnight?")

    assert "Recent emails from Yahoo:" in context
    assert "Overnight update" in context


@pytest.mark.asyncio
async def test_maybe_get_email_context_includes_recent_summary_for_overnight_queries(monkeypatch):
    async def fake_unread_counts(args):
        return {"counts": {"personal": 5}, "total_unread": 5}

    async def fake_summary(args):
        assert args["hours"] == 12
        return {
            "important": [
                {
                    "formatted": "[Personal] Deadline moved up [UNREAD] — from Boss. Why: unread, marked urgent."
                }
            ],
            "emails": [],
            "count": 1,
            "hours": 12,
        }

    monkeypatch.setattr(email_tools, "execute_get_email_unread_counts", fake_unread_counts)
    monkeypatch.setattr(email_tools, "execute_get_email_summary", fake_summary)

    context = await email_tools.maybe_get_email_context(
        "summarize my emails received overnight. Anything important?"
    )

    assert "Email unread counts:" in context
    assert "Recent important emails:" in context
    assert "Deadline moved up" in context


@pytest.mark.asyncio
async def test_execute_get_email_summary_filters_recently_deprioritized_sender(monkeypatch):
    monkeypatch.setattr(email_tools, "_all_accounts", lambda: ["personal"])

    class FakeClient:
        def get_recent_messages(self, count: int, hours: int):
            return [
                {
                    "date": "Wed, 15 Apr 2026 08:00:00 +0000",
                    "from": "Moffett Field Golf Course <course@golffacility.com>",
                    "subject": "Driving Range Closure Starting Tomorrow!",
                    "snippet": "Schedule update",
                    "unread": True,
                },
                {
                    "date": "Wed, 15 Apr 2026 09:00:00 +0000",
                    "from": "Boss <boss@example.com>",
                    "subject": "Please review today",
                    "snippet": "Need your input",
                    "unread": True,
                },
            ]

    monkeypatch.setattr(email_tools, "_get_client", lambda account_name: FakeClient())

    result = await email_tools.execute_get_email_summary(
        {
            "account": "all",
            "count": 5,
            "hours": 24,
            "exclude_phrases": ["Moffett Fiels"],
        }
    )

    assert result["count"] == 1
    assert all("Moffett" not in item["formatted"] for item in result["emails"])
