"""WhatsApp tool definitions and helpers for Pepper core.

Follows the same pattern as email_tools.py and imessage_tools.py.
"""

from __future__ import annotations

import asyncio
import re
import structlog
from agent.query_intents import (
    WHATSAPP_QUERY_TERMS,
    is_attention_request,
    is_source_query,
)

logger = structlog.get_logger()

WHATSAPP_TOOLS = [
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "get_recent_whatsapp_chats",
            "description": (
                "Fetch recent WhatsApp chats from the local WhatsApp Desktop database. "
                "Returns chat names, unread counts, and whether each chat is a group. "
                "Use when asked about WhatsApp messages, group chats, or family/friend groups."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max number of chats to return (default 15, max 30)",
                        "default": 15,
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
            "name": "get_whatsapp_chat",
            "description": (
                "Fetch messages from a specific WhatsApp chat by its chat ID. "
                "Use after get_recent_whatsapp_chats to drill into a specific conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "integer",
                        "description": "The chat ID from get_recent_whatsapp_chats",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to return (default 30)",
                        "default": 30,
                    },
                },
                "required": ["chat_id"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "search_whatsapp",
            "description": (
                "Search WhatsApp messages for a keyword or phrase. "
                "Use when asked to find something specific in WhatsApp history."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword or phrase to search in message text",
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
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "get_whatsapp_groups",
            "description": (
                "List all WhatsApp group chats with participant counts. "
                "Use when asked about group dynamics, family groups, or friend groups on WhatsApp."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]


def _get_client():
    from subsystems.communications.whatsapp_client import WhatsAppClient
    return WhatsAppClient()


async def execute_get_recent_whatsapp_chats(args: dict) -> dict:
    limit = min(int(args.get("limit", 15)), 30)
    try:
        client = _get_client()
        chats = await client.get_recent_chats(limit=limit)
        formatted = []
        for c in chats:
            group_tag = " [group]" if c["is_group"] else ""
            unread = f" [{c['unread_count']} unread]" if c["unread_count"] else ""
            formatted.append(
                f"{c['name']}{group_tag}{unread} — last: {c['last_message_at'] or 'unknown'}"
            )
        return {
            "chats": formatted,
            "count": len(chats),
            "raw": chats,
            "summary": f"{len(chats)} WhatsApp chat(s) found.",
        }
    except (PermissionError, FileNotFoundError) as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("whatsapp_chats_failed", error=str(e))
        return {"error": f"WhatsApp unavailable: {e}"}


async def execute_get_whatsapp_chat(args: dict) -> dict:
    chat_id = args.get("chat_id")
    if chat_id is None:
        return {"error": "chat_id is required"}
    limit = min(int(args.get("limit", 30)), 100)
    try:
        client = _get_client()
        messages = await client.get_chat_messages(int(chat_id), limit=limit)
        if not messages:
            return {"messages": [], "summary": f"No messages found in chat {chat_id}."}
        formatted = []
        for m in messages:
            sender = "You" if m["from_me"] else m["sender"]
            formatted.append(f"[{m['timestamp'] or '?'}] {sender}: {m['text']}")
        return {
            "messages": formatted,
            "count": len(messages),
            "chat_id": chat_id,
            "summary": f"{len(messages)} message(s) in chat {chat_id}.",
        }
    except (PermissionError, FileNotFoundError) as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("whatsapp_chat_failed", error=str(e))
        return {"error": f"WhatsApp unavailable: {e}"}


async def execute_search_whatsapp(args: dict) -> dict:
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
            "summary": f"Found {len(messages)} WhatsApp message(s) matching '{query}'.",
        }
    except (PermissionError, FileNotFoundError) as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("whatsapp_search_failed", error=str(e))
        return {"error": f"WhatsApp unavailable: {e}"}


async def execute_get_whatsapp_groups(args: dict) -> dict:
    try:
        client = _get_client()
        groups = await client.get_group_chats()
        formatted = []
        for g in groups:
            formatted.append(
                f"{g['name']} — {g['member_count']} members"
                f" (last: {g['last_message_at'] or 'unknown'})"
            )
        return {
            "groups": formatted,
            "count": len(groups),
            "raw": groups,
            "summary": f"{len(groups)} WhatsApp group(s) found.",
        }
    except (PermissionError, FileNotFoundError) as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("whatsapp_groups_failed", error=str(e))
        return {"error": f"WhatsApp unavailable: {e}"}


async def execute_whatsapp_tool(name: str, args: dict) -> dict:
    if name == "get_recent_whatsapp_chats":
        return await execute_get_recent_whatsapp_chats(args)
    elif name in ("get_whatsapp_chat", "get_whatsapp_messages"):
        return await execute_get_whatsapp_chat(args)
    elif name == "search_whatsapp":
        return await execute_search_whatsapp(args)
    elif name == "get_whatsapp_groups":
        return await execute_get_whatsapp_groups(args)
    return {"error": f"Unknown WhatsApp tool: {name}"}


_WHATSAPP_ATTENTION_TRIGGERS = (
    "who needs a reply",
)


def is_whatsapp_attention_query(user_message: str) -> bool:
    """Detect WhatsApp-summary questions that should bypass the LLM."""
    return is_attention_request(
        user_message,
        WHATSAPP_QUERY_TERMS,
        extra_terms=_WHATSAPP_ATTENTION_TRIGGERS,
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
    group_tag = " [group]" if item["is_group"] else ""
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
        f"{index}. {item['name']}{group_tag}{unread_tag} — "
        f'Last message: "{sender_prefix}{snippet}"{timing}. '
        f"Why: {item['why']}."
    )


async def execute_get_recent_whatsapp_attention(args: dict) -> dict:
    """Build a deterministic summary of recent WhatsApp chats worth attention."""
    limit = min(int(args.get("limit", 8)), 12)
    message_limit = min(int(args.get("message_limit", 3)), 5)
    try:
        client = _get_client()
        chats = await client.get_recent_chats(limit=limit)
        if not chats:
            return {
                "items": [],
                "count": 0,
                "summary": "I checked WhatsApp and there are no recent chats.",
            }

        message_batches = await asyncio.gather(
            *[client.get_chat_messages(c["chat_id"], limit=message_limit) for c in chats],
            return_exceptions=True,
        )

        unread_items: list[dict] = []
        recent_incoming_items: list[dict] = []

        for chat, batch in zip(chats, message_batches):
            messages = [] if isinstance(batch, Exception) else batch
            readable = [m for m in messages if (m.get("text") or "").strip()]
            latest_incoming = next((m for m in readable if not m["from_me"]), None)
            fallback = readable[0] if readable else None
            chosen = latest_incoming or fallback

            item = {
                "chat_id": chat["chat_id"],
                "name": chat["name"],
                "is_group": chat["is_group"],
                "unread_count": chat["unread_count"],
                "sender": (
                    "You"
                    if chosen and chosen["from_me"]
                    else (chosen.get("sender", "") if chosen else "")
                ),
                "timestamp": (
                    chosen.get("timestamp")
                    if chosen
                    else chat.get("last_message_at")
                ),
                "text": _clean_message_text(chosen.get("text", "")) if chosen else "",
                "why": (
                    "unread messages in this chat"
                    if chat["unread_count"]
                    else "one of your most recent incoming chats"
                ),
            }

            if chat["unread_count"] > 0:
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
                    "I checked your recent WhatsApp chats. I don't see any unread "
                    "conversations or readable recent incoming messages that stand out."
                ),
            }

        lines = [f"I found {len(items)} WhatsApp chat(s) worth your attention:"]
        for idx, item in enumerate(items, start=1):
            lines.append(_format_attention_line(idx, item))
        return {
            "items": items,
            "count": len(items),
            "summary": "\n".join(lines),
        }
    except (PermissionError, FileNotFoundError) as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("whatsapp_attention_failed", error=str(e))
        return {"error": f"WhatsApp unavailable: {e}"}


