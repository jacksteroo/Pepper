"""Pepper proactive scheduler.

Manages timed jobs: morning brief, commitment check, weekly review, and memory
compression. All content-generation jobs now route through pepper.chat() with
heavy=True so the skill system guides the response — no hand-rolled formatters.
"""

import structlog
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from agent.briefs import CommitmentExtractor
from agent.models import AuditLog

logger = structlog.get_logger()


class PepperScheduler:
    def __init__(self, pepper_core, config, telegram_bot=None):
        self.pepper = pepper_core
        self.config = config
        self.bot = telegram_bot
        self.extractor = CommitmentExtractor(llm_client=getattr(pepper_core, 'llm', None))
        self._scheduler = AsyncIOScheduler()
        self._last_brief: datetime = None
        self._last_review: datetime = None

    def start(self):
        """Register all jobs and start the scheduler."""
        self._scheduler.add_job(
            self.generate_morning_brief,
            CronTrigger(hour=self.config.MORNING_BRIEF_HOUR, minute=self.config.MORNING_BRIEF_MINUTE),
            id="morning_brief",
            replace_existing=True,
        )
        self._scheduler.add_job(
            self.check_commitments,
            CronTrigger(hour=12, minute=0),
            id="commitment_check",
            replace_existing=True,
        )
        self._scheduler.add_job(
            self.generate_weekly_review,
            CronTrigger(
                day_of_week=self.config.WEEKLY_REVIEW_DAY,
                hour=self.config.WEEKLY_REVIEW_HOUR,
                minute=0,
            ),
            id="weekly_review",
            replace_existing=True,
        )
        self._scheduler.add_job(
            self.run_memory_compression,
            CronTrigger(day_of_week=6, hour=2, minute=0),
            id="memory_compression",
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info("scheduler_started", jobs=[j.id for j in self._scheduler.get_jobs()])

    def stop(self):
        self._scheduler.shutdown(wait=False)
        logger.info("scheduler_stopped")

    # ── Jobs ──────────────────────────────────────────────────────────────────

    async def generate_morning_brief(self) -> str:
        """Trigger a morning brief via pepper.chat().

        The morning_brief skill injects the workflow into the system prompt.
        heavy=True ensures all data sources (calendar, memory, email) are fetched.

        Each run uses a date-stamped session ID and isolated=True so no scheduler
        turns ever touch the shared working-memory deque or bleed into user sessions.
        """
        tz = ZoneInfo(self.config.TIMEZONE)
        today = datetime.now(tz).strftime("%A, %B %-d, %Y")
        logger.info("generating_morning_brief", date=today)

        session_id = f"scheduler_morning_brief_{datetime.now(tz).strftime('%Y%m%d')}"
        brief_text = await self.pepper.chat(
            f"Generate my morning brief for {today}.",
            session_id=session_id,
            heavy=True,
            isolated=True,
        )

        # Guaranteed persistence: save here rather than relying on the model to
        # follow the skill's save_memory instruction (skills are guidance, not
        # mandates). The morning_brief skill no longer includes a save_memory step
        # so there is no duplication.
        await self.pepper.memory.save_to_recall(
            f"MORNING BRIEF ({today}): {brief_text[:500]}",
            importance=0.6,
        )
        await self._send(brief_text)
        await self._audit("morning_brief_sent", f"Brief for {today}")
        self._last_brief = datetime.utcnow()
        logger.info("morning_brief_sent", date=today)
        return brief_text

    async def check_commitments(self) -> str:
        """Surface open commitments via pepper.chat().

        The notification decision is made from structured recall-memory results,
        not by parsing the LLM's free-form response — this avoids false triggers
        when the model paraphrases a "nothing to do" answer in an unexpected way.
        """
        logger.info("checking_commitments")

        # Query recall memory for commitment entries and filter out resolved ones.
        # If memory is unavailable (no DB/embeddings), fall through and notify anyway
        # rather than silently drop a potential reminder.
        raw = await self.pepper.memory.search_recall("COMMITMENT", limit=20)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        open_items = []
        for item in raw:
            content = item.get("content", "").upper()
            if not content.startswith("COMMITMENT:"):
                continue
            if content.startswith("COMMITMENT: [RESOLVED]"):
                continue
            # Skip commitments recorded in the last 48 hours — they don't need a
            # reminder yet. Items with no parseable timestamp are included so we
            # never silently drop a reminder.
            created_raw = item.get("created_at")
            if created_raw:
                try:
                    created = datetime.fromisoformat(created_raw)
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created > cutoff:
                        continue
                except ValueError:
                    pass
            open_items.append(item)

        if not open_items:
            logger.info("commitment_check_no_open_items")
            await self._audit("commitment_check", "no open commitments")
            return ""

        session_id = f"scheduler_commitment_check_{datetime.now(ZoneInfo(self.config.TIMEZONE)).strftime('%Y%m%d_%H')}"
        response = await self.pepper.chat(
            "Commitment check: list any open commitments older than 48 hours. "
            "Skip anything already resolved.",
            session_id=session_id,
            heavy=True,
            isolated=True,
        )

        await self._send(response)
        await self._audit("commitment_check", response[:200])
        return response

    async def generate_weekly_review(self) -> str:
        """Trigger a weekly review via pepper.chat().

        The weekly_review skill guides the response: this week's highlights,
        open loops, next week's calendar, and forward-looking priorities.

        Each run uses a week-stamped session ID and isolated=True so no scheduler
        turns ever touch the shared working-memory deque or bleed into user sessions.
        """
        tz = ZoneInfo(self.config.TIMEZONE)
        week_label = datetime.now(tz).strftime("Week of %B %-d, %Y")
        logger.info("generating_weekly_review", week=week_label)

        session_id = f"scheduler_weekly_review_{datetime.now(tz).strftime('%Y_%W')}"
        review_text = await self.pepper.chat(
            f"Generate my weekly review for {week_label}.",
            session_id=session_id,
            heavy=True,
            isolated=True,
        )

        # Guaranteed persistence: save here rather than relying on the model to
        # follow the skill's save_memory instruction (skills are guidance, not
        # mandates). The weekly_review skill no longer includes a save_memory step
        # so there is no duplication.
        await self.pepper.memory.save_to_recall(
            f"WEEKLY REVIEW ({week_label}): {review_text[:500]}",
            importance=0.7,
        )
        await self._send(review_text)
        await self._audit("weekly_review_sent", week_label)
        self._last_review = datetime.utcnow()
        logger.info("weekly_review_sent", week=week_label)
        return review_text

    async def run_memory_compression(self) -> dict:
        logger.info("running_memory_compression")
        result = await self.pepper.memory.compress_to_archival()
        await self._audit("memory_compression", str(result))
        return result

    def get_status(self) -> dict:
        return {
            "jobs": [j.id for j in self._scheduler.get_jobs()],
            "last_brief": self._last_brief.isoformat() if self._last_brief else None,
            "last_review": self._last_review.isoformat() if self._last_review else None,
            "running": self._scheduler.running,
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _send(self, text: str) -> None:
        if self.bot:
            try:
                await self.bot.send_message(text)
            except Exception as e:
                logger.warning("telegram_send_failed", error=str(e))
        else:
            logger.info("brief_output", text=text[:200])

    async def _audit(self, event_type: str, details: str = "") -> None:
        if not self.pepper.db_factory:
            return
        try:
            async with self.pepper.db_factory() as session:
                session.add(AuditLog(event_type=event_type, details=details))
                await session.commit()
        except Exception as e:
            logger.warning("audit_log_failed", error=str(e))
