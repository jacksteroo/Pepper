"""Pepper proactive scheduler.

Manages timed jobs: morning brief, commitment check, weekly review, and memory
compression. All content-generation jobs now route through pepper.chat() with
heavy=True so the skill system guides the response — no hand-rolled formatters.

Epic 01 (#23): every `pepper.chat()` call from this module carries
`trigger_source=TriggerSource.SCHEDULER` plus the job name, so #22's trace
emitter records the turn under the correct provenance. A separate
`reflector_trigger` Postgres NOTIFY fires once per day at 23:55 — #39's
reflector LISTENs on that channel.
"""

import structlog
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text
from agent.briefs import CommitmentExtractor
from agent.commitment_followup import CommitmentFollowup
from agent.models import AuditLog
from agent.traces import TriggerSource

# Postgres NOTIFY channel used to signal end-of-day to the reflector
# process (#39). The payload is intentionally signal-only — no trace
# contents — so the LISTEN client can never interpret the channel as
# a content sink.
REFLECTOR_TRIGGER_CHANNEL = "reflector_trigger"

logger = structlog.get_logger()


class PepperScheduler:
    def __init__(self, pepper_core, config, telegram_bot=None):
        self.pepper = pepper_core
        self.config = config
        self.bot = telegram_bot
        self.extractor = CommitmentExtractor(llm_client=getattr(pepper_core, 'llm', None))
        # Persistent instance so _surfaced set accumulates across scheduler runs
        # within the same process and prevents same-day re-nags.
        self._commitment_followup = CommitmentFollowup(pepper_core)
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
        # Phase 6.6: periodic capability re-probe.
        # Covers the case where a user grants Full Disk Access, configures a
        # credential file, or installs WhatsApp Desktop without restarting Pepper.
        self._scheduler.add_job(
            self.refresh_capabilities,
            CronTrigger(minute="*/15"),
            id="capability_refresh",
            replace_existing=True,
        )
        # Phase 6.7: surface unresolved commitments at the relevant time.
        self._scheduler.add_job(
            self.run_commitment_followup,
            CronTrigger(hour="8,17,22", minute=5),
            id="commitment_followup",
            replace_existing=True,
        )
        # Epic 01 (#23): end-of-day signal for the reflector (#39). Fires
        # one Postgres NOTIFY on the documented channel at 23:55. Payload
        # is signal-only — the channel is not a content sink.
        self._scheduler.add_job(
            self.fire_reflector_trigger,
            CronTrigger(hour=23, minute=55),
            id="reflector_trigger",
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
            trigger_source=TriggerSource.SCHEDULER,
            scheduler_job_name="morning_brief",
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
            trigger_source=TriggerSource.SCHEDULER,
            scheduler_job_name="commitment_check",
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
            trigger_source=TriggerSource.SCHEDULER,
            scheduler_job_name="weekly_review",
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

    async def refresh_capabilities(self) -> dict:
        """Phase 6.6: Re-probe all data sources so registry reflects current state."""
        try:
            await self.pepper._capability_registry.refresh(self.config)
            available = self.pepper._capability_registry.get_available_sources()
            logger.info("capability_refresh_done", available=available)
            return {"ok": True, "available": available}
        except Exception as e:
            logger.warning("capability_refresh_failed", error=str(e))
            return {"ok": False, "error": str(e)}

    async def run_commitment_followup(self) -> str:
        """Phase 6.7: surface unresolved commitments at the time they're due.

        Re-surfaces commitments whose follow-up time has arrived (today/EOD/tonight
        mapped to the scheduler's 3 daily slots). Already-resolved ones are skipped.
        """
        due = await self._commitment_followup.find_due_commitments()
        if not due:
            logger.info("commitment_followup_none_due")
            return ""
        message = self._commitment_followup.format_followup_message(due)
        await self._send(message)
        await self._audit("commitment_followup", f"{len(due)} items")
        logger.info("commitment_followup_sent", count=len(due))
        return message

    async def fire_reflector_trigger(self) -> bool:
        """Fire the end-of-day Postgres NOTIFY for the reflector (#39).

        Payload format: `<YYYY-MM-DD>` in the local timezone — a fixed,
        signal-only string. The reflector LISTENs on this channel and
        kicks off its daily window query. Trace contents are NEVER
        included in the payload. Returns True on success, False on
        error (job is best-effort and never raises).
        """
        if self.pepper.db_factory is None:
            logger.info("reflector_trigger_skipped", reason="no_db_factory")
            return False
        tz = ZoneInfo(self.config.TIMEZONE)
        payload = datetime.now(tz).strftime("%Y-%m-%d")
        try:
            async with self.pepper.db_factory() as session:
                # NOTIFY needs literal SQL — no bind parameters allowed
                # in NOTIFY's payload position. The payload is a fixed-
                # format date string we generated ourselves; no user
                # input flows into this SQL.
                escaped = payload.replace("'", "''")
                await session.execute(
                    text(f"NOTIFY {REFLECTOR_TRIGGER_CHANNEL}, '{escaped}'"),
                )
                await session.commit()
            logger.info(
                "reflector_trigger_fired",
                channel=REFLECTOR_TRIGGER_CHANNEL,
                payload=payload,
            )
            await self._audit("reflector_trigger", payload)
            return True
        except Exception as exc:
            logger.warning(
                "reflector_trigger_failed",
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )
            return False

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
