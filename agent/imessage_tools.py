"""iMessage tool definitions and helpers for Pepper core.

Follows the same pattern as email_tools.py:
  - IMESSAGE_TOOLS: Anthropic tool-schema list for the LLM
  - execute_imessage_tool: dispatcher called by PepperCore._execute_tool
  - maybe_get_imessage_context: proactive context injection
"""

from __future__ import annotations

import asyncio
import re

import structlog
from agent.query_intents import (
    IMESSAGE_QUERY_TERMS,
    is_attention_request,
    is_source_query,
)

logger = structlog.get_logger()

IMESSAGE_TOOLS = [
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "get_recent_imessages",
            "description": (
                "CALL THIS when the user asks about iMessages, text messages, SMS, or who texted them. "
                "Reads directly from the local Mac iMessage database (chat.db). "
                "Returns conversation list with names, unread counts, and last message time. "
                "Always attempt this tool — do not say you cannot read iMessages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max number of conversations to return (default 15, max 30)",
                        "default": 15,
                    },
                    "days": {
                        "type": "integer",
                        "description": "Look back this many days (default 30)",
                        "default": 30,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "get_imessage_conversation",
            "description": (
                "Fetch the message history with a specific contact or group chat from iMessage. "
                "Use when asked about a specific person's texts or a specific group chat."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "contact": {
                        "type": "string",
                        "description": "Contact name, phone number, or part of chat identifier to look up",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to return (default 30)",
                        "default": 30,
                    },
                },
                "required": ["contact"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "search_imessages",
            "description": (
                "Search iMessage history for messages containing a keyword or phrase. "
                "Use when asked to find a specific text or message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword or phrase to search for in message text",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 15)",
                        "default": 15,
                    },
                },
                "required": ["query"],
            },
        },
    },
]


def _get_client():
    from subsystems.communications.imessage_client import IMessageClient
    return IMessageClient()


async def execute_get_recent_imessages(args: dict) -> dict:
    limit = min(int(args.get("limit", 15)), 30)
    days = int(args.get("days", 30))
    try:
        client = _get_client()
        convos = await client.get_recent_conversations(limit=limit, days=days)
        formatted = []
        for c in convos:
            unread = f" [{c['unread_count']} unread]" if c["unread_count"] else ""
            formatted.append(
                f"{c['display_name']}{unread} — {c['message_count']} messages"
                f" (last: {c['last_message_at'] or 'unknown'})"
            )
        return {
            "conversations": formatted,
            "count": len(convos),
            "days": days,
            "summary": f"{len(convos)} active conversation(s) in the last {days} days.",
        }
    except PermissionError as e:
        return {"error": str(e)}
    except FileNotFoundError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("imessage_recent_failed", error=str(e))
        return {"error": f"iMessage unavailable: {e}"}


async def execute_get_imessage_conversation(args: dict) -> dict:
    contact = args.get("contact", "")
    limit = min(int(args.get("limit", 30)), 100)
    if not contact:
        return {"error": "contact is required"}
    try:
        client = _get_client()
        messages = await client.get_conversation(contact, limit=limit)
        if not messages:
            return {"messages": [], "summary": f"No messages found with '{contact}'."}
        formatted = []
        for m in messages:
            sender = "You" if m["from_me"] else m["sender"]
            formatted.append(f"[{m['timestamp'] or '?'}] {sender}: {m['text']}")
        return {
            "messages": formatted,
            "count": len(messages),
            "contact": contact,
            "summary": f"{len(messages)} message(s) with '{contact}'.",
        }
    except PermissionError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("imessage_conversation_failed", error=str(e))
        return {"error": f"iMessage unavailable: {e}"}


async def execute_search_imessages(args: dict) -> dict:
    query = args.get("query", "")
    limit = min(int(args.get("limit", 15)), 50)
    if not query:
        return {"error": "query is required"}
    try:
        client = _get_client()
        messages = await client.search_messages(query, limit=limit)
        formatted = []
        for m in messages:
            sender = "You" if m["from_me"] else m["sender"]
            formatted.append(
                f"[{m['timestamp'] or '?'}] {m['chat']} — {sender}: {m['text']}"
            )
        return {
            "messages": formatted,
            "count": len(messages),
            "query": query,
            "summary": f"Found {len(messages)} message(s) matching '{query}'.",
        }
    except PermissionError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("imessage_search_failed", error=str(e))
        return {"error": f"iMessage unavailable: {e}"}


async def execute_imessage_tool(name: str, args: dict) -> dict:
    if name == "get_recent_imessages":
        return await execute_get_recent_imessages(args)
    elif name == "get_imessage_conversation":
        return await execute_get_imessage_conversation(args)
    elif name == "search_imessages":
        return await execute_search_imessages(args)
    return {"error": f"Unknown iMessage tool: {name}"}


_IMESSAGE_ATTENTION_TRIGGERS = (
    "who needs a reply",
    "who texted",
    "any texts",
)


def is_imessage_attention_query(user_message: str) -> bool:
    """Detect iMessage-summary questions that should bypass the LLM."""
    return is_attention_request(
        user_message,
        IMESSAGE_QUERY_TERMS,
        extra_terms=_IMESSAGE_ATTENTION_TRIGGERS,
    )


def _clean_message_text(text: str, max_chars: int = 140) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"


def _short_timestamp(timestamp: str | None) -> str:
    if not timestamp:
        return ""
    return timestamp.replace("T", " ")[:16]