async def maybe_get_whatsapp_context(user_message: str) -> str:
    """Proactively inject recent WhatsApp chats + message snippets when the query is WhatsApp-related.

    Fetches chat list AND the last 3 messages from the top 5 most-recent chats so the
    model has real message text to work with. Without actual message content the model
    will hallucinate plausible-sounding messages.
    """
    if not is_source_query(user_message, WHATSAPP_QUERY_TERMS):
        return ""
    try:
        from subsystems.communications.whatsapp_client import WhatsAppClient
        if not WhatsAppClient.is_available():
            return "[WhatsApp] Database not found — WhatsApp Desktop may not be installed."
        client = WhatsAppClient()
        chats = await client.get_recent_chats(limit=15)
        if not chats:
            return "[WhatsApp] Database accessible but no chats found."

        total_unread = sum(c["unread_count"] for c in chats)
        lines = [f"[WhatsApp] {len(chats)} recent chat(s), {total_unread} unread:"]

        # Fetch last 3 messages from the 5 most-recent chats concurrently
        top_chats = chats[:5]
        message_batches = await asyncio.gather(
            *[client.get_chat_messages(c["chat_id"], limit=3) for c in top_chats],
            return_exceptions=True,
        )

        for c, msgs in zip(top_chats, message_batches):
            group_tag = " [group]" if c["is_group"] else ""
            unread_tag = f" [{c['unread_count']} unread]" if c["unread_count"] else ""
            lines.append(f"\n  {c['name']}{group_tag}{unread_tag}:")
            if isinstance(msgs, Exception) or not msgs:
                lines.append("    (no messages)")
            else:
                for m in reversed(msgs):  # oldest first
                    sender = "You" if m["from_me"] else m["sender"]
                    ts = (m["timestamp"] or "")[:16]  # trim to minute
                    text = (m["text"] or "").strip()
                    if text:
                        lines.append(f"    [{ts}] {sender}: {text}")

        # Remaining chats — names only, no message fetch
        for c in chats[5:10]:
            group_tag = " [group]" if c["is_group"] else ""
            unread_tag = f" [{c['unread_count']} unread]" if c["unread_count"] else ""
            last = c["last_message_at"] or "unknown"
            lines.append(f"  • {c['name']}{group_tag}{unread_tag} — last: {last}")

        logger.debug("whatsapp_context_injected", chats=len(chats), unread=total_unread)
        return "\n".join(lines)
    except Exception as e:
        logger.warning("whatsapp_proactive_failed", error=str(e))
        return f"[WhatsApp] Unavailable: {e}"
