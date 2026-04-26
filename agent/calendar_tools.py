"""Calendar tool definitions and helpers for Pepper core.

Follows the same pattern as web_search.py / routing.py:
  - CALENDAR_TOOLS: Anthropic tool-schema list for the LLM
  - Helper functions called by PepperCore._execute_tool and _maybe_get_calendar_context
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from agent.query_intents import CALENDAR_QUERY_TERMS, infer_calendar_days, is_source_query

logger = structlog.get_logger()


def _event_sort_key(event: dict[str, Any]) -> str:
    start = event.get("start", {})
    return start.get("dateTime") or start.get("date") or ""

def _calendar_id_labels() -> dict[str, str]:
    from agent.accounts import get_calendar_id_labels
    return get_calendar_id_labels()


def _calendar_account_labels() -> dict[str, str]:
    from agent.accounts import get_calendar_account_labels
    return get_calendar_account_labels()

CALENDAR_TOOLS = [
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "get_upcoming_events",
            "description": (
                "Fetch upcoming calendar events across ALL of the user's Google accounts and calendars "
                "(personal, work, business name, partner company, shared, subscribed). "
                "Always call without calendar_filter unless the user explicitly asks for one specific calendar. "
                "Use when asked about schedule, meetings, what's coming up, or availability."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "How many days ahead to look (default 7, max 90)",
                        "default": 7,
                    },
                    "calendar_filter": {
                        "type": "string",
                        "description": (
                            "ONLY use when the user explicitly asks to narrow to ONE specific calendar "
                            "(e.g. 'show me only my work calendar'). "
                            "Do NOT pass this for default schedule queries — omitting it returns all calendars "
                            "across all accounts, which is always preferred."
                        ),
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
            "name": "get_calendar_events_range",
            "description": (
                "Fetch calendar events between two specific dates. Use this for any query "
                "about the past (e.g. 'what did I do last October', '18 months ago', "
                "'in Q3 2024') or for future ranges beyond 90 days. Accepts ISO date strings."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Start of the range, ISO 8601 date or datetime (e.g. '2024-10-01' or '2024-10-01T00:00:00')",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End of the range, ISO 8601 date or datetime (e.g. '2024-10-31' or '2024-10-31T23:59:59')",
                    },
                    "calendar_filter": {
                        "type": "string",
                        "description": (
                            "ONLY use when the user explicitly asks to narrow to ONE specific calendar. "
                            "Omit to query all calendars across all accounts (always preferred for default queries)."
                        ),
                    },
                },
                "required": ["start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "list_calendars",
            "description": "List all Google Calendars the user has access to.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]


def _get_client(account: str | None = None):
    """Lazy import to avoid import errors when Google libs aren't installed."""
    from subsystems.calendar.client import CalendarClient
    return CalendarClient(account=account)


def _get_all_clients():
    """Return (clients, skipped_warnings) for every authorized Google account."""
    from subsystems.calendar.auth import list_authorized_accounts
    from subsystems.calendar.client import CalendarClient
    accounts = list_authorized_accounts()
    clients = []
    skipped: list[str] = []
    for acc in accounts:
        account_arg = None if acc == "default" else acc
        try:
            clients.append((acc, CalendarClient(account=account_arg)))
        except Exception as e:
            logger.warning("calendar_account_skipped", account=acc, error=str(e))
            err_str = str(e)
            if "invalid_grant" in err_str:
                skipped.append(f"{acc}: token expired — re-run setup_auth to reconnect")
            else:
                skipped.append(f"{acc}: {e}")
    return clients, skipped


def _calendar_label(cal: dict[str, Any]) -> str:
    """Return a human-friendly label for a calendar."""
    cal_id = cal.get("id", "")
    return _calendar_id_labels().get(cal_id, cal.get("summary", cal_id))