def _format_attention_line(index: int, item: dict) -> str:
    unread_tag = f" [{item['unread_count']} unread]" if item["unread_count"] else ""
    sender = item.get("sender", "")
    sender_prefix = (
        f"{sender}: "
        if sender and sender not in {"unknown", "me", "You"}
        else ""
    )
    snippet = item.get("text") or "Latest readable text unavailable."
    timestamp = _short_timestamp(item.get("timestamp"))
    timing = f" at {timestamp}" if timestamp else ""
    return (
        f"{index}. {item['display_name']}{unread_tag} — "
        f'Last message: "{sender_prefix}{snippet}"{timing}. '
        f"Why: {item['why']}."
    )


async def execute_get_recent_imessage_attention(args: dict) -> dict:
    """Build a deterministic summary of recent iMessage chats worth attention."""
    limit = min(int(args.get("limit", 8)), 12)
    days = int(args.get("days", 30))
    message_limit = min(int(args.get("message_limit", 3)), 5)
    try:
        client = _get_client()
        convos = await client.get_recent_conversations(limit=limit, days=days)
        if not convos:
            return {
                "items": [],
                "count": 0,
                "summary": f"I checked iMessage and found no active conversations in the last {days} days.",
            }

        message_batches = await asyncio.gather(
            *[client.get_chat_messages(c["chat_id"], limit=message_limit) for c in convos],
            return_exceptions=True,
        )

        unread_items: list[dict] = []
        recent_incoming_items: list[dict] = []

        for convo, batch in zip(convos, message_batches):
            messages = [] if isinstance(batch, Exception) else batch
            readable = [m for m in messages if (m.get("text") or "").strip()]
            latest_incoming = next((m for m in readable if not m["from_me"]), None)
            fallback = readable[0] if readable else None
            chosen = latest_incoming or fallback

            item = {
                "chat_id": convo["chat_id"],
                "display_name": convo["display_name"],
                "unread_count": convo["unread_count"],
                "sender": (
                    "You"
                    if chosen and chosen["from_me"]
                    else (chosen.get("sender", "") if chosen else "")
                ),
                "timestamp": chosen.get("timestamp") if chosen else convo.get("last_message_at"),
                "text": _clean_message_text(chosen.get("text", "")) if chosen else "",
                "why": (
                    "unread messages in this conversation"
                    if convo["unread_count"]
                    else "one of your most recent incoming conversations"
                ),
            }

            if convo["unread_count"] > 0:
                unread_items.append(item)
            elif latest_incoming:
                recent_incoming_items.append(item)

        items = unread_items[:5]
        if not items:
            items = recent_incoming_items[:3]

        if not items:
            return {
                "items": [],
                "count": 0,
                "summary": (
                    "I checked your recent iMessages. I don't see any unread conversations "
                    "or readable recent incoming texts that stand out."
                ),
            }

        lines = [f"I found {len(items)} iMessage conversation(s) worth your attention:"]
        for idx, item in enumerate(items, start=1):
            lines.append(_format_attention_line(idx, item))
        return {
            "items": items,
            "count": len(items),
            "summary": "\n".join(lines),
        }
    except PermissionError as e:
        return {"error": str(e)}
    except FileNotFoundError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("imessage_attention_failed", error=str(e))
        return {"error": f"iMessage unavailable: {e}"}


async def maybe_get_imessage_context(user_message: str) -> str:
    """Proactively inject recent iMessage snippets when the query is text-related."""
    if not is_source_query(user_message, IMESSAGE_QUERY_TERMS):
        return ""

    try:
        from subsystems.communications.imessage_client import IMessageClient
        if not IMessageClient.is_available():
            return "[iMessage] Database not found — iMessage may not be set up on this Mac."
        client = IMessageClient()
        convos = await client.get_recent_conversations(limit=15, days=30)
        if not convos:
            return "[iMessage] Database accessible but no recent conversations found."

        total_unread = sum(c["unread_count"] for c in convos)
        lines = [f"[iMessage] {len(convos)} recent conversation(s), {total_unread} unread:"]

        top_convos = convos[:5]
        message_batches = await asyncio.gather(
            *[client.get_chat_messages(c["chat_id"], limit=3) for c in top_convos],
            return_exceptions=True,
        )

        for convo, msgs in zip(top_convos, message_batches):
            unread_tag = f" [{convo['unread_count']} unread]" if convo["unread_count"] else ""
            lines.append(f"\n  {convo['display_name']}{unread_tag}:")
            if isinstance(msgs, Exception) or not msgs:
                lines.append("    (no messages)")
                continue
            for message in reversed(msgs):
                text = (message.get("text") or "").strip()
                if not text:
                    continue
                sender = "You" if message["from_me"] else message["sender"]
                ts = (message["timestamp"] or "")[:16]
                lines.append(f"    [{ts}] {sender}: {text}")

        for convo in convos[5:10]:
            unread_tag = f" [{convo['unread_count']} unread]" if convo["unread_count"] else ""
            last = convo["last_message_at"] or "unknown"
            lines.append(f"  • {convo['display_name']}{unread_tag} — last: {last}")

        logger.debug("imessage_context_injected", conversations=len(convos), unread=total_unread)
        return "\n".join(lines)
    except Exception as e:
        logger.warning("imessage_proactive_failed", error=str(e))
        return f"[iMessage] Unavailable: {e}"
