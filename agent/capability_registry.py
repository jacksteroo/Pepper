"""
Phase 6.3 — Explicit Capability Registry.

Tracks per-source availability at runtime so Pepper can answer capability
questions deterministically instead of relying on prompt folklore.

Status semantics:
  available               — source is configured and accessible right now
  not_configured          — credentials or setup not present
  permission_required     — configured but OS/file permission blocks access
  temporarily_unavailable — transient API/network error
  disabled                — explicitly turned off

CapabilityRegistry.populate(config) is called once at startup; individual
sources are refreshed via update_status() after live tool failures so the
registry reflects actual runtime state.
"""
from __future__ import annotations

import asyncio
import os
import structlog
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = structlog.get_logger()

# Shared config dir used by all subsystems (mirrors subsystems/google_auth.py)
_CONFIG_DIR = Path.home() / ".config" / "pepper"


class CapabilityStatus(str, Enum):
    AVAILABLE = "available"
    NOT_CONFIGURED = "not_configured"
    PERMISSION_REQUIRED = "permission_required"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    DISABLED = "disabled"


_STATUS_PHRASES = {
    CapabilityStatus.AVAILABLE: "available",
    CapabilityStatus.NOT_CONFIGURED: "not configured",
    CapabilityStatus.PERMISSION_REQUIRED: "needs a permission grant",
    CapabilityStatus.TEMPORARILY_UNAVAILABLE: "temporarily unavailable",
    CapabilityStatus.DISABLED: "disabled",
}

# Maps query_router source hints → registry keys
SOURCE_ALIASES: dict[str, list[str]] = {
    "email": ["email_gmail", "email_yahoo"],
    "gmail": ["email_gmail"],
    "yahoo": ["email_yahoo"],
    "imessage": ["imessage"],
    "text": ["imessage"],
    "texts": ["imessage"],
    "sms": ["imessage"],
    "whatsapp": ["whatsapp"],
    "slack": ["slack"],
    "calendar": ["calendar_google"],
    "schedule": ["calendar_google"],
    "memory": ["memory"],
    "images": ["web_search"],
    "web": ["web_search"],
    "search": ["web_search"],
    "filesystem": ["local_files"],
}


@dataclass
class SourceCapability:
    source: str
    display_name: str
    status: CapabilityStatus = CapabilityStatus.NOT_CONFIGURED
    detail: str = ""
    configured_accounts: list[str] = field(default_factory=list)


