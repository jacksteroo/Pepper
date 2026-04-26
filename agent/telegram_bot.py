import asyncio
import random
import re
import structlog
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ChatAction
from agent.config import Settings
from agent.models import AuditLog

logger = structlog.get_logger()

_THINKING_STARS = ["·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢", "·"]  # forward then reverse
_CURSORS = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]
_CURSOR_INTERVAL = 0.5

_FALLBACK_ACKS = [
    "Got it, working on that now.",
    "On it, give me a moment.",
    "Right away.",
    "Sure, let me get that sorted.",
]

_ACK_SYSTEM_PROMPT = (
    "You are Pepper, a sharp AI chief of staff. You are a single AI — never say 'we', 'our', "
    "'my team', or imply you are a group. Always use first-person singular (I, me, my). "
    "Write a brief conversational acknowledgment (2 sentences max) of what the user is asking. "
    "First sentence: rephrase what they want in plain language so they know you understood. "
    "Second sentence: say you're working on it — natural, not robotic. "
    "No emojis. No bullet points. No 'certainly' or 'of course'. Keep it under 30 words total."
)

class JARViSTelegramBot:
    def __init__(self, token: str, pepper_core, config: Settings):
        self.token = token
        self.pepper = pepper_core
        self.config = config
        self._allowed_ids = config.get_allowed_telegram_user_ids()
        self._app: Application = None
        self._bot: Bot = None

    async def setup(self) -> None:
        """Build the Application and register all handlers."""
        self._app = Application.builder().token(self.token).build()
        self._bot = self._app.bot

        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("brief", self._cmd_brief))
        self._app.add_handler(CommandHandler("review", self._cmd_review))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )
        self._app.add_error_handler(self._error_handler)

    async def start(self) -> None:
        """Start polling. Runs until stop() is called."""
        await self.setup()
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("telegram_bot_started")

    async def stop(self) -> None:
        if self._app:
            if self._app.updater and self._app.updater.running:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        logger.info("telegram_bot_stopped")

    async def send_message(self, text: str, parse_mode: str = "Markdown") -> None:
        """Push a message to all allowed users (or just first one for single-user setup)."""
        if not self._bot:
            logger.warning("telegram_send_skipped", reason="bot not initialized")
            return
        # For single-user setup, TELEGRAM_ALLOWED_USER_IDS should have one entry
        if self._allowed_ids:
            for user_id in self._allowed_ids:
                try:
                    await self._send_long(user_id, text, parse_mode)
                except Exception as e:
                    logger.error("telegram_push_failed", user_id=user_id, error=str(e))
        else:
            logger.warning("telegram_no_recipients", reason="TELEGRAM_ALLOWED_USER_IDS not set")

    # ─── Auth ──────────────────────────────────────────────────────────────

    def _is_allowed(self, user_id: int) -> bool:
        if not self._allowed_ids:
            return True  # no restriction set — single-user assumption
        return user_id in self._allowed_ids

    async def _check_auth(self, update: Update) -> bool:
        user_id = update.effective_user.id
        if not self._is_allowed(user_id):
            await update.message.reply_text("Unauthorized.")
            await self._audit(f"unauthorized_access user_id={user_id}")
            logger.warning("unauthorized_telegram_access", user_id=user_id)
            return False
        return True

    # ─── Command Handlers ──────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        await update.message.reply_text(
            "🤖 *Pepper is Online.*\n\n"
            "I'm your personal AI chief of staff. I know your life context and I'm here to help you navigate it.\n\n"
            "Ask me anything — about your life, your relationships, what you should focus on, or what you're avoiding.\n\n"
            "Use /help to see available commands.",
            parse_mode="Markdown",
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        await update.message.reply_text(
            "*Commands for Pepper*\n\n"
            "/brief — Generate morning brief now\n"
            "/review — Generate weekly review now\n"
            "/status — System status (subsystems, memory, scheduler)\n"
            "/help — Show this message\n\n"
            "Or just send any message to talk to Pepper.",
            parse_mode="Markdown",
        )

    async def _cmd_brief(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        try:
            # Access scheduler through pepper
            scheduler = getattr(self.pepper, '_scheduler', None)
            if scheduler:
                brief = await scheduler.generate_morning_brief()
            else:
                brief = "Scheduler not initialized yet."
            await self._send_long(update.effective_chat.id, brief)
        except Exception as e:
            logger.error("cmd_brief_failed", error=str(e))
            await update.message.reply_text("Failed to generate brief. Check logs.")

    async def _cmd_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        try:
            scheduler = getattr(self.pepper, '_scheduler', None)
            if scheduler:
                review = await scheduler.generate_weekly_review()
            else:
                review = "Scheduler not initialized yet."
            await self._send_long(update.effective_chat.id, review)
        except Exception as e:
            logger.error("cmd_review_failed", error=str(e))
            await update.message.reply_text("Failed to generate review. Check logs.")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        try:
            s = await self.pepper.get_status()
            lines = ["*Pepper Status*\n"]
            lines.append(f"{'✅' if s.get('initialized') else '❌'} Core initialized")

            subsystems = s.get("subsystems", {})
            if subsystems:
                lines.append("\n*Subsystems:*")
                for name, health in subsystems.items():
                    icon = "✅" if health == "ok" else ("⚠️" if health == "degraded" else "❌")
                    lines.append(f"{icon} {name}: {health}")

            sched = s.get("scheduler", {})
            if sched:
                lines.append(f"\n*Scheduler:* {'running' if sched.get('running') else 'stopped'}")
                if sched.get("last_brief"):
                    lines.append(f"Last brief: {sched['last_brief'][:16]}")

            lines.append(f"\n*Working memory:* {s.get('working_memory_size', 0)} messages")
            lines.append(f"*Local model:* {s.get('default_local_model', 'unknown')}")

            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            logger.error("cmd_status_failed", error=str(e))
            await update.message.reply_text("Failed to fetch status.")

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Global error handler — log the exception so it doesn't surface as an unhandled crash."""
        logger.error(
            "telegram_unhandled_error",
            error=str(context.error),
            update=str(update)[:200] if update else None,
        )

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Guard against updates where message or text is None (e.g. edited messages,
        # channel posts, or inline-query results that slip through the TEXT filter).
        if update.message is None or update.message.text is None:
            logger.debug(
                "telegram_non_text_update_skipped",
                update_id=getattr(update, "update_id", None),
            )
            return
        if not await self._check_auth(update):
            return
        user_message = update.message.text
        session_id = str(update.effective_user.id)
        logger.info("telegram_in", user_id=session_id, text=user_message[:300])

        heavy, reason = self.pepper.decide_query_depth(user_message)
        logger.debug("telegram_query_depth", heavy=heavy, reason=reason, text=user_message[:80])
        chat_task = asyncio.create_task(
            self.pepper.chat(user_message, session_id, heavy=heavy, channel="Telegram")
        )

        if heavy:
            # Heavy query — first send a context-aware ack that regurgitates
            # the request so the user knows we understood, then show a spinner
            # while data is being fetched and reasoned over.
            try:
                ack_result = await asyncio.wait_for(
                    self.pepper.llm.chat(
                        [
                            {"role": "system", "content": _ACK_SYSTEM_PROMPT},
                            {"role": "user", "content": user_message},
                        ]
                    ),
                    timeout=4,
                )
                ack_text = (ack_result.get("content") or "").strip()
                if not ack_text:
                    raise ValueError("empty ack")
            except Exception:
                ack_text = random.choice(_FALLBACK_ACKS)

            await self._stream_response(update.effective_chat.id, ack_text)

            status_msg = [None]
            status_msg[0] = await update.message.reply_text(
                rf"`{_THINKING_STARS[0]}` _Thinking\.\.\._",
                parse_mode="MarkdownV2",
            )

            async def _animate():
                frame = 1
                while True:
                    await asyncio.sleep(0.25)
                    star = _THINKING_STARS[frame % len(_THINKING_STARS)]
                    try:
                        await status_msg[0].edit_text(
                            rf"`{star}` _Thinking\.\.\._",
                            parse_mode="MarkdownV2",
                        )
                    except Exception:
                        pass
                    frame += 1
                    if frame % 8 == 0:
                        try:
                            await context.bot.send_chat_action(
                                chat_id=update.effective_chat.id, action=ChatAction.TYPING
                            )
                        except Exception:
                            pass

            animator = asyncio.create_task(_animate())
            try:
                response = await chat_task
                logger.info("telegram_out", user_id=session_id, text=response[:300])
                try:
                    await status_msg[0].delete()
                except Exception:
                    pass
                if not response:
                    response = "I wasn't able to generate a response. Please try again."
                await self._render_response(update.effective_chat.id, response)
            except Exception as e:
                logger.error("message_handler_failed", error=str(e), exc_info=True)
                try:
                    await status_msg[0].delete()
                except Exception:
                    pass
                await update.message.reply_text("Something went wrong on my end. Please try again.")
            finally:
                chat_task.cancel()
                animator.cancel()
        else:
            # Simple query — stream the answer directly, no spinner
            try:
                response = await chat_task
                logger.info("telegram_out", user_id=session_id, text=response[:300])
                if not response:
                    response = "I wasn't able to generate a response. Please try again."
                await self._render_response(update.effective_chat.id, response)
            except Exception as e:
                logger.error("message_handler_failed", error=str(e), exc_info=True)
                await update.message.reply_text("Something went wrong on my end. Please try again.")
            finally:
                chat_task.cancel()

    # ─── Helpers ───────────────────────────────────────────────────────────

    async def _send_image(self, chat_id: int, url: str) -> bool:
        """Send a single image by URL. Returns True on success."""
        try:
            await self._bot.send_photo(chat_id=chat_id, photo=url)
            logger.info("telegram_photo_sent", url=url)
            return True
        except Exception as e:
            logger.warning("telegram_photo_failed", url=url, error=str(e))
            return False

    async def _render_response(self, chat_id: int, text: str) -> None:
        """Render a response, extracting any [IMAGE:url] markers and sending them as photos."""
        image_pattern = re.compile(r"\[IMAGE:([^\]]+)\]")
        images = image_pattern.findall(text)
        clean_text = image_pattern.sub("", text).strip()

        for url in images:
            await self._send_image(chat_id, url.strip())

        if clean_text:
            await self._stream_response(chat_id, clean_text)

    async def _send_long(self, chat_id, text: str, parse_mode: str = "Markdown") -> None:
        """Send text, splitting into chunks if > 4096 chars (Telegram limit)."""
        if not text:
            return
        chunk_size = 4096
        for i in range(0, len(text), chunk_size):
            chunk = text[i:i + chunk_size]
            try:
                await self._bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)
            except Exception:
                # Fallback: send without markdown if parse fails
                await self._bot.send_message(chat_id=chat_id, text=chunk)

    async def _stream_response(self, chat_id: int, text: str) -> None:
        """Sentence-by-sentence reveal with spinning braille cursor pause between each."""
        paragraphs = [p.strip() for p in re.split(r'\n{2,}', text.strip()) if p.strip()]
        if not paragraphs:
            return

        cursor_frame = 0

        for para in paragraphs:
            if len(para) > 4000:
                await self._send_long(chat_id, para)
                continue

            # Find sentence cut points within the original para (preserves newlines/formatting)
            cut_points = [m.end() for m in re.finditer(r'[.!?](?=\s|$)', para)]
            if not cut_points or cut_points[-1] < len(para):
                cut_points.append(len(para))

            msg = await self._bot.send_message(chat_id=chat_id, text=_CURSORS[0])
            prev_end = 0

            for end_pos in cut_points:
                accumulated = para[:end_pos]
                sentence_len = end_pos - prev_end
                prev_end = end_pos

                pause = max(1.0, min(3.0, sentence_len / 25))
                steps = round(pause / _CURSOR_INTERVAL)

                try:
                    await self._bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg.message_id,
                        text=accumulated + " " + _CURSORS[cursor_frame % len(_CURSORS)],
                    )
                except Exception:
                    pass

                for _ in range(steps):
                    await asyncio.sleep(_CURSOR_INTERVAL)
                    cursor_frame += 1
                    try:
                        await self._bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg.message_id,
                            text=accumulated + " " + _CURSORS[cursor_frame % len(_CURSORS)],
                        )
                    except Exception:
                        pass

            try:
                await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg.message_id,
                    text=para,
                    parse_mode="Markdown",
                )
            except Exception:
                try:
                    await self._bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg.message_id,
                        text=para,
                    )
                except Exception:
                    pass

    async def _audit(self, details: str) -> None:
        if not self.pepper.db_factory:
            return
        try:
            async with self.pepper.db_factory() as session:
                session.add(AuditLog(event_type="telegram_event", details=details))
                await session.commit()
        except Exception:
            pass
