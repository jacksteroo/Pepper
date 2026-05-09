from __future__ import annotations

import re
import structlog
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.capability_registry import CapabilityRegistry

logger = structlog.get_logger()

# Repo root = parent of the directory this file lives in (agent/ → Pepper/)
_REPO_ROOT = Path(__file__).parent.parent


def load_soul(path: str = "docs/SOUL.md") -> str:
    """Read SOUL.md and return its full content.

    Relative paths are resolved against the repo root.
    """
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = _REPO_ROOT / file_path
    if not file_path.exists():
        logger.warning("soul_not_found", path=str(file_path))
        return ""
    return file_path.read_text(encoding="utf-8")


def load_life_context(path: str) -> str:
    """Read the LIFE_CONTEXT.md file and return its full content.

    Relative paths are resolved against the repo root so the file is found
    regardless of the process working directory.
    """
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = _REPO_ROOT / file_path
    if not file_path.exists():
        logger.warning("life_context_not_found", path=str(file_path))
        return ""
    content = file_path.read_text(encoding="utf-8")
    if not content.strip():
        logger.warning("life_context_empty", path=str(file_path))
    return content


def get_life_context_sections(path: str = None) -> dict[str, str]:
    """Parse markdown ## headings as section keys, content as values."""
    resolved_path = path or "data/life_context.md"
    content = load_life_context(resolved_path)
    if not content:
        return {}

    sections: dict[str, str] = {}
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in content.splitlines():
        heading_match = re.match(r"^##\s+(.+)$", line)
        if heading_match:
            if current_heading is not None:
                sections[current_heading] = "\n".join(current_lines).strip()
            current_heading = heading_match.group(1).strip()
            current_lines = []
        else:
            if current_heading is not None:
                current_lines.append(line)

    # Flush the last section
    if current_heading is not None:
        sections[current_heading] = "\n".join(current_lines).strip()

    return sections


def get_owner_name(path: str = None, config=None) -> str:
    """Resolve the owner's name from config first, then life context."""
    owner_name = getattr(config, "OWNER_NAME", None)
    if isinstance(owner_name, str):
        cleaned = owner_name.strip()
        if cleaned and cleaned.lower() != "the owner":
            return cleaned

    resolved_path = path or "data/life_context.md"
    sections = get_life_context_sections(resolved_path)
    identity = sections.get("Identity", "")

    match = re.search(r"\*\*Name:\*\*\s*(.+)", identity)
    if match:
        return match.group(1).strip()

    content = load_life_context(resolved_path)
    for pattern in (
        r"The person you are speaking with is (.+?)\s+[—-]",
        r"The human messaging you is (.+?)\s+[—-]",
    ):
        match = re.search(pattern, content)
        if match:
            return match.group(1).strip()

    return "your owner"


async def update_life_context(
    section: str, content: str, db_session, path: str = None
) -> None:
    """Update a section in the file and save a LifeContextVersion record to DB.

    Finds the ## heading that matches `section` (case-insensitive partial match),
    replaces its content until the next ## heading, writes the file back, and
    appends a LifeContextVersion row to the database.
    """
    from agent.models import LifeContextVersion

    resolved_path = path or "data/life_context.md"
    file_path = Path(resolved_path)
    original = file_path.read_text(encoding="utf-8") if file_path.exists() else ""

    lines = original.splitlines(keepends=True)
    section_lower = section.lower()

    # Find the matching ## heading line index
    start_idx: int | None = None
    for i, line in enumerate(lines):
        heading_match = re.match(r"^##\s+(.+)$", line.rstrip())
        if heading_match and section_lower in heading_match.group(1).lower():
            start_idx = i
            break

    if start_idx is None:
        # Section not found — append it
        new_section_text = f"\n## {section}\n\n{content}\n"
        updated = original + new_section_text
    else:
        # Find the end of this section (next ## heading or EOF)
        end_idx = len(lines)
        for j in range(start_idx + 1, len(lines)):
            if re.match(r"^##\s+", lines[j]):
                end_idx = j
                break

        # Build the replacement block
        replacement_lines = [lines[start_idx], "\n", content.rstrip("\n") + "\n", "\n"]
        updated_lines = lines[:start_idx] + replacement_lines + lines[end_idx:]
        updated = "".join(updated_lines)

    file_path.write_text(updated, encoding="utf-8")

    # Persist a version record
    version = LifeContextVersion(
        content=updated,
        change_summary=f"Updated section: {section}",
    )
    db_session.add(version)
    await db_session.commit()