def _format_event(event: dict[str, Any], calendars: list[dict[str, Any]]) -> str:
    start = event.get("start", {})
    dt_str = start.get("dateTime") or start.get("date") or ""
    try:
        if "T" in dt_str:
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is not None:
                dt = dt.astimezone()
            time_str = dt.strftime("%-I:%M %p %Z").strip() or dt.strftime("%-I:%M %p")
            date_str = dt.strftime("%a %b %-d")
        else:
            date_str = dt_str
            time_str = "all day"
    except ValueError:
        date_str = dt_str
        time_str = ""

    summary = event.get("summary", "(no title)")
    cal_id = event.get("_calendar_id", "")
    _id_labels = _calendar_id_labels()
    cal_label = _id_labels.get(cal_id, "")
    cal_name = next(
        (c.get("summary", "") for c in calendars if c.get("id") == cal_id), cal_label
    )
    location = event.get("location", "")
    attendees = event.get("attendees", [])
    attendee_names = [
        a.get("displayName") or a.get("email", "") for a in attendees if not a.get("self")
    ]

    parts = [f"{date_str} {time_str} — {summary}"]
    if cal_name:
        parts.append(f"  Calendar: {cal_name}")
    if location:
        parts.append(f"  Location: {location}")
    if attendee_names:
        parts.append(f"  With: {', '.join(attendee_names[:5])}")
    return "\n".join(parts)


async def execute_get_upcoming_events(args: dict) -> dict:
    days = min(int(args.get("days", 7)), 90)
    cal_filter = (args.get("calendar_filter") or "").lower()

    try:
        account_clients, skipped = await asyncio.to_thread(_get_all_clients)
        if not account_clients:
            msg = "No authorized Google accounts found. Run setup_auth.py first."
            if skipped:
                msg += " (" + "; ".join(skipped) + ")"
            return {"error": msg}

        all_events: list[dict[str, Any]] = []
        all_calendars: list[dict[str, Any]] = []

        for acc_name, client in account_clients:
            calendars = await asyncio.to_thread(client.list_calendars)
            # Tag each calendar with its account
            for cal in calendars:
                cal["_account"] = acc_name
            all_calendars.extend(calendars)

            if cal_filter:
                cal_ids = [
                    c["id"] for c in calendars
                    if cal_filter in c.get("summary", "").lower()
                    or cal_filter in _calendar_id_labels().get(c["id"], "").lower()
                    or cal_filter in acc_name.lower()
                ]
            else:
                cal_ids = None

            events = await asyncio.to_thread(client.list_upcoming_events, days, cal_ids)
            for e in events:
                e["_account"] = acc_name
            all_events.extend(events)

        all_events.sort(key=_event_sort_key)

        if not all_events:
            result: dict = {"events": [], "summary": f"No events in the next {days} days."}
            if skipped:
                result["warnings"] = skipped
            return result

        formatted = [_format_event(e, all_calendars) for e in all_events]
        result = {
            "events": formatted,
            "count": len(all_events),
            "days": days,
            "summary": f"{len(all_events)} event(s) in the next {days} days.",
        }
        if skipped:
            result["warnings"] = skipped
        return result
    except FileNotFoundError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("calendar_fetch_failed", error=str(e))
        return {"error": f"Calendar fetch failed: {e}"}


