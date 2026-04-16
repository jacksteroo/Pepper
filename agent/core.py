from __future__ import annotations

import asyncio
import json
import re
import time
import structlog
from datetime import datetime
from zoneinfo import ZoneInfo
from agent.config import Settings
from agent.llm import ModelClient
from agent.life_context import build_system_prompt, get_owner_name, update_life_context
from agent.tool_router import ToolRouter
from agent.memory import MemoryManager
from agent.memory_tools import MEMORY_TOOLS
from agent.models import Conversation
from agent.briefs import CommitmentExtractor
from agent.context_compressor import ContextCompressor
from agent.error_classifier import ClassifiedLLMError, ErrorCategory
from agent.skills import load_skills, SkillMatcher
from agent.skill_reviewer import SkillReviewer
from agent.web_search import brave_search, brave_image_search
from agent.routing import get_driving_time
from agent.calendar_tools import (
    CALENDAR_TOOLS,
    execute_get_upcoming_events,
    execute_get_calendar_events_range,
    execute_list_calendars,
    maybe_get_calendar_context,
)
from agent.email_tools import (
    EMAIL_TOOLS,
    execute_get_recent_emails,
    execute_get_email_action_items,
    execute_get_email_summary,
    execute_search_emails,
    execute_get_email_unread_counts,
    detect_email_account_scope,
    detect_email_time_window_hours,
    is_email_action_items_query,
    is_email_summary_query,
    maybe_get_email_context,
)
from agent.imessage_tools import (
    IMESSAGE_TOOLS,
    execute_imessage_tool,
    execute_get_recent_imessage_attention,
    is_imessage_attention_query,
    maybe_get_imessage_context,
)
from agent.whatsapp_tools import (
    WHATSAPP_TOOLS,
    execute_whatsapp_tool,
    execute_get_recent_whatsapp_attention,
    is_whatsapp_attention_query,
    maybe_get_whatsapp_context,
)
from agent.slack_tools import (
    SLACK_TOOLS,
    execute_slack_tool,
    maybe_get_slack_context,
)
from agent.contact_tools import (
    CONTACT_TOOLS,
    execute_contact_tool,
)
from agent.comms_health_tools import (
    COMMS_HEALTH_TOOLS,
    execute_comms_health_tool,
)

logger = structlog.get_logger()

IMAGE_TOOLS = [
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "search_images",
            "description": (
                "Search for images and return URLs to display them. "
                "Use this whenever the user asks to see a photo, picture, or image of someone or something. "
                "The returned URLs will be rendered as inline photos in the Telegram chat. "
                "After calling this tool, embed each image URL in your response using [IMAGE:url] "
                "and add any relevant context about the subject."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Image search query (e.g. 'Lisa Kudrow The Comeback HBO')",
                    },
                },
                "required": ["query"],
            },
        },
    }
]


