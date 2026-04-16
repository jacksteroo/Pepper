"""Slack tool definitions and helpers for Pepper core.

Follows the same pattern as email_tools.py and imessage_tools.py.

Requires SLACK_BOT_TOKEN in .env.
"""

from __future__ import annotations

import asyncio
import os

import structlog
from agent.query_intents import SLACK_QUERY_TERMS, is_source_query

logger = structlog.get_logger()

SLACK_TOOLS = [
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "search_slack",
            "description": (
                "Search Slack messages for a keyword, topic, or person. "
                "Use when asked about Slack conversations, work discussions, or specific topics. "
                "Supports natural language queries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (keyword, person name, topic, etc.)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "get_slack_channel_messages",
            "description": (
                "Fetch recent messages from a Slack channel. "
                "Use when asked about a specific channel's conversations or activity."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "The Slack channel ID (e.g. C01234ABCDE)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to return (default 20)",
                        "default": 20,
                    },
                    "days": {
                        "type": "integer",
                        "description": "Look back this many days (default 7)",
                        "default": 7,
                    },
                },
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "get_slack_deadlines",
            "description": (
                "Scan recent Slack messages in a channel for deadline language "
                "('due Friday', 'by EOD', 'need this by', etc.). "
                "Use when asked about upcoming deadlines or work commitments from Slack."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "The Slack channel ID to scan",
                    },
                    "days": {
                        "type": "integer",
                        "description": "How many days back to scan (default 14)",
                        "default": 14,
                    },
                },
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "list_slack_channels",
            "description": (
                "List available Slack channels with their member counts and topics. "
                "Use to discover channel IDs for other Slack tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_private": {
                        "type": "boolean",
                        "description": "Include private channels (default false)",
                        "default": False,
                    },
                },
                "required": [],
            },
        },
    },
]


def _get_token() -> str:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise ValueError(
            "SLACK_BOT_TOKEN not configured. "
            "Add it to .env — see .env.example for setup instructions."
        )
    return token


def _get_client():
    from subsystems.communications.slack_client import SlackClient
    return SlackClient(_get_token())


async def execute_search_slack(args: dict) -> dict:
    query = args.get("query", "")
    limit = min(int(args.get("limit", 10)), 50)
    if not query:
        return {"error": "query is required"}
    try:
        client = _get_client()
        results = await asyncio.to_thread(client.search_messages, query, limit)
        formatted = []
        for m in results:
            formatted.append(
                f"[{m['timestamp'] or '?'}] #{m['channel']} — {m['sender']}: {m['text']}"
            )
        return {
            "messages": formatted,
            "count": len(results),
            "query": query,
            "summary": f"Found {len(results)} Slack message(s) matching '{query}'.",
        }
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("slack_search_failed", error=str(e))
        return {"error": f"Slack search failed: {e}"}


async def execute_get_slack_channel_messages(args: dict) -> dict:
    channel_id = args.get("channel_id", "")
    limit = min(int(args.get("limit", 20)), 100)
    days = int(args.get("days", 7))
    if not channel_id:
        return {"error": "channel_id is required"}
    try:
        client = _get_client()
        messages = await asyncio.to_thread(client.get_channel_messages, channel_id, limit, days)
        formatted = []
        for m in messages:
            formatted.append(
                f"[{m['timestamp'] or '?'}] {m['sender']}: {m['text']}"
            )
        return {
            "messages": formatted,
            "count": len(messages),
            "channel_id": channel_id,
            "days": days,
            "summary": f"{len(messages)} message(s) in the last {days} days.",
        }
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("slack_channel_failed", error=str(e))
        return {"error": f"Slack channel fetch failed: {e}"}


async def execute_get_slack_deadlines(args: dict) -> dict:
    channel_id = args.get("channel_id", "")
    days = int(args.get("days", 14))
    if not channel_id:
        return {"error": "channel_id is required"}
    try:
        from subsystems.communications.slack_client import detect_deadlines
        client = _get_client()
        messages = await asyncio.to_thread(client.get_channel_messages, channel_id, 200, days)
        deadline_msgs = detect_deadlines(messages)
        formatted = []
        for m in deadline_msgs:
            hints = ", ".join(m.get("deadline_hints", []))
            formatted.append(
                f"[{m['timestamp'] or '?'}] {m['sender']}: {m['text'][:150]}"
                f" [deadline: {hints}]"
            )
        return {
            "deadlines": formatted,
            "count": len(deadline_msgs),
            "channel_id": channel_id,
            "days_scanned": days,
            "summary": (
                f"Found {len(deadline_msgs)} message(s) with deadline language "
                f"in the last {days} days."
            ),
        }
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("slack_deadlines_failed", error=str(e))
        return {"error": f"Slack deadline scan failed: {e}"}


async def execute_list_slack_channels(args: dict) -> dict:
    include_private = bool(args.get("include_private", False))
    try:
        client = _get_client()
        channels = await asyncio.to_thread(client.list_channels, include_private)
        formatted = []
        for c in channels:
            private_tag = " [private]" if c["is_private"] else ""
            formatted.append(
                f"#{c['name']}{private_tag} (id: {c['id']}, members: {c['member_count']})"
                + (f" — {c['topic']}" if c["topic"] else "")
            )
        return {
            "channels": formatted,
            "count": len(channels),
            "summary": f"{len(channels)} Slack channel(s) available.",
        }
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("slack_list_channels_failed", error=str(e))
        return {"error": f"Slack channel list failed: {e}"}


async def execute_slack_tool(name: str, args: dict) -> dict:
    if name == "search_slack":
        return await execute_search_slack(args)
    elif name == "get_slack_channel_messages":
        return await execute_get_slack_channel_messages(args)
    elif name == "get_slack_deadlines":
        return await execute_get_slack_deadlines(args)
    elif name == "list_slack_channels":
        return await execute_list_slack_channels(args)
    return {"error": f"Unknown Slack tool: {name}"}


async def maybe_get_slack_context(user_message: str) -> str:
    """Proactively note Slack availability when deadline/work keywords appear."""
    if not is_source_query(user_message, SLACK_QUERY_TERMS):
        return ""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return ""
    # Don't make a full API call for proactive context — just signal availability
    logger.debug("slack_context_hinted")
    return "Slack integration is available — use list_slack_channels to find channel IDs."
