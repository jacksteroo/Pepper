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

# Postgres NOTIFY channels used to signal cadence-bounded work to the
# reflector process. The daily channel (#39) fires at 23:55 local; the
# weekly + monthly rollup channels (#40) fire on Sunday 23:55 and on
# day-1 00:05 respectively. Payloads are intentionally signal-only —
# a date string in the format `YYYY-MM-DD` (local TZ) — so the LISTEN
# client can never interpret the channel as a content sink.
REFLECTOR_TRIGGER_CHANNEL = "reflector_trigger"
REFLECTOR_WEEKLY_CHANNEL = "reflector_weekly_trigger"
REFLECTOR_MONTHLY_CHANNEL = "reflector_monthly_trigger"

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
        # Epic 04 (#40): weekly rollup trigger. Fires MONDAY 00:15
        # local — *not* Sunday 23:55. The reflector's daily pass
        # (180 s LLM cap, 300 s wall-clock) needs to land Sunday's
        # daily before the weekly query runs. Co-firing them in the
        # same minute would race: the weekly might roll up only six
        # dailies (Mon-Sat) if it dequeued first. 20 minutes after
        # the Sunday daily is comfortable headroom.
        # Payload is the SUNDAY date (today_local minus one day) so
        # the rollup window calculation matches "the week that just
        # ended."
        self._scheduler.add_job(
            self.fire_reflector_weekly_trigger,
            CronTrigger(day_of_week=0, hour=0, minute=15),
            id="reflector_weekly_trigger",
            replace_existing=True,
        )
        # Epic 04 (#40): monthly rollup trigger. Fires day 1 of each
        # month at 00:15 local — 20 minutes after the day-1 daily
        # would have started running (and any in-flight Sunday weekly
        # has finished). Covers the previous month. Payload is
        # signal-only (the day-1 date string).
        self._scheduler.add_job(
            self.fire_reflector_monthly_trigger,
            CronTrigger(day=1, hour=0, minute=15),
            id="reflector_monthly_trigger",
            replace_existing=True,
        )
        # Epic 01 (#21): nightly trace compression. Promotes
        # working → recall (>24h) and recall → archival (>28d).
        # Lives here, NOT inside the reflector — separation of concerns.
        # Idempotent so a re-run after partial failure is safe.
        self._scheduler.add_job(
            self.run_trace_compression,
            CronTrigger(hour=2, minute=15),
            id="trace_compression",
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

        # Epic 06 (#55) — wait short-circuit. If the model called the
        # wait tool during this turn, treat the brief as a chosen
        # non-action (success, not failure). Suppress the send + the
        # save_to_recall, but log the wait into the audit trail and
        # mark _last_brief so the daily slot is consumed (avoids a
        # double-fire if the operator hits /brief/now after).
        wait = self.pepper.waits.consume_latest(session_id)
        if wait is not None:
            logger.info(
                "morning_brief_waited",
                date=today,
                reason_preview=wait.reason[:200],
                until_raw=wait.until_raw,
            )
            await self._audit(
                "morning_brief_waited",
                f"Brief for {today} held back. Reason: {wait.reason[:300]}",
            )
            self._last_brief = datetime.utcnow()
            return brief_text

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
        # Open commitments span all time; #29's default 30-day recency
        # tilt would bury old-but-still-open promises. Disable it here.
        raw = await self.pepper.memory.search_recall(
            "COMMITMENT", limit=20, time_window_days=None
        )
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

    async def run_trace_compression(self) -> dict:
        """Nightly trace compression (#21).

        Idempotent: re-running on the same day produces identical
        results because each tier scan filter excludes already-promoted
        rows.
        """
        if self.pepper.db_factory is None:
            logger.info("trace_compression_skipped", reason="no_db_factory")
            return {"ok": False, "reason": "no_db_factory"}
        try:
            from agent.traces.compression import run_nightly_compression

            result = await run_nightly_compression(self.pepper.db_factory)
            await self._audit(
                "trace_compression",
                f"recall={result['recall'].advanced_to_recall} "
                f"archival={result['archival'].advanced_to_archival}",
            )
            return {
                "ok": True,
                "recall_advanced": result["recall"].advanced_to_recall,
                "archival_advanced": result["archival"].advanced_to_archival,
                "errors": (
                    result["recall"].errors + result["archival"].errors
                ),
            }
        except Exception as exc:
            logger.warning(
                "trace_compression_failed",
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )
            return {"ok": False, "error": type(exc).__name__}

    async def _fire_reflector_notify(
        self, channel: str, audit_kind: str, *, payload_date_offset_days: int = 0
    ) -> bool:
        """Common path: NOTIFY <channel>, '<YYYY-MM-DD>' in local TZ.

        Payload is signal-only. The channel name is interpolated into
        the literal NOTIFY SQL; all `channel` callers below are
        compile-time constants, so there is no user-supplied input
        here. Date payload is generated server-side and escaped for
        the single-quoted literal as defence in depth.

        `payload_date_offset_days` lets the weekly trigger send the
        Sunday-of-the-just-ended-week even though the cron itself
        fires on Monday 00:15 (#40 race-fix). Defaults to 0
        (today's date in local TZ).
        """
        if self.pepper.db_factory is None:
            logger.info("reflector_trigger_skipped", reason="no_db_factory", channel=channel)
            return False
        tz = ZoneInfo(self.config.TIMEZONE)
        when = datetime.now(tz) + timedelta(days=payload_date_offset_days)
        payload = when.strftime("%Y-%m-%d")
        try:
            async with self.pepper.db_factory() as session:
                escaped = payload.replace("'", "''")
                await session.execute(
                    text(f"NOTIFY {channel}, '{escaped}'"),
                )
                await session.commit()
            logger.info(
                "reflector_trigger_fired",
                channel=channel,
                payload=payload,
            )
            await self._audit(audit_kind, payload)
            return True
        except Exception as exc:
            logger.warning(
                "reflector_trigger_failed",
                channel=channel,
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )
            return False

    async def fire_reflector_trigger(self) -> bool:
        """Fire the end-of-day Postgres NOTIFY for the reflector (#39).

        Cron: every day at 23:55 local. Payload = today's date.
        """
        return await self._fire_reflector_notify(
            REFLECTOR_TRIGGER_CHANNEL, "reflector_trigger"
        )

    async def fire_reflector_weekly_trigger(self) -> bool:
        """Fire the Monday-00:15 weekly rollup NOTIFY (#40).

        Cron fires on Monday 00:15 local — *not* Sunday 23:55 — so
        Sunday's daily reflection has time to land first (race-fix
        from #40 review). The payload is therefore yesterday's date
        (Sunday) so `weekly_window_for_payload` reads the Sunday at
        the end of the week the rollup covers.
        """
        return await self._fire_reflector_notify(
            REFLECTOR_WEEKLY_CHANNEL,
            "reflector_weekly_trigger",
            payload_date_offset_days=-1,
        )

    async def fire_reflector_monthly_trigger(self) -> bool:
        """Fire the day-1 00:15 monthly rollup NOTIFY (#40).

        Cron fires on the 1st of the month at 00:15 local. Payload =
        today's date (the 1st), which the rollup interprets as
        "cover the previous month."
        """
        return await self._fire_reflector_notify(
            REFLECTOR_MONTHLY_CHANNEL, "reflector_monthly_trigger"
        )

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