def build_domain_rules_block(life_context_sections: dict[str, str]) -> str:
    """Build a runtime-templated domain-rules supplement from parsed life context sections.

    Replaces the stale hard-coded named entities and dates that previously lived
    in ``docs/SOUL.md`` Domain Rules (removed in issue #99). Instead, we pull:

    - Active open loops from the "Open Loops Taking Up Mental Space" or
      "Active Challenges" sections
    - Confirmed kid programs from the "Kids" or "Children" or family sections

    This means closed loops automatically disappear from the prompt when
    ``life_context.md`` is updated, and newly opened loops appear immediately.

    The returned string is a plain-text block suitable for appending to the
    system prompt after SOUL.md; it is prefixed with a section header so it
    reads as a natural continuation of the Domain Rules section.

    Returns an empty string when the relevant sections are absent (cold-start,
    test paths with minimal life context, etc.).

    TODO (#99): Rule 14 of the GROUNDING RULES (Susan's career) should collapse
    into this block once it is stable — the rule currently lives in
    ``agent/context/grounding_rules.py`` and references Susan by name. Once #99
    is fully adopted, rule 14 can be reduced to a structural rule and the specific
    career facts templated from the Partner section of life_context.md.
    """
    if not life_context_sections:
        return ""

    lines: list[str] = []

    # ── Open loops / Active Challenges ──────────────────────────────────────
    # Match any section whose heading contains these keywords (case-insensitive)
    open_loop_keywords = ("open loop", "active challenge", "mental space")
    open_loop_content: list[str] = []
    for heading, body in life_context_sections.items():
        if any(kw in heading.lower() for kw in open_loop_keywords):
            for line in body.splitlines():
                stripped = line.strip()
                if stripped and stripped.startswith(("-", "*", "•")):
                    open_loop_content.append(stripped.lstrip("-*• ").strip())

    if open_loop_content:
        lines.append(
            "[Domain Rules — runtime context from current life context]"
        )
        lines.append("")
        lines.append(
            "Current open loops (from 'Open Loops' / 'Active Challenges' sections):"
        )
        for item in open_loop_content:
            lines.append(f"- {item}")
        lines.append("")

    # ── Confirmed kid programs ───────────────────────────────────────────────
    # Look for "confirmed" mentions in Kids / Children / Family sections
    program_keywords = ("confirmed", "starts ", "start date", "harvard", "pre-college", "summer program")
    kid_section_keywords = ("kid", "children", "child", "family", "matthew", "connor", "dylan")
    confirmed_programs: list[str] = []
    for heading, body in life_context_sections.items():
        if any(kw in heading.lower() for kw in kid_section_keywords):
            for line in body.splitlines():
                stripped = line.strip()
                if stripped and any(kw in stripped.lower() for kw in program_keywords):
                    confirmed_programs.append(stripped.lstrip("-*• ").strip())

    if confirmed_programs:
        if not lines:
            lines.append(
                "[Domain Rules — runtime context from current life context]"
            )
            lines.append("")
        lines.append(
            "Confirmed kid programs and activities (for Pre-College Programs rule):"
        )
        for item in confirmed_programs:
            lines.append(f"- {item}")
        lines.append("")

    if not lines:
        return ""

    return "\n".join(lines).rstrip()


def build_schedule_block(config=None) -> str:
    """Generate the schedule section of the system prompt from the live job registry.

    Reads ``get_job_registry(config)`` from ``agent.scheduler`` so adding or
    removing jobs in the registry automatically updates the system prompt —
    the hardcoded f-string that previously lived here is gone.

    Returns an empty string when *config* is None (cold-start / test paths
    that don't supply a config).
    """
    if config is None:
        return ""

    from agent.scheduler import get_job_registry

    jobs = get_job_registry(config)
    lines = [
        "Your automated schedule (runs inside your process — always on while the container is up):",
    ]
    for job in jobs:
        lines.append(f"- {job.name}: {job.cron_spec} — {job.description}")
    return "\n".join(lines)