async def execute_get_calendar_events_range(args: dict) -> dict:
    start_str = args.get("start_date", "")
    end_str = args.get("end_date", "")
    cal_filter = (args.get("calendar_filter") or "").lower()

    if not start_str or not end_str:
        return {"error": "start_date and end_date are required."}

    try:
        # Parse dates — accept date-only (YYYY-MM-DD) or full datetime strings
        def _parse(s: str) -> datetime:
            s = s.strip()
            if len(s) == 10:  # date-only
                return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        start = _parse(start_str)
        end = _parse(end_str)
        # If end is date-only, include the full day
        if len(end_str.strip()) == 10:
            end = end.replace(hour=23, minute=59, second=59)
    except ValueError as e:
        return {"error": f"Invalid date format: {e}. Use ISO 8601 (e.g. '2024-10-01')."}

    try:
        account_clients, skipped = await asyncio.to_thread(_get_all_clients)
        if not account_clients:
            msg = "No authorized Google accounts found. Run setup_auth.py first."
            if skipped:
                msg += " (" + "; ".join(skipped) + ")"
            return {"error": msg}

        all_events: list[dict[str, Any]] = []
        all_calendars: list[dict[str, Any]] = []

        for acc_name, client in account_clients:
            calendars = await asyncio.to_thread(client.list_calendars)
            for cal in calendars:
                cal["_account"] = acc_name
            all_calendars.extend(calendars)

            if cal_filter:
                cal_ids = [
                    c["id"] for c in calendars
                    if cal_filter in c.get("summary", "").lower()
                    or cal_filter in _calendar_id_labels().get(c["id"], "").lower()
                    or cal_filter in acc_name.lower()
                ]
            else:
                cal_ids = None

            events = await asyncio.to_thread(client.list_events_range, start, end, cal_ids)
            for e in events:
                e["_account"] = acc_name
            all_events.extend(events)

        all_events.sort(key=_event_sort_key)

        if not all_events:
            result: dict = {
                "events": [],
                "summary": f"No events found between {start_str} and {end_str}.",
            }
            if skipped:
                result["warnings"] = skipped
            return result

        formatted = [_format_event(e, all_calendars) for e in all_events]
        result = {
            "events": formatted,
            "count": len(all_events),
            "start_date": start_str,
            "end_date": end_str,
            "summary": f"{len(all_events)} event(s) between {start_str} and {end_str}.",
        }
        if skipped:
            result["warnings"] = skipped
        return result
    except FileNotFoundError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("calendar_range_fetch_failed", error=str(e))
        return {"error": f"Calendar range fetch failed: {e}"}


async def execute_list_calendars() -> dict:
    try:
        account_clients, skipped = await asyncio.to_thread(_get_all_clients)
        result = []
        for acc_name, client in account_clients:
            calendars = await asyncio.to_thread(client.list_calendars)
            for cal in calendars:
                entry = {
                    "name": cal.get("summary", ""),
                    "id": cal.get("id", ""),
                    "access": cal.get("accessRole", ""),
                    "primary": cal.get("primary", False),
                    "account": acc_name,
                }
                _id_labels = _calendar_id_labels()
                if cal["id"] in _id_labels:
                    entry["label"] = _id_labels[cal["id"]]
                result.append(entry)
        out: dict = {"calendars": result, "count": len(result)}
        if skipped:
            out["warnings"] = skipped
        return out
    except Exception as e:
        logger.error("list_calendars_failed", error=str(e))
        return {"error": f"Could not list calendars: {e}"}


async def _maybe_get_calendar_context(
    user_message: str,
    *,
    timezone_name: str | None,
) -> str:
    """Proactively inject upcoming events when the query is schedule-related."""
    if not is_source_query(user_message, CALENDAR_QUERY_TERMS, extra_terms=("today", "tomorrow", "this week", "next week", "coming up")):
        return ""

    # Determine look-ahead window from message
    days = infer_calendar_days(user_message, default=7)

    try:
        normalized = user_message.lower()
        tz = ZoneInfo(timezone_name) if timezone_name else datetime.now().astimezone().tzinfo
        now_local = datetime.now(tz)
        if "today" in normalized or "tonight" in normalized:
            start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1) - timedelta(seconds=1)
            result = await execute_get_calendar_events_range(
                {"start_date": start.isoformat(), "end_date": end.isoformat()}
            )
            heading = "Calendar events for today:"
        elif "tomorrow" in normalized:
            start = (now_local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1) - timedelta(seconds=1)
            result = await execute_get_calendar_events_range(
                {"start_date": start.isoformat(), "end_date": end.isoformat()}
            )
            heading = "Calendar events for tomorrow:"
        else:
            start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=days) - timedelta(seconds=1)
            result = await execute_get_calendar_events_range(
                {"start_date": start.isoformat(), "end_date": end.isoformat()}
            )
            heading = f"Calendar events for the next {days} day(s), including today:"
        if "error" in result or not result.get("events"):
            return ""
        lines = [heading]
        lines.extend(result["events"])
        logger.debug("calendar_context_injected", count=result["count"])
        return "\n".join(lines)
    except Exception as e:
        logger.warning("calendar_proactive_failed", error=str(e))
        return ""


async def maybe_get_calendar_context(
    user_message: str,
    *,
    timezone_name: str | None = None,
) -> str:
    return await _maybe_get_calendar_context(
        user_message,
        timezone_name=timezone_name,
    )