class CapabilityRegistry:
    """Runtime map of data source → availability status.

    Usage:
        registry = CapabilityRegistry()
        await registry.populate(config)

        registry.get_status("email_gmail")          # → CapabilityStatus.AVAILABLE
        registry.answer_capability_query("email")   # → "Yes, I can access Gmail (jack@…)."
        registry.update_status("imessage", CapabilityStatus.PERMISSION_REQUIRED, "FDA denied")
    """

    def __init__(self) -> None:
        self._sources: dict[str, SourceCapability] = {}

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _set(
        self,
        source: str,
        display: str,
        status: CapabilityStatus,
        detail: str = "",
        accounts: list[str] | None = None,
    ) -> None:
        self._sources[source] = SourceCapability(
            source=source,
            display_name=display,
            status=status,
            detail=detail,
            configured_accounts=accounts or [],
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def update_status(self, source: str, status: CapabilityStatus, detail: str = "") -> None:
        """Update a source after a live tool call (e.g. permission denied error)."""
        cap = self._sources.get(source)
        if cap:
            cap.status = status
            cap.detail = detail
            logger.info(
                "capability_status_updated",
                source=source,
                status=status.value,
                detail=detail[:120] if detail else "",
            )

    def classify_tool_error(self, tool_name: str, error: str) -> None:
        """Phase 6.6: map a tool-call error to a registry status update.

        Recognizable auth/permission patterns update the matching source so the
        next turn's prompt and router reflect the new reality — a permission
        revoked mid-session does not wait for a restart to propagate.
        """
        if not error:
            return
        err_lower = error.lower()

        # Map tool-name prefixes to registry source keys.
        tool_to_source: list[tuple[tuple[str, ...], str]] = [
            (("get_recent_imessages", "get_imessage_", "search_imessages"), "imessage"),
            (("get_recent_whatsapp", "get_whatsapp_", "search_whatsapp"), "whatsapp"),
            (("search_slack", "get_slack_", "list_slack_"), "slack"),
            (("get_upcoming_events", "get_calendar_", "list_calendars"), "calendar_google"),
            (("search_web", "search_images"), "web_search"),
        ]
        source_key: str | None = None
        for prefixes, key in tool_to_source:
            if any(tool_name.startswith(p) for p in prefixes):
                source_key = key
                break
        # Email tools span Gmail + Yahoo; update whichever the error names, or both
        if tool_name.startswith(("get_recent_emails", "search_emails", "get_email_")):
            if "gmail" in err_lower:
                source_key = "email_gmail"
            elif "yahoo" in err_lower:
                source_key = "email_yahoo"
            else:
                # Ambiguous — skip update to avoid flipping both to an error state
                # based on a vague message.
                return

        if not source_key or source_key not in self._sources:
            return

        permission_markers = (
            "permission", "full disk access", "fda", "forbidden",
            "operation not permitted", "access denied", "not authorized",
        )
        auth_markers = (
            "401", "unauthorized", "invalid_grant", "token expired",
            "credentials", "authentication",
        )
        transient_markers = (
            "timeout", "temporarily", "503", "502", "504", "connection",
            "rate limit", "too many requests", "429",
        )

        if any(m in err_lower for m in permission_markers):
            self.update_status(source_key, CapabilityStatus.PERMISSION_REQUIRED, error[:200])
        elif any(m in err_lower for m in auth_markers):
            self.update_status(source_key, CapabilityStatus.NOT_CONFIGURED, error[:200])
        elif any(m in err_lower for m in transient_markers):
            self.update_status(source_key, CapabilityStatus.TEMPORARILY_UNAVAILABLE, error[:200])
        # Unknown error patterns don't get mapped — avoid spurious state changes.

    async def refresh(self, config) -> None:
        """Re-probe all sources. Used by the periodic scheduler and on-demand retry.

        Thin wrapper around populate() so callers have a clear "refresh" verb.
        """
        logger.info("capability_registry_refresh_start")
        await self.populate(config)
        logger.info("capability_registry_refresh_complete")

    def get(self, source: str) -> SourceCapability | None:
        return self._sources.get(source)

    def get_status(self, source: str) -> CapabilityStatus:
        cap = self._sources.get(source)
        return cap.status if cap else CapabilityStatus.NOT_CONFIGURED

    def get_available_sources(self) -> list[str]:
        return [k for k, c in self._sources.items() if c.status == CapabilityStatus.AVAILABLE]

    def all_sources(self) -> dict[str, SourceCapability]:
        return dict(self._sources)

    def answer_capability_query(self, source_hint: str) -> str:
        """Return a precise user-facing statement for a source capability query.

        source_hint is a normalized hint such as "email", "imessage", "calendar".
        """
        keys = SOURCE_ALIASES.get(source_hint.lower(), [source_hint])
        lines: list[str] = []

        for key in keys:
            cap = self._sources.get(key)
            if not cap:
                continue
            if cap.status == CapabilityStatus.AVAILABLE:
                accounts = (
                    f" ({', '.join(cap.configured_accounts)})"
                    if cap.configured_accounts
                    else ""
                )
                lines.append(f"Yes, I can access {cap.display_name}{accounts}.")
            elif cap.status == CapabilityStatus.NOT_CONFIGURED:
                lines.append(
                    f"{cap.display_name} is not configured"
                    + (f": {cap.detail}" if cap.detail else " (no credentials set up)") + "."
                )
            elif cap.status == CapabilityStatus.PERMISSION_REQUIRED:
                lines.append(
                    f"{cap.display_name} needs a permission grant: "
                    + (cap.detail or "Full Disk Access required") + "."
                )
            elif cap.status == CapabilityStatus.TEMPORARILY_UNAVAILABLE:
                lines.append(
                    f"{cap.display_name} is temporarily unavailable: "
                    + (cap.detail or "API error") + "."
                )
            elif cap.status == CapabilityStatus.DISABLED:
                lines.append(f"{cap.display_name} is disabled.")

        if not lines:
            return f"I don't have '{source_hint}' registered as a capability."
        return " ".join(lines)

    def answer_generic_capability_query(self) -> str:
        """Return a full capability summary for generic 'what can you do?' queries."""
        available = []
        unavailable = []
        for cap in self._sources.values():
            if cap.status == CapabilityStatus.AVAILABLE:
                accounts = f" ({', '.join(cap.configured_accounts)})" if cap.configured_accounts else ""
                available.append(f"{cap.display_name}{accounts}")
            elif cap.status not in (CapabilityStatus.DISABLED,):
                phrase = _STATUS_PHRASES[cap.status]
                unavailable.append(f"{cap.display_name} ({phrase})")

        lines: list[str] = []
        if available:
            lines.append("I currently have access to: " + ", ".join(available) + ".")
        if unavailable:
            lines.append("Not yet available: " + ", ".join(unavailable) + ".")
        return " ".join(lines) if lines else "No capabilities are registered yet."

    def get_full_report(self) -> dict[str, dict]:
        """Structured report for API endpoints / web UI."""
        return {
            key: {
                "display_name": cap.display_name,
                "status": cap.status.value,
                "detail": cap.detail,
                "accounts": cap.configured_accounts,
            }
            for key, cap in self._sources.items()
        }

    # ── Startup population ─────────────────────────────────────────────────────

    async def populate(self, config) -> None:
        """Probe all sources and populate statuses.

        Called once at startup. Can be re-called after failures to refresh.
        """
        await asyncio.gather(
            self._check_gmail(config),
            self._check_yahoo(config),
            self._check_imessage(),
            self._check_whatsapp(),
            self._check_slack(),
            self._check_calendar(),
            self._check_web_search(config),
            self._check_memory(),
            self._check_local_filesystem(),
            return_exceptions=True,
        )
        logger.info(
            "capability_registry_populated",
            statuses={k: v.status.value for k, v in self._sources.items()},
            available_count=len(self.get_available_sources()),
        )

    async def _check_gmail(self, config) -> None:
        creds_path = _CONFIG_DIR / "google_credentials.json"
        # Token can be per-account (google_token_{account}.json) or shared
        token_paths = list(_CONFIG_DIR.glob("google_token*.json"))

        has_creds = creds_path.exists()
        has_token = bool(token_paths)

        if has_creds and has_token:
            # Extract account names from token file names
            accounts: list[str] = []
            for tp in token_paths:
                stem = tp.stem  # e.g. "google_token_jack" or "google_token"
                if "_" in stem[len("google_token"):]:
                    accounts.append(stem.split("google_token_", 1)[-1])
            self._set("email_gmail", "Gmail", CapabilityStatus.AVAILABLE,
                      accounts=accounts or ["default"])
        elif has_creds and not has_token:
            self._set("email_gmail", "Gmail", CapabilityStatus.NOT_CONFIGURED,
                      detail="Credentials present but no OAuth token — run setup_auth")
        else:
            self._set("email_gmail", "Gmail", CapabilityStatus.NOT_CONFIGURED,
                      detail="No Google credentials found at ~/.config/pepper/google_credentials.json")

    async def _check_yahoo(self, config) -> None:
        yahoo_creds = _CONFIG_DIR / "yahoo_credentials.json"
        legacy_creds = _CONFIG_DIR / "email_credentials.json"

        if yahoo_creds.exists() or legacy_creds.exists():
            import json
            try:
                path = yahoo_creds if yahoo_creds.exists() else legacy_creds
                data = json.loads(path.read_text())
                accounts = list(data.keys()) if isinstance(data, dict) else []
                self._set("email_yahoo", "Yahoo Mail", CapabilityStatus.AVAILABLE,
                          accounts=accounts)
            except Exception:
                self._set("email_yahoo", "Yahoo Mail", CapabilityStatus.AVAILABLE)
        else:
            self._set("email_yahoo", "Yahoo Mail", CapabilityStatus.NOT_CONFIGURED,
                      detail="No Yahoo credentials — run setup_auth")

    async def _check_imessage(self) -> None:
        from subsystems.communications.imessage_client import IMESSAGE_DB

        chat_db = IMESSAGE_DB
        if not chat_db.exists():
            self._set("imessage", "iMessage", CapabilityStatus.NOT_CONFIGURED,
                      detail=f"chat.db not found at {chat_db}")
            return
        if os.access(chat_db, os.R_OK):
            self._set("imessage", "iMessage", CapabilityStatus.AVAILABLE)
        else:
            self._set("imessage", "iMessage", CapabilityStatus.PERMISSION_REQUIRED,
                      detail="Full Disk Access not granted to this process")

    async def _check_whatsapp(self) -> None:
        from subsystems.communications.whatsapp_client import WHATSAPP_DB
        wa_db = WHATSAPP_DB
        export_dir = Path.home() / "Documents" / "WhatsApp Chats"

        if wa_db.exists():
            if os.access(wa_db, os.R_OK):
                self._set("whatsapp", "WhatsApp", CapabilityStatus.AVAILABLE)
            else:
                self._set("whatsapp", "WhatsApp", CapabilityStatus.PERMISSION_REQUIRED,
                          detail="Cannot read WhatsApp database — Full Disk Access may be needed")
        elif export_dir.is_dir():
            self._set("whatsapp", "WhatsApp", CapabilityStatus.AVAILABLE,
                      detail="Using exported chat files")
        else:
            self._set("whatsapp", "WhatsApp", CapabilityStatus.NOT_CONFIGURED,
                      detail="WhatsApp Desktop not installed or database not found")

    async def _check_slack(self) -> None:
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if token and token.startswith("xoxb-"):
            self._set("slack", "Slack", CapabilityStatus.AVAILABLE)
        elif token:
            self._set("slack", "Slack", CapabilityStatus.AVAILABLE,
                      detail="Token configured (format unverified)")
        else:
            self._set("slack", "Slack", CapabilityStatus.NOT_CONFIGURED,
                      detail="SLACK_BOT_TOKEN not set in environment")

    async def _check_calendar(self) -> None:
        creds_path = _CONFIG_DIR / "google_credentials.json"
        token_paths = list(_CONFIG_DIR.glob("google_token*.json"))

        # Calendar shares the same Google OAuth credentials as Gmail
        if creds_path.exists() and token_paths:
            self._set("calendar_google", "Google Calendar", CapabilityStatus.AVAILABLE)
        elif creds_path.exists():
            self._set("calendar_google", "Google Calendar", CapabilityStatus.NOT_CONFIGURED,
                      detail="Credentials present but no OAuth token — run setup_auth")
        else:
            self._set("calendar_google", "Google Calendar", CapabilityStatus.NOT_CONFIGURED,
                      detail="No Google credentials found")

    async def _check_web_search(self, config) -> None:
        api_key = getattr(config, "BRAVE_API_KEY", None) or os.environ.get("BRAVE_API_KEY")
        if api_key:
            self._set("web_search", "Web Search", CapabilityStatus.AVAILABLE)
        else:
            self._set("web_search", "Web Search", CapabilityStatus.NOT_CONFIGURED,
                      detail="BRAVE_API_KEY not set")

    async def _check_memory(self) -> None:
        # Memory is always available — runs in-process with no external deps
        self._set("memory", "Memory", CapabilityStatus.AVAILABLE)

    async def _check_local_filesystem(self) -> None:
        from agent.local_filesystem_tools import allowed_roots

        roots = [str(root) for root in allowed_roots()]
        if roots:
            self._set(
                "local_files",
                "Local Files",
                CapabilityStatus.AVAILABLE,
                detail=f"Read-only access within: {', '.join(roots)}",
            )
        else:
            self._set(
                "local_files",
                "Local Files",
                CapabilityStatus.NOT_CONFIGURED,
                detail="No readable local filesystem roots detected",
            )
