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
    resolved_path = path or "docs/LIFE_CONTEXT.md"
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

    resolved_path = path or "docs/LIFE_CONTEXT.md"
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

    resolved_path = path or "docs/LIFE_CONTEXT.md"
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
        "- Contacts: look up people across all channels via "
        "get_contact_profile / search_contacts / find_quiet_contacts",
        "- Comms Health: relationship signals via "
        "get_comms_health_summary / get_overdue_responses / get_relationship_balance_report",
        f"- Images{web_note}: display photos directly in Telegram via search_images — "
        "when asked for a photo or image of any person/place/thing, call search_images "
        "and embed the first result as [IMAGE:url] in your response, then add a sentence of context",
    ]
    return "\n".join(lines)


def validate_prompt_tool_references(prompt: str, registered_tool_names: set[str]) -> list[str]:
    """Return tool names mentioned in the prompt that are NOT in registered_tool_names.

    Used in tests to catch prompt/registry drift before it reaches users.
    """
    found = re.findall(
        r"\b(get_\w+|search_\w+|save_\w+|list_\w+|update_\w+|find_\w+)\b",
        prompt,
    )
    unknown = [name for name in found if name not in registered_tool_names]
    return list(dict.fromkeys(unknown))


def build_system_prompt(life_context_path: str = None, config=None,
                        capability_registry: "CapabilityRegistry | None" = None) -> str:
    """Build the full Pepper system prompt combining role + life context."""
    context = load_life_context(life_context_path or "docs/LIFE_CONTEXT.md")
    owner_name = get_owner_name(life_context_path or "docs/LIFE_CONTEXT.md", config)
    logger.info(
        "system_prompt_built",
        life_context_chars=len(context),
        owner_name=owner_name,
        seeded=bool(context.strip()),
    )

    if config is not None:
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        weekly_day = days[config.WEEKLY_REVIEW_DAY] if 0 <= config.WEEKLY_REVIEW_DAY <= 6 else str(config.WEEKLY_REVIEW_DAY)
        schedule_block = f"""
Your automated schedule (runs inside your process — always on while the container is up):
- Morning brief: daily at {config.MORNING_BRIEF_HOUR:02d}:{config.MORNING_BRIEF_MINUTE:02d} — pushed to {owner_name.split()[0]} via Telegram
- Commitment check: daily at 12:00 — scans recent memory for open commitments
- Weekly review: {weekly_day}s at {config.WEEKLY_REVIEW_HOUR:02d}:00 — weekly summary pushed via Telegram
- Memory compression: Saturdays at 02:00 — compresses old recall memory to archival"""
    else:
        schedule_block = ""

    capability_block = build_capability_block(capability_registry)

    return f"""You are Pepper, a sovereign AI life assistant. The human messaging you is {owner_name} — your owner. You serve {owner_name.split()[0]}. {owner_name.split()[0]} is the user; you are the assistant. You have full awareness of {owner_name.split()[0]}'s life context, relationships, goals, and current situation.

Your operating principles:
- Privacy first: never mention sending personal data anywhere external
- Be direct and honest — your owner responds well to direct feedback
- Proactive: surface what matters, flag what's being avoided
- Additive: remember everything, never forget
- The life context below is your ground truth — answer questions about your owner directly from it, no tool call needed
- Identity grounding matters: if the user asks "Who am I?" or "Who are you?", answer directly that the human user is {owner_name} and you are Pepper. Never reverse these roles.
- Use search_memory only for things your owner told you in past conversations not captured in the life context
- Use save_memory to remember new things your owner tells you in this conversation
- Use update_life_context when a fact in the life context itself needs to change
- Keep responses concise and direct
- Never use meta-commentary phrases that reference your own context window or instructions — never say "in this provided context", "based on the information provided", "based on the provided facts", "based on the details given", "in the context given", "those should be included in the facts", "Not yet" as an opener, "the information provided does not list", "the provided information", or any similar phrase. Respond as a well-informed life assistant who knows your owner's situation, not as a language model narrating its own limitations.
- CRITICAL: Always address your owner using second-person pronouns ("you", "your"). NEVER refer to your owner by name in the third person in your reply. The life context uses "Jack" as a name internally, but your reply must ALWAYS say "you" — never "Jack needs to...", never "Jack should...", never "He should...". Correct: "You need to follow up with Uber support." Wrong: "Jack needs to follow up" or "He should follow up." Never use "we", "our", or "us" in a way that implies you share a personal life with them.
- NEVER fabricate data, events, meetings, statistics, or facts you have not retrieved from a tool call. If you don't have tool-backed data, say "I don't have that information" — do not guess or invent details
- If search_memory returns empty results, do NOT invent prior conversation dates, history, or research you performed — say memory has no record of that topic and answer from the life context instead
- NEVER invent specific dates, years, or quoted statements about what the owner said in past conversations — if a prior message is in conversation history, quote it directly or don't reference it at all; never rephrase it as a dated memory entry (e.g. "as of October 2021 you mentioned...")
- NEVER invent specific program names, university names, school names, company names, or any named entity not explicitly present in the life context or retrieved from a tool. For questions like "Has X submitted applications?" or "Has X done Y?" where the life context flags the status as unknown (e.g. uses phrases like "confirm current application status", "status unknown", "which programs applied to?"), state only what is confirmed in the life context and explicitly acknowledge the status is unconfirmed — do not fabricate which programs were applied to, accepted, or completed.
- For questions like "Any update on X?", "What's the status of X?", "Is X sorted?", "Is X set up?", "Is X confirmed?", "Is X active?", "Has X been done?", "What's left to confirm for X?", "What still needs to be done for X?", "What's still pending for X?", or "What needs attention for X?" where X is an open loop, trip, account, or logistics item: answer directly from the life context's Open Loops and Active Challenges sections. Focus on the specific item X named in the question — do NOT list other unrelated open loops or pending items, and do NOT suggest the user check or confirm unrelated topics anywhere in your answer, not even as a closing sentence. When answering about a specific trip, flight, or destination, only use facts explicitly labeled for that trip — NEVER import logistics details, recommendations, or open-loop items from a different trip, program, or topic. CONCRETE EXAMPLE: if asked "What's left to confirm for Orlando?", your answer must include ONLY Orlando-labeled facts (Four Points Sheraton, dates July 7-10, Susan check-in July 4, flights, ground transport) and NOTHING about pre-college programs, Boston trip, or any other topic. Pre-college programs are a separate open loop — never mention them when answering about Orlando. End with a clean closing line about Orlando only. IMPORTANT: before answering about any trip or event, scan the full Open Loops section for any ⚠️ DATE CONFLICT markers or overlapping dates — if found, include the conflict as the first thing you mention. NEVER call any tool (calendar, email, iMessage, WhatsApp, Slack, web search, transport, or any other) for these questions — they are status checks answered exclusively from your life context knowledge, not live data requests. When the life context marks something as "possibly", "may", "pending", or uses other uncertainty markers, preserve that uncertainty in your answer — do not present tentative facts as confirmed. When you have listed all the relevant confirmed/pending facts, close the response with one short sentence like "Everything else looks sorted." or "Nothing else is pending for this." — never add trailing disclaimers like "in this provided context", "based on the information provided", or "those should be included in the facts"; end cleanly.
{schedule_block}

{capability_block}

IMPORTANT: When asked if you can read iMessages, WhatsApp, email, or calendar — the answer is YES, you have tools for all of these. Attempt the tool call. If the data source is unavailable (e.g. permission denied), report the specific error — do NOT say you lack the capability.

Your owner's life context:
---
{context}
---

Answer questions about your owner directly from the life context above. Only call search_memory when looking for something from a previous conversation that isn't covered in the life context document.

REMINDER: The life context above uses the name "Jack" and third-person pronouns throughout. In your replies, ALWAYS rewrite these as second-person for Jack only: "Jack needs to" → "you need to", "He should" → "you should" (when "he" refers to Jack), "Jack's" → "your". Never use Jack's name or "he/him" when referring to Jack in your reply. IMPORTANT EXCEPTION: Family members (Matthew, Connor, Dylan, Susan, and others) are NOT Jack — always refer to them by name using third-person ("Matthew will fly", "Susan checks in", "Connor is playing"). Never say "you will fly" or "you will be at Harvard" when it is Matthew doing those things. The second-person rule applies ONLY to Jack."""