class PepperCore:
    def __init__(self, config: Settings, db_session_factory=None, skills_dir=None):
        self.config = config
        self.db_factory = db_session_factory
        self.llm = ModelClient(config)
        self.memory = MemoryManager(
            llm_client=self.llm, db_session_factory=db_session_factory
        )
        self.tool_router = ToolRouter()
        self._system_prompt: str = ""
        self._initialized = False
        self.commitment_extractor = CommitmentExtractor(llm_client=self.llm)
        self._compressor = ContextCompressor(
            llm_client=self.llm,
            memory_manager=self.memory,
            config=config,
        )
        self._scheduler = None
        self._sessions_loaded: set[str] = set()  # tracks which sessions have had history reloaded

        # Phase 4: skill system
        _skills = load_skills(skills_dir=skills_dir)
        self._skill_matcher = SkillMatcher(_skills)
        self._skill_reviewer = SkillReviewer(self.llm, _skills, config)

    @staticmethod
    def _normalize_user_text(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()

    @staticmethod
    def _preview_text(text: str, max_chars: int = 160) -> str:
        normalized = re.sub(r"\s+", " ", (text or "")).strip()
        if len(normalized) <= max_chars:
            return normalized
        return f"{normalized[:max_chars]}..."

    @classmethod
    def _summarize_context_block(cls, value: str) -> dict[str, object]:
        normalized = re.sub(r"\s+", " ", (value or "")).strip()
        return {
            "present": bool(normalized),
            "chars": len(normalized),
            "preview": cls._preview_text(normalized, 140),
        }

    @classmethod
    def _summarize_tool_result(cls, result: dict) -> dict[str, object]:
        summary: dict[str, object] = {
            "status": "error" if "error" in result else "ok",
            "keys": sorted(result.keys())[:8],
        }

        if "error" in result:
            summary["error"] = cls._preview_text(str(result["error"]), 180)
        if "message" in result:
            summary["message"] = cls._preview_text(str(result["message"]), 180)
        if "summary" in result:
            summary["summary"] = cls._preview_text(str(result["summary"]), 180)
        if isinstance(result.get("results"), list):
            summary["result_count"] = len(result["results"])
        if isinstance(result.get("action_items"), list):
            summary["action_item_count"] = len(result["action_items"])
        if isinstance(result.get("commitments"), list):
            summary["commitment_count"] = len(result["commitments"])

        return summary

    @staticmethod
    def _format_email_action_items_response(result: dict, account_scope: str) -> str:
        if "error" in result:
            return f"I couldn't scan your email inboxes: {result['error']}"

        warnings = result.get("warnings", [])
        action_items = result.get("action_items", [])
        scope_text = (
            "your inboxes"
            if account_scope == "all"
            else f"your {account_scope} inbox"
        )

        if not action_items:
            response = (
                f"I scanned recent subject lines and snippets in {scope_text} and "
                "I don't see any obvious action items."
            )
        else:
            lines = [f"I found {len(action_items)} likely action item(s) in {scope_text}:"]
            for item in action_items:
                lines.append(f"- {item['formatted']}")
            response = "\n".join(lines)

        if warnings:
            response += "\n\nWarnings: " + "; ".join(warnings)
        return response

    @staticmethod
    def _format_email_summary_response(result: dict, account_scope: str) -> str:
        if "error" in result:
            return f"I couldn't scan your email inboxes: {result['error']}"

        warnings = result.get("warnings", [])
        emails = result.get("emails", [])
        important = result.get("important", [])
        hours = result.get("hours", 24)
        scope_text = (
            "your inboxes"
            if account_scope == "all"
            else f"your {account_scope} inbox"
        )

        if not emails:
            response = f"I don't see any emails in {scope_text} from the last {hours} hours."
        else:
            lines = [f"I found {len(emails)} email(s) in {scope_text} from the last {hours} hours."]
            if important:
                lines.append("")
                lines.append("Most important:")
                for item in important:
                    lines.append(f"- {item['formatted']}")
            else:
                lines.append("")
                lines.append("Nothing looks especially urgent from the subject lines and snippets.")
                lines.append("Recent messages:")
                for item in emails[:5]:
                    lines.append(f"- {item['formatted']}")
            response = "\n".join(lines)

        if warnings:
            response += "\n\nWarnings: " + "; ".join(warnings)
        return response

    def _answer_identity_question(self, user_message: str) -> str | None:
        normalized = self._normalize_user_text(user_message)
        if not normalized:
            return None

        owner_patterns = (
            r"\bwho am i\b",
            r"\bwho am i now\b",
            r"\bdo you know who i am\b",
            r"\bwhat do you know about me\b",
            r"\bdo you know anything about me\b",
            r"\btell me about me\b",
        )
        assistant_patterns = (
            r"\bwho are you\b",
            r"\bwhat are you\b",
            r"\bwhat is your name\b",
            r"\bwhat s your name\b",
        )

        asks_owner_identity = any(re.search(pattern, normalized) for pattern in owner_patterns)
        asks_assistant_identity = any(re.search(pattern, normalized) for pattern in assistant_patterns)

        if not asks_owner_identity and not asks_assistant_identity:
            return None

        owner_name = get_owner_name(self.config.LIFE_CONTEXT_PATH, self.config)
        if asks_owner_identity and asks_assistant_identity:
            return f"You are {owner_name}. I'm Pepper, your AI life assistant."
        if asks_owner_identity:
            return f"You are {owner_name}."
        return "I'm Pepper, your AI life assistant."

    @staticmethod
    def _probe_subsystem_health() -> dict[str, str]:
        """Check subsystem availability by probing in-process imports.

        Tools run in-process (not via HTTP microservices), so we test whether
        the key module for each subsystem is importable rather than pinging a port.
        """
        import importlib

        probes = {
            "calendar": "subsystems.calendar.client",
            "communications": "subsystems.communications.gmail_client",
            "knowledge": "subsystems.knowledge",
            "health": "subsystems.health",
            "finance": "subsystems.finance",
            "people": "subsystems.people",
        }
        result = {}
        for name, module_path in probes.items():
            try:
                importlib.import_module(module_path)
                result[name] = "ok"
            except ImportError:
                result[name] = "down"
        return result

    async def initialize(self) -> None:
        """Call once at startup."""
        self._system_prompt = build_system_prompt(self.config.LIFE_CONTEXT_PATH, self.config)
        self._initialized = True
        logger.info("pepper_initialized", subsystems=self._probe_subsystem_health())

    # ── Query depth classification ─────────────────────────────────────────────

    _CLASSIFY_SYSTEM = (
        "You decide whether a message needs a live API/data lookup before "
        "answering. Reply with exactly one word — HEAVY or LIGHT — no "
        "punctuation, no explanation.\n\n"
        "Default: HEAVY. Only answer LIGHT if the message is one of the "
        "following narrow cases:\n"
        "  - pure greeting, thanks, acknowledgment, or chit-chat "
        "    ('hi', 'thanks', 'cool', 'ok', 'good morning')\n"
        "  - a question about general world knowledge or an abstract "
        "    explanation that has zero dependence on the user's personal "
        "    data, calendar, inbox, contacts, history, or memory\n"
        "  - a coding/math question with no personal-data dependency\n\n"
        "Everything else is HEAVY. In particular HEAVY covers: anything "
        "that would require an API call (calendar, email, iMessage, "
        "WhatsApp, Slack, web search, weather, maps/directions), any "
        "memory recall of past conversations, anything about the user's "
        "own life state (priorities, focus, schedule, commitments, "
        "follow-ups, what to do today/tomorrow, who's waiting on them), "
        "any draft reply or message that must reference real people / "
        "projects / threads, AND any follow-up that builds on a previous "
        "answer that itself was HEAVY.\n\n"
        "HEAVY also covers any question asking what the assistant knows "
        "about the user — e.g. 'what do you know about me?', 'tell me "
        "about myself', 'what's my situation', 'what's my context', "
        "'who am I', 'what do you remember about me', 'summarize my "
        "life', 'what are my goals' — these require reading the user's "
        "personal profile and must be HEAVY.\n\n"
        "When in doubt, HEAVY."
    )

    async def classify_query(self, message: str) -> bool:
        """Return True if the message needs proactive data fetches.

        Uses the local LLM so it handles any language, typos, and paraphrasing.
        Falls back to True (heavy path) on any error — conservative default.
        """
        try:
            result = await self.llm.chat(
                messages=[
                    {"role": "system", "content": self._CLASSIFY_SYSTEM},
                    {"role": "user", "content": message},
                ],
                model=f"local/{self.config.DEFAULT_LOCAL_MODEL}",
                options={"num_predict": 5},
            )
            verdict = result.get("content", "HEAVY").strip().upper().split()[0]
            heavy = verdict != "LIGHT"
            logger.debug("classify_query", verdict=verdict, heavy=heavy, message=message[:80])
            return heavy
        except Exception as exc:
            logger.warning("classify_query_failed", error=str(exc), message=message[:80])
            return True  # safe default: full fetch path

    async def chat(
        self,
        user_message: str,
        session_id: str,
        progress_callback=None,
        heavy: bool | None = None,
        channel: str = "",
        isolated: bool = False,
    ) -> str:
        """Main conversation entry point.

        progress_callback: optional async callable(str) called at key processing stages
        so callers (e.g. Telegram bot) can surface real-time status to the user.

        heavy: if already classified by the caller, pass it here to skip a
        redundant LLM call. If None, classify_query() is called automatically.

        channel: the interface the user is messaging from (e.g. "Telegram", "HTTP API").
        Injected into the system prompt so the model knows its context.

        isolated: when True, this turn does NOT touch the shared working-memory
        deque — no session history is loaded, no user/assistant turns are appended.
        Use for scheduler/automation calls so they never bleed into user sessions
        and concurrent user turns are never overwritten.
        """
        started_at = time.perf_counter()
        chat_logger = logger.bind(session_id=session_id, channel=channel or "HTTP API")

        if not self._initialized:
            chat_logger.info("chat_initialize_start")
            await self.initialize()
            chat_logger.info("chat_initialize_complete")

        chat_logger.info(
            "chat_in",
            text=user_message[:300],
            message_chars=len(user_message),
        )

        async def _progress(msg: str) -> None:
            # Log every heavy-path progress ack so it lands in docker stdout
            # and logs/pepper.log — useful for the simulator + eval loop to
            # see exactly what data fetches fired for each turn.
            chat_logger.info("progress_ack", message=msg)
            if progress_callback:
                try:
                    await progress_callback(msg)
                except Exception:
                    pass

        # Reload conversation history from DB on first use after a restart.
        # Skipped for isolated calls — they start fresh with no prior turns.
        if not isolated:
            if session_id not in self._sessions_loaded:
                chat_logger.info("session_history_reload_requested")
                self._sessions_loaded.add(session_id)
                await self._reload_session_history(session_id)
            else:
                chat_logger.debug("session_history_reload_skipped", reason="already_loaded")

        # Add to working memory (skipped for isolated scheduler/automation calls so
        # those turns never appear in later user sessions).
        if not isolated:
            self.memory.add_to_working_memory("user", user_message)
            chat_logger.info(
                "working_memory_user_added",
                working_memory_size=len(self.memory._working),
                user_preview=self._preview_text(user_message, 180),
            )

        identity_response = self._answer_identity_question(user_message)
        if identity_response is not None:
            if not isolated:
                self.memory.add_to_working_memory("assistant", identity_response)
            chat_logger.info(
                "identity_short_circuit",
                response_preview=self._preview_text(identity_response, 180),
            )
            chat_logger.info("chat_out", text=identity_response[:1000])
            if not isolated:
                await self._save_conversation(session_id, user_message, identity_response)
            chat_logger.info(
                "chat_complete",
                path="identity",
                duration_ms=round((time.perf_counter() - started_at) * 1000),
            )
            return identity_response

        # Detect and save commitments from user messages
        if self.commitment_extractor.has_commitment_language(user_message):
            commitments = await self.commitment_extractor.extract_from_text(user_message)
            chat_logger.info(
                "commitments_detected",
                count=len(commitments),
                items=[self._preview_text(c.get("text", ""), 120) for c in commitments],
            )
            for c in commitments:
                await self.memory.save_to_recall(
                    f"COMMITMENT: {c['text']}", importance=0.8
                )
        else:
            chat_logger.debug("commitments_not_detected")

        if heavy is None:
            if self.config.ALWAYS_HEAVY:
                heavy = True
                chat_logger.debug("query_depth", heavy=True, reason="ALWAYS_HEAVY", message=user_message[:80])
            else:
                heavy = await self.classify_query(user_message)
                chat_logger.debug("query_depth", heavy=heavy, reason="classified", message=user_message[:80])
        else:
            chat_logger.debug("query_depth", heavy=heavy, reason="caller_set", message=user_message[:80])

        if heavy and is_email_action_items_query(user_message):
            await _progress("Scanning inboxes for action items...")
            account_scope = detect_email_account_scope(user_message)
            result = await execute_get_email_action_items(
                {"account": account_scope, "count": 8, "hours": 168}
            )
            chat_logger.info(
                "email_action_items_result",
                account_scope=account_scope,
                result=self._summarize_tool_result(result),
            )
            response_text = self._format_email_action_items_response(result, account_scope)
            if not isolated:
                self.memory.add_to_working_memory("assistant", response_text)
            chat_logger.info("chat_out", text=response_text[:1000])
            if not isolated:
                await self._save_conversation(session_id, user_message, response_text)
            chat_logger.info(
                "chat_complete",
                path="email_action_items",
                duration_ms=round((time.perf_counter() - started_at) * 1000),
            )
            return response_text

        if heavy and is_email_summary_query(user_message):
            await _progress("Scanning recent emails...")
            account_scope = detect_email_account_scope(user_message)
            hours = detect_email_time_window_hours(user_message)
            result = await execute_get_email_summary(
                {"account": account_scope, "count": 10, "hours": hours}
            )
            chat_logger.info(
                "email_summary_result",
                account_scope=account_scope,
                hours=hours,
                result=self._summarize_tool_result(result),
            )
            response_text = self._format_email_summary_response(result, account_scope)
            if not isolated:
                self.memory.add_to_working_memory("assistant", response_text)
            chat_logger.info("chat_out", text=response_text[:1000])
            if not isolated:
                await self._save_conversation(session_id, user_message, response_text)
            chat_logger.info(
                "chat_complete",
                path="email_summary",
                duration_ms=round((time.perf_counter() - started_at) * 1000),
            )
            return response_text

        if heavy and is_whatsapp_attention_query(user_message):
            await _progress("Scanning recent WhatsApp chats...")
            result = await execute_get_recent_whatsapp_attention(
                {"limit": 8, "message_limit": 3}
            )
            chat_logger.info(
                "whatsapp_attention_result",
                result=self._summarize_tool_result(result),
            )
            if "error" in result:
                response_text = f"I couldn't scan your WhatsApp chats: {result['error']}"
            else:
                response_text = result["summary"]
            if not isolated:
                self.memory.add_to_working_memory("assistant", response_text)
            chat_logger.info("chat_out", text=response_text[:1000])
            if not isolated:
                await self._save_conversation(session_id, user_message, response_text)
            chat_logger.info(
                "chat_complete",
                path="whatsapp_attention",
                duration_ms=round((time.perf_counter() - started_at) * 1000),
            )
            return response_text

        if heavy and is_imessage_attention_query(user_message):
            await _progress("Scanning recent iMessages...")
            result = await execute_get_recent_imessage_attention(
                {"limit": 8, "days": 30, "message_limit": 3}
            )
            chat_logger.info(
                "imessage_attention_result",
                result=self._summarize_tool_result(result),
            )
            if "error" in result:
                response_text = f"I couldn't scan your iMessages: {result['error']}"
            else:
                response_text = result["summary"]
            if not isolated:
                self.memory.add_to_working_memory("assistant", response_text)
            chat_logger.info("chat_out", text=response_text[:1000])
            if not isolated:
                await self._save_conversation(session_id, user_message, response_text)
            chat_logger.info(
                "chat_complete",
                path="imessage_attention",
                duration_ms=round((time.perf_counter() - started_at) * 1000),
            )
            return response_text

        if heavy:
            # For follow-up questions ("what's the second priority?") the
            # current turn often won't match keyword triggers on its own.
            # Concatenate the previous user turn so trigger heuristics inherit
            # context from the parent question.  Isolated calls have no prior
            # turns, so history_for_triggers is empty.
            history_for_triggers = [] if isolated else self.memory.get_working_memory(limit=6)
            prior_user_turns = [m["content"] for m in history_for_triggers if m.get("role") == "user"][-3:-1]
            trigger_text = " ".join(prior_user_turns + [user_message])

            # Full proactive fetch path — inject live data before the LLM sees the question.
            # All fetches are independent I/O, so run them concurrently with gather()
            # rather than awaiting one at a time. Cuts the heavy-path latency to the
            # slowest single fetch instead of the sum.
            await _progress("Scanning calendar, inbox, messages, memory...")
            proactive_fetch_started = time.perf_counter()

            fetch_results = await asyncio.gather(
                self.memory.build_context_for_query(user_message),
                self._maybe_search_web(user_message),
                self._maybe_get_driving_time(user_message),
                maybe_get_calendar_context(trigger_text),
                maybe_get_email_context(trigger_text),
                maybe_get_imessage_context(trigger_text),
                maybe_get_whatsapp_context(trigger_text),
                maybe_get_slack_context(trigger_text),
                return_exceptions=True,
            )

            # Unpack — any exception becomes empty context so one slow/broken
            # subsystem can't take down the whole turn.
            labels = (
                "memory", "web", "routing", "calendar",
                "email", "imessage", "whatsapp", "slack",
            )
            unpacked: list[str] = []
            for label, res in zip(labels, fetch_results):
                if isinstance(res, Exception):
                    chat_logger.warning("proactive_fetch_failed", source=label, error=str(res))
                    unpacked.append("")
                else:
                    unpacked.append(res or "")
            (
                memory_context, web_context, routing_context, calendar_context,
                email_context, imessage_context, whatsapp_context, slack_context,
            ) = unpacked

            context_summary = {
                label: self._summarize_context_block(value)
                for label, value in zip(labels, unpacked)
            }
            chat_logger.info(
                "proactive_fetch_complete",
                duration_ms=round((time.perf_counter() - proactive_fetch_started) * 1000),
                contexts=context_summary,
            )

            model = self.config.select_model("conversation", "summary")
        else:
            # Fast path — skip all proactive fetches, answer directly from working memory
            memory_context = web_context = routing_context = ""
            calendar_context = email_context = ""
            imessage_context = whatsapp_context = slack_context = ""
            model = f"local/{self.config.DEFAULT_LOCAL_MODEL}"

        chat_logger.info(
            "model_selected",
            heavy=heavy,
            model=model,
        )

        # Build system prompt, optionally augmented with recalled context
        tz = ZoneInfo(self.config.TIMEZONE)
        now_local = datetime.now(tz)
        system = f"[Current time: {now_local.strftime('%A, %B %-d, %Y at %-I:%M %p')} {now_local.tzname()} ({self.config.TIMEZONE})]\n\n" + self._system_prompt
        if channel:
            system = f"[Interface: You are responding via {channel}.]\n\n" + system
        if memory_context:
            system += f"\n\n{memory_context}"
        if web_context:
            system += f"\n\n{web_context}"
        if routing_context:
            system += f"\n\n{routing_context}"
        if calendar_context:
            system += f"\n\n{calendar_context}"
        if email_context:
            system += f"\n\n{email_context}"
        if imessage_context:
            system += f"\n\n{imessage_context}"
        if whatsapp_context:
            system += f"\n\n{whatsapp_context}"
        if slack_context:
            system += f"\n\n{slack_context}"

        chat_logger.info(
            "context_injected_into_prompt",
            heavy=heavy,
            contexts={
                "memory": self._summarize_context_block(memory_context),
                "web": self._summarize_context_block(web_context),
                "routing": self._summarize_context_block(routing_context),
                "calendar": self._summarize_context_block(calendar_context),
                "email": self._summarize_context_block(email_context),
                "imessage": self._summarize_context_block(imessage_context),
                "whatsapp": self._summarize_context_block(whatsapp_context),
                "slack": self._summarize_context_block(slack_context),
            },
            system_prompt_chars=len(system),
        )

        if heavy:
            # Anti-hallucination guardrail. Small local models love to emit
            # template placeholders like [Commitment XYZ] / [Name] / [Date]
            # when a question matches a familiar shape. Forbid that
            # explicitly and force grounding on the injected context.
            owner_name = get_owner_name(self.config.LIFE_CONTEXT_PATH, self.config)
            owner_first = owner_name.split()[0]
            system += (
                "\n\n[GROUNDING RULES — read before answering]\n"
                f"0. The human user is {owner_name}. "
                "You are Pepper. If asked who the user is, answer with the human's identity, not your own.\n"
                f"1. The sections above (calendar, email, messages, memory, "
                f"web) contain REAL data fetched live for this turn. Use "
                f"ONLY that data when answering questions about {owner_first}'s "
                "life, schedule, inbox, contacts, or commitments.\n"
                "2. NEVER emit placeholder template text like "
                "'[Commitment XYZ]', '[Name]', '[Date]', '[Project ABC]', "
                "or any bracketed stand-in. If you don't have a specific "
                "real item to name, say so plainly: 'I don't see anything "
                "specific in your <calendar/inbox/...> matching that.'\n"
                "3. If a section above is empty or missing, do NOT invent "
                "events, emails, or commitments. Say what's missing.\n"
                "4. Quote real entity names (real people, real meeting "
                "titles, real subject lines) directly from the data above. "
                "If you can't, that's a signal you don't have the answer.\n"
                "5. If asked whether you have access to WhatsApp, iMessage, email, "
                "or any other data source, call the relevant tool first. NEVER "
                "claim you can or cannot see messages without tool evidence. "
                "If the tool returns an error, report the error verbatim.\n"
                "6. If the user names a specific source like WhatsApp, answer "
                "from that source only unless they explicitly ask to combine "
                "multiple sources.\n"
                f"7. Be concise and direct. {owner_first} prefers short answers."
            )
            await _progress("Synthesizing response...")

        # Phase 4.2: inject matching skill workflows into the system prompt.
        # Skills are guidance, not mandates — the model follows them when relevant.
        matched_skills = self._skill_matcher.match(user_message)
        if matched_skills:
            system = self._skill_matcher.inject_into_prompt(system, user_message)
            # Honor skill model declarations: upgrade to frontier when any matched
            # skill requires it.  Only applies on the heavy path — the fast path
            # is intentionally local-only to avoid latency and cost on quick queries.
            #
            # Privacy guard: the heavy path injects raw personal data (email,
            # iMessage, WhatsApp, Slack) into the system prompt, so frontier
            # upgrades are blocked whenever any of those contexts is non-empty.
            # Frontier models must never receive raw personal content.
            if heavy and any(s.model == "frontier" for s in matched_skills):
                # memory_context is included here because build_context_for_query()
                # injects raw recalled contents verbatim — it must never reach a
                # frontier model even when the message-channel contexts are empty.
                has_raw_personal = any([
                    memory_context, email_context, imessage_context,
                    whatsapp_context, slack_context,
                ])
                if has_raw_personal:
                    chat_logger.warning(
                        "model_upgrade_blocked_raw_personal",
                        skills=[s.name for s in matched_skills if s.model == "frontier"],
                    )
                else:
                    frontier_model = self.config.DEFAULT_FRONTIER_MODEL
                    if frontier_model != model:
                        model = frontier_model
                        chat_logger.info(
                            "model_upgraded_for_skill",
                            new_model=model,
                            skills=[s.name for s in matched_skills if s.model == "frontier"],
                        )
            chat_logger.info(
                "skills_injected",
                names=[s.name for s in matched_skills],
                message_preview=user_message[:80],
            )

        # Working memory already includes the user message we just added.
        # Isolated calls have no shared history — use an empty list.
        history = [] if isolated else self.memory.get_working_memory(limit=20)
        messages = [{"role": "system", "content": system}] + history
        chat_logger.info(
            "llm_messages_prepared",
            n_messages=len(messages),
            history_messages=len(history),
        )

        # Phase 3.2: compress if approaching context window limit.
        # Compression always uses the local model — never routes to frontier.
        if self._compressor.needs_compression(messages):
            chat_logger.info("context_compression_start", n_messages=len(messages))
            messages = await self._compressor.compress(messages)
            chat_logger.info("context_compression_complete", n_messages=len(messages))

        # All tools run in-process — no HTTP microservices to fetch from.
        tools = MEMORY_TOOLS + CALENDAR_TOOLS + EMAIL_TOOLS + IMESSAGE_TOOLS + WHATSAPP_TOOLS + SLACK_TOOLS + CONTACT_TOOLS + COMMS_HEALTH_TOOLS + IMAGE_TOOLS

        # Call LLM — catches ClassifiedLLMError from Phase 3.3 error classifier.
        # Context overflow mid-call (edge case: compressor threshold was not hit but
        # model still overflowed) triggers a forced compress + single retry.  All
        # other errors surface a user-friendly message and abort the turn.
        response_text = ""
        tool_calls: list = []  # populated below; kept in scope for the skill reviewer
        try:
            chat_logger.info("llm_dispatch", model=model, n_messages=len(messages), tool_count=len(tools))
            result = await self.llm.chat(messages, tools=tools or None, model=model)
            response_text = result.get("content", "")
            tool_calls = result.get("tool_calls", [])
            chat_logger.info(
                "llm_result_received",
                model=result.get("model_used", model),
                latency_ms=round(result.get("latency_ms", 0)),
                response_chars=len(response_text),
                tool_call_count=len(tool_calls),
                tool_names=[c.get("function", {}).get("name") for c in tool_calls],
            )
            if tool_calls:
                response_text = await self._handle_tool_calls(
                    tool_calls, messages, model, session_id, tools=tools
                )
        except ClassifiedLLMError as llm_err:
            if llm_err.category == ErrorCategory.CONTEXT_OVERFLOW:
                logger.warning(
                    "context_overflow_mid_call",
                    model=model,
                    session_id=session_id,
                    n_messages=len(messages),
                )
                try:
                    chat_logger.info("llm_retry_after_overflow_start", model=model)
                    messages = await self._compressor.compress(messages)
                    result = await self.llm.chat(messages, tools=tools or None, model=model)
                    response_text = result.get("content", "")
                    tool_calls = result.get("tool_calls", [])
                    chat_logger.info(
                        "llm_retry_after_overflow_result",
                        model=result.get("model_used", model),
                        latency_ms=round(result.get("latency_ms", 0)),
                        response_chars=len(response_text),
                        tool_call_count=len(tool_calls),
                        tool_names=[c.get("function", {}).get("name") for c in tool_calls],
                    )
                    if tool_calls:
                        response_text = await self._handle_tool_calls(
                            tool_calls, messages, model, session_id, tools=tools
                        )
                except ClassifiedLLMError as retry_err:
                    logger.error(
                        "llm_retry_after_overflow_failed",
                        category=retry_err.category,
                        session_id=session_id,
                    )
                    response_text = retry_err.user_message
            else:
                logger.warning(
                    "llm_call_failed",
                    category=llm_err.category,
                    session_id=session_id,
                    model=model,
                )
                response_text = llm_err.user_message

        # Hallucination guard: small local models love to emit bracketed
        # placeholder text like [Commitment XYZ] / [Name] / [Date] when
        # they pattern-match a familiar question shape but don't actually
        # ground on the injected context. Detect, strip the placeholders,
        # and keep the rest of the response — blanket rejection discards
        # real grounded content just because one phrase leaked.
        import re as _re
        _placeholder_re = _re.compile(
            r"\[(?:Commitment|Project|Name|Date|Time|Person|Place|Address|"
            r"Email|Subject|Topic|Task|Item|Meeting|Event)[^\]]{0,40}\]",
            _re.IGNORECASE,
        )
        placeholders_found = _placeholder_re.findall(response_text) if response_text else []
        if placeholders_found:
            logger.warning(
                "hallucination_guard_triggered",
                session_id=session_id,
                model=model,
                heavy=heavy,
                placeholders=placeholders_found,
                snippet=response_text[:1000],
            )
            # Strip placeholders in-place; collapse any double-spaces left behind
            response_text = _placeholder_re.sub("", response_text)
            response_text = _re.sub(r"  +", " ", response_text).strip()

        # Guard: local models sometimes return empty output when the context is
        # too large or they get confused. Surface a clear error rather than
        # sending a blank message to the user.
        if not response_text:
            logger.warning("empty_llm_response", model=model, session_id=session_id, n_messages=len(messages))
            response_text = (
                "I wasn't able to form a response — the model returned empty output. "
                "This usually means the conversation context is getting long. "
                "Try asking a more specific question, or start a fresh conversation."
            )

        # Add assistant response to working memory (skipped for isolated calls).
        if not isolated:
            self.memory.add_to_working_memory("assistant", response_text)
            chat_logger.info(
                "working_memory_assistant_added",
                working_memory_size=len(self.memory._working),
                response_preview=self._preview_text(response_text, 180),
            )

        chat_logger.info("chat_out", text=response_text[:1000])

        # Phase 4.3: fire background skill review (non-blocking, best-effort).
        # Runs after the response is ready so it never delays the user.
        if matched_skills:
            _tool_names_made = [
                c.get("function", {}).get("name")
                for c in tool_calls
                if c.get("function", {}).get("name")
            ]
            asyncio.create_task(
                self._skill_reviewer.review_turn(
                    skill_names=[s.name for s in matched_skills],
                    user_message=user_message,
                    assistant_response=response_text,
                    tool_calls_made=_tool_names_made,
                )
            )

        # Save conversation to DB (best-effort). Isolated scheduler turns are
        # excluded — they are synthetic automation prompts, not user conversations.
        if not isolated:
            await self._save_conversation(session_id, user_message, response_text)

        chat_logger.info(
            "chat_complete",
            path="full_pipeline",
            duration_ms=round((time.perf_counter() - started_at) * 1000),
            response_chars=len(response_text),
        )

        return response_text

    async def _handle_tool_calls(
        self, tool_calls: list, messages: list, model: str, session_id: str,
        tools: list | None = None,
    ) -> str:
        """Execute tool calls and get final response.

        Read-only tool calls (side_effects=False) are dispatched concurrently via
        asyncio.gather. Any batch that contains at least one side-effect tool falls
        back to sequential execution in model-produced order.
        """
        tool_logger = logger.bind(session_id=session_id)

        # Build name → side_effects lookup from the active tool list.
        # Unknown tools (e.g. subsystem tools not in the local registry) default
        # to True (conservative — treat as side-effect until proven otherwise).
        side_effects_map: dict[str, bool] = {}
        for t in (tools or []):
            name = t.get("function", {}).get("name")
            if name:
                side_effects_map[name] = t.get("side_effects", True)

        has_any_side_effect = any(
            side_effects_map.get(c.get("function", {}).get("name", ""), True)
            for c in tool_calls
        )

        tool_logger.info(
            "tool_pipeline_start",
            n_tool_calls=len(tool_calls),
            tool_names=[c.get("function", {}).get("name") for c in tool_calls],
            execution_mode="sequential" if has_any_side_effect or len(tool_calls) <= 1 else "parallel",
        )

        async def _run_one(call: dict) -> dict:
            fn = call.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            return await self._execute_tool(name, args)

        tool_results: list[dict] = []

        if not has_any_side_effect and len(tool_calls) > 1:
            # All reads — run concurrently and preserve original ordering
            logger.debug(
                "tool_calls_parallel",
                n=len(tool_calls),
                names=[c.get("function", {}).get("name") for c in tool_calls],
            )
            raw_results = await asyncio.gather(
                *[_run_one(c) for c in tool_calls], return_exceptions=True
            )
            for call, result in zip(tool_calls, raw_results):
                if isinstance(result, Exception):
                    logger.warning(
                        "tool_call_exception",
                        name=call.get("function", {}).get("name"),
                        error=str(result),
                    )
                    result = {"error": str(result)}
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps(result),
                })
        else:
            # Sequential: side effects present, or only one call (no benefit to gather)
            if has_any_side_effect and len(tool_calls) > 1:
                logger.debug(
                    "tool_calls_sequential",
                    n=len(tool_calls),
                    names=[c.get("function", {}).get("name") for c in tool_calls],
                    reason="side_effects",
                )
            for call in tool_calls:
                result = await _run_one(call)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps(result),
                })

        # Continue conversation with tool results
        messages_with_results = (
            messages
            + [{"role": "assistant", "content": "", "tool_calls": tool_calls}]
            + tool_results
        )

        tool_logger.info(
            "tool_results_ready_for_followup",
            n_tool_results=len(tool_results),
            tool_result_previews=[
                self._preview_text(result.get("content", ""), 160)
                for result in tool_results
            ],
        )

        tool_logger.info("tool_followup_llm_dispatch", model=model, n_messages=len(messages_with_results))
        follow_up = await self.llm.chat(messages_with_results, model=model)
        response_text = follow_up.get("content", "")
        tool_logger.info(
            "tool_followup_llm_result",
            model=follow_up.get("model_used", model),
            latency_ms=round(follow_up.get("latency_ms", 0)),
            response_chars=len(response_text),
        )

        # hermes3 sometimes returns empty content after tool calls. Re-prompt without
        # tools to force a summary.
        if not response_text:
            logger.debug("tool_followup_empty", n_tool_calls=len(tool_calls), retrying=True)
            retry_messages = messages_with_results + [
                {"role": "user", "content": "Please summarize what you found."}
            ]
            tool_logger.info("tool_followup_retry_dispatch", model=model, n_messages=len(retry_messages))
            retry = await self.llm.chat(retry_messages, model=model)
            response_text = retry.get("content", "")
            tool_logger.info(
                "tool_followup_retry_result",
                model=retry.get("model_used", model),
                latency_ms=round(retry.get("latency_ms", 0)),
                response_chars=len(response_text),
            )

        tool_logger.info(
            "tool_pipeline_complete",
            response_preview=self._preview_text(response_text, 180),
        )

        return response_text

    async def _execute_tool(self, name: str, args: dict) -> dict:
        """Route tool call to memory tools or subsystem."""
        started_at = time.perf_counter()
        logger.info("tool_call_started", name=name, args=args)

        try:
            if name == "save_memory":
                await self.memory.save_to_recall(
                    args.get("content", ""), args.get("importance", 0.5)
                )
                result = {"ok": True, "message": "Saved to memory"}

            elif name == "search_memory":
                results = await self.memory.search_recall(
                    args.get("query", ""), args.get("limit", 5)
                )
                result = {"results": results}

            elif name == "update_life_context":
                if self.db_factory:
                    async with self.db_factory() as session:
                        await update_life_context(
                            args["section"],
                            args["content"],
                            session,
                            self.config.LIFE_CONTEXT_PATH,
                        )
                self._system_prompt = build_system_prompt(self.config.LIFE_CONTEXT_PATH, self.config)
                result = {"ok": True, "message": f"Updated section: {args['section']}"}

            elif name == "get_driving_time":
                api_key = self.config.GOOGLE_MAPS_API_KEY
                if not api_key:
                    result = {"error": "GOOGLE_MAPS_API_KEY not configured"}
                else:
                    result = await get_driving_time(
                        args.get("origin", ""),
                        args.get("destination", ""),
                        api_key,
                    )

            elif name == "search_web":
                api_key = self.config.BRAVE_API_KEY
                if not api_key:
                    result = {"error": "BRAVE_API_KEY not configured"}
                else:
                    results = await brave_search(
                        args.get("query", ""), api_key, count=args.get("count", 5)
                    )
                    result = {"results": results}

            elif name == "search_images":
                api_key = self.config.BRAVE_API_KEY
                if not api_key:
                    result = {"error": "BRAVE_API_KEY not configured"}
                else:
                    query = args.get("query", "")
                    if not query:
                        result = {"error": "search_images requires a non-empty 'query' argument"}
                    else:
                        try:
                            urls = await brave_image_search(query, api_key, count=3)
                            if not urls:
                                result = {"error": "No images found for that query."}
                            else:
                                # Tell the LLM explicitly that embedding [IMAGE:url] renders the photo
                                # in Telegram — so it doesn't apologise about "not being able to show images"
                                embedded = " ".join(f"[IMAGE:{u}]" for u in urls[:1])
                                result = {
                                    "displayed": embedded,
                                    "note": (
                                        "The image above is already being displayed in Telegram. "
                                        "Include the [IMAGE:url] marker in your response exactly as shown in 'displayed', "
                                        "then add one sentence of context. Do NOT say you cannot show images."
                                    ),
                                }
                        except Exception as e:
                            logger.warning("search_images_failed", query=query, error=str(e))
                            result = {"error": f"Image search failed: {e}"}

            elif name == "get_upcoming_events":
                result = await execute_get_upcoming_events(args)

            elif name == "get_calendar_events_range":
                result = await execute_get_calendar_events_range(args)

            elif name == "list_calendars":
                result = await execute_list_calendars()

            elif name == "get_recent_emails":
                result = await execute_get_recent_emails(args)

            elif name == "search_emails":
                result = await execute_search_emails(args)

            elif name == "get_email_unread_counts":
                result = await execute_get_email_unread_counts(args)

            elif name in ("get_recent_imessages", "get_imessage_conversation", "search_imessages"):
                result = await execute_imessage_tool(name, args)

            elif name in (
                "get_recent_whatsapp_chats", "get_whatsapp_chat", "get_whatsapp_messages",
                "search_whatsapp", "get_whatsapp_groups",
            ):
                result = await execute_whatsapp_tool(name, args)

            elif name in (
                "search_slack", "get_slack_channel_messages",
                "get_slack_deadlines", "list_slack_channels",
            ):
                result = await execute_slack_tool(name, args)

            elif name in ("get_contact_profile", "find_quiet_contacts", "search_contacts"):
                result = await execute_contact_tool(name, args)

            elif name in (
                "get_comms_health_summary", "get_overdue_responses",
                "get_relationship_balance_report",
            ):
                result = await execute_comms_health_tool(name, args)

            elif name == "mark_commitment_complete":
                await self.memory.save_to_recall(
                    f"[RESOLVED] {args.get('description', '')}", importance=0.6
                )
                result = {"ok": True}

            else:
                # Route to subsystem — find which subsystem owns this tool
                for subsystem in self.tool_router._subsystems:
                    result = await self.tool_router.call_tool(subsystem, name, args)
                    if "error" not in result:
                        logger.debug("tool_result", name=name, result=result)
                        break
                else:
                    result = {"error": f"Unknown tool: {name}"}
                    logger.warning("tool_unknown", name=name)

            logger.info(
                "tool_call_completed",
                name=name,
                duration_ms=round((time.perf_counter() - started_at) * 1000),
                result=self._summarize_tool_result(result),
            )
            return result
        except Exception as exc:
            logger.error(
                "tool_call_failed",
                name=name,
                error=str(exc),
                duration_ms=round((time.perf_counter() - started_at) * 1000),
            )
            raise

    async def _reload_session_history(self, session_id: str) -> None:
        """Reload the last 20 turns for this session from DB into working memory.

        Called once per session after a restart so conversation context survives
        process bounces. Does nothing if the DB is unavailable or the session is new.
        """
        if not self.db_factory:
            return
        try:
            reload_started = time.perf_counter()
            from sqlalchemy import select
            from agent.models import Conversation as ConvModel
            async with self.db_factory() as db:
                result = await db.execute(
                    select(ConvModel)
                    .where(ConvModel.session_id == session_id)
                    .order_by(ConvModel.created_at.desc())
                    .limit(20)
                )
                rows = result.scalars().all()
            if not rows:
                logger.info("session_history_empty", session_id=session_id)
                return
            # Rows are newest-first; reverse to get chronological order before loading
            for row in reversed(rows):
                self.memory.add_to_working_memory(row.role, row.content)
            logger.info(
                "session_history_reloaded",
                session_id=session_id,
                turns=len(rows),
                duration_ms=round((time.perf_counter() - reload_started) * 1000),
                oldest_preview=self._preview_text(rows[-1].content if rows else "", 120),
                newest_preview=self._preview_text(rows[0].content if rows else "", 120),
            )
        except Exception as e:
            logger.warning("session_history_reload_failed", session_id=session_id, error=str(e))

    async def _save_conversation(
        self, session_id: str, user_message: str, response: str
    ) -> None:
        if not self.db_factory:
            return
        try:
            save_started = time.perf_counter()
            async with self.db_factory() as session:
                session.add(
                    Conversation(
                        session_id=session_id, role="user", content=user_message
                    )
                )
                session.add(
                    Conversation(
                        session_id=session_id, role="assistant", content=response
                    )
                )
                await session.commit()
            logger.info(
                "conversation_saved",
                session_id=session_id,
                duration_ms=round((time.perf_counter() - save_started) * 1000),
                user_preview=self._preview_text(user_message, 120),
                response_preview=self._preview_text(response, 120),
            )
        except Exception as e:
            logger.error("save_conversation_failed", session_id=session_id, error=str(e))

    _ROUTING_TRIGGERS = (
        "how long", "how far", "drive to", "driving to", "driving time",
        "get to", "directions to", "commute to", "distance to",
        "how long does it take", "minutes from", "minutes away",
    )

    async def _maybe_get_driving_time(self, user_message: str) -> str:
        """Run a routing lookup if the message is asking about driving time/distance."""
        if not self.config.GOOGLE_MAPS_API_KEY:
            return ""
        lower = user_message.lower()
        if not any(t in lower for t in self._ROUTING_TRIGGERS):
            return ""
        try:
            # Default origin to home if not clearly specified
            from agent.accounts import get_location
            origin = get_location("home") or "home"
            result = await get_driving_time(origin, user_message, self.config.GOOGLE_MAPS_API_KEY)
            if "error" in result:
                return ""
            duration = result.get("duration_in_traffic") or result.get("duration", "unknown")
            distance = result.get("distance", "")
            dest = result.get("destination", "")
            context = (
                f"Routing result: {duration} drive to {dest} ({distance})"
                f" from {result.get('origin', origin)}."
            )
            if "duration_in_traffic" in result:
                context += " (includes live traffic)"
            logger.debug("routing_context_injected", duration=duration, destination=dest[:60])
            return context
        except Exception as e:
            logger.warning("routing_proactive_failed", error=str(e))
            return ""

    _SEARCH_TRIGGERS = (
        "search", "look up", "look it up", "find out", "google",
        "latest", "current", "news", "today", "right now",
        "what's the", "what is the", "how much", "price of",
        "who is", "where is", "when is", "weather",
    )

    async def _maybe_search_web(self, user_message: str) -> str:
        """Run a Brave search if the message looks search-like. Returns formatted context or ''."""
        if not self.config.BRAVE_API_KEY:
            return ""
        lower = user_message.lower()
        if not any(t in lower for t in self._SEARCH_TRIGGERS):
            return ""
        try:
            results = await brave_search(user_message, self.config.BRAVE_API_KEY, count=5)
            if not results:
                return ""
            lines = ["Web search results:"]
            for r in results:
                lines.append(f"- {r['title']}: {r['description']} ({r['url']})")
            logger.debug("web_context_injected", query=user_message[:100], n=len(results))
            return "\n".join(lines)
        except Exception as e:
            logger.warning("web_search_failed", error=str(e))
            return ""

    async def get_status(self) -> dict:
        subsystem_health = self._probe_subsystem_health()
        status = {
            "initialized": self._initialized,
            "subsystems": subsystem_health,
            "working_memory_size": len(self.memory._working),
            "life_context_path": self.config.LIFE_CONTEXT_PATH,
            "default_local_model": self.config.DEFAULT_LOCAL_MODEL,
            "frontier_model": self.config.DEFAULT_FRONTIER_MODEL,
            "telegram_enabled": bool(self.config.TELEGRAM_BOT_TOKEN),
        }
        if hasattr(self, '_scheduler') and self._scheduler:
            status["scheduler"] = self._scheduler.get_status()
        return status