def build_capability_block(registry: "CapabilityRegistry | None" = None) -> str:
    """Generate the capability section of the system prompt from actual tool names.

    When a CapabilityRegistry is provided, statuses are reflected so the model
    knows precisely which sources are live vs. not configured.  Without a registry,
    falls back to a static accurate description (used during cold-start prompt build
    before the registry has been populated).

    Tool names here MUST match the actual registered tool names in core.py.
    A test in test_life_context.py validates this invariant.
    """
    def _status_note(registry: "CapabilityRegistry | None", source_key: str) -> str:
        if registry is None:
            return ""
        from agent.capability_registry import CapabilityStatus
        status = registry.get_status(source_key)
        if status == CapabilityStatus.AVAILABLE:
            return ""
        if status == CapabilityStatus.NOT_CONFIGURED:
            return " (not configured)"
        if status == CapabilityStatus.PERMISSION_REQUIRED:
            cap = registry.get(source_key)
            detail = cap.detail if cap else "permission required"
            return f" (permission required: {detail})"
        if status == CapabilityStatus.TEMPORARILY_UNAVAILABLE:
            return " (temporarily unavailable)"
        if status == CapabilityStatus.DISABLED:
            return " (disabled)"
        return ""

    cal_note = _status_note(registry, "calendar_google")
    gmail_note = _status_note(registry, "email_gmail")
    yahoo_note = _status_note(registry, "email_yahoo")
    imsg_note = _status_note(registry, "imessage")
    wa_note = _status_note(registry, "whatsapp")
    slack_note = _status_note(registry, "slack")
    mem_note = _status_note(registry, "memory")
    web_note = _status_note(registry, "web_search")
    files_note = _status_note(registry, "local_files")

    lines = [
        "Your available capabilities (USE THESE — never say you \"cannot\" access something listed here):",
        f"- Calendar{cal_note}: read upcoming events, meetings, appointments via "
        "get_upcoming_events / get_calendar_events_range / list_calendars",
        f"- Email (Gmail{gmail_note}, Yahoo{yahoo_note}): read inboxes via "
        "get_recent_emails / search_emails / get_email_unread_counts / "
        "get_email_action_items / get_email_summary",
        f"- iMessage{imsg_note}: read text message conversations via "
        "get_recent_imessages / get_imessage_conversation / search_imessages"
        " — REQUIRES Full Disk Access granted to Terminal or Docker Desktop",
        f"- WhatsApp{wa_note}: read WhatsApp chats via "
        "get_recent_whatsapp_chats / get_whatsapp_chat / get_whatsapp_messages / "
        "search_whatsapp / get_whatsapp_groups — available when WhatsApp Desktop is not running",
        f"- Slack{slack_note}: read channels and DMs via "
        "list_slack_channels / get_slack_channel_messages / search_slack / get_slack_deadlines",
        f"- Memory{mem_note}: save and recall personal facts via "
        "save_memory / search_memory / update_life_context",
        f"- Local files{files_note}: inspect mounted repo files and local data paths via "
        "inspect_local_path (read-only; useful for /data/* and docs/* questions)",
        "- Contacts: look up people across all channels via "
        "get_contact_profile / search_contacts / find_quiet_contacts",
        "- Comms Health: relationship signals via "
        "get_comms_health_summary / get_overdue_responses / get_relationship_balance_report",
        f"- Images{web_note}: display photos directly in Telegram via search_images — "
        "when asked for a photo or image of any person/place/thing, call search_images "
        "and embed the first result as [IMAGE:url] in your response, then add a sentence of context",
        "- Health / Wearables: NOT CONNECTED — no access to Oura, Garmin, Whoop, or Apple Health data. "
        "When asked about sleep, activity, recovery, or biometrics, say clearly that health data is not "
        "yet integrated and do not infer or guess any health metrics.",
    ]
    return "\n".join(lines)


def validate_prompt_tool_references(prompt: str, registered_tool_names: set[str]) -> list[str]:
    """Return tool names mentioned in the prompt that are NOT in registered_tool_names.

    Used in tests to catch prompt/registry drift before it reaches users.
    """
    found = re.findall(
        r"\b(get_\w+|search_\w+|save_\w+|list_\w+|update_\w+|find_\w+|inspect_\w+)\b",
        prompt,
    )
    unknown = [name for name in found if name not in registered_tool_names]
    return list(dict.fromkeys(unknown))


def build_system_prompt(life_context_path: str = None, config=None,
                        capability_registry: "CapabilityRegistry | None" = None) -> str:
    """Build the full Pepper system prompt: soul + schedule + capabilities + life context."""
    soul = load_soul()
    context = load_life_context(life_context_path or "data/life_context.md")
    # Sanitize stale past-deadline phrases so the model never sees them in the
    # system prompt (the same pattern is applied to injected context blocks in
    # core.py; applying it here ensures consistency across both code paths).
    context = re.sub(
        r'some\s+(?:January|February|March|April)\s+20\d\d\s+deadlines\s+were\s+imminent',
        'deadline window has passed — confirm current application status',
        context,
        flags=re.IGNORECASE,
    )
    owner_name = get_owner_name(life_context_path or "data/life_context.md", config)
    logger.info(
        "system_prompt_built",
        life_context_chars=len(context),
        soul_chars=len(soul),
        owner_name=owner_name,
        seeded=bool(context.strip()),
    )

    schedule_block = build_schedule_block(config)

    capability_block = build_capability_block(capability_registry)

    # Build runtime-templated domain rules from the current life context (#99).
    # This replaces hard-coded named entities and dates that previously lived in
    # SOUL.md Domain Rules. Open loops disappear from the prompt when they are
    # removed from life_context.md; confirmed programs surface automatically.
    sections = get_life_context_sections(life_context_path or "data/life_context.md")
    domain_rules_block = build_domain_rules_block(sections)

    return f"""{soul}
{schedule_block}

{capability_block}

IMPORTANT: When asked if you can read iMessages, WhatsApp, email, calendar, or mounted local files — the answer is YES, you have tools for all of these. Attempt the tool call. If the data source is unavailable (e.g. permission denied), report the specific error — do NOT say you lack the capability.

IMPORTANT: Health and biometric data (sleep, activity, recovery, heart rate, wearables — Oura, Garmin, Whoop, Apple Health) is NOT connected and has NEVER been connected. When asked about health metrics, say clearly: "Health data isn't integrated yet — I can't see wearable or biometric data." Do NOT say data was "previously shared", "previously connected", or "previously available". Do NOT guess or infer any health metrics.

Your owner's life context:
---
{context}
---

Answer questions about your owner directly from the life context above. Only call search_memory when looking for something from a previous conversation that isn't covered in the life context document.
{f"{chr(10)}{domain_rules_block}" if domain_rules_block else ""}"""
