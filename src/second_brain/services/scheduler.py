"""Proactive scheduler — APScheduler setup, job registration, main scheduler job.

Runs periodic jobs to surface relevant nudges, sync calendar, check for
upcoming meetings, and retry failed operations.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import and_
from sqlalchemy.orm import sessionmaker

from second_brain.config import get_config_int
from second_brain.models.calendar_event import CalendarEvent
from second_brain.models.entry import Entry
from second_brain.models.nudge import NudgeHistory
from second_brain.prompts.scheduler_reasoning import (
    SCHEDULER_SYSTEM_PROMPT,
    SCHEDULER_USER_PROMPT_TEMPLATE,
    SchedulerDecision,
)
from second_brain.services.anthropic_client import AnthropicClient

logger = logging.getLogger(__name__)


class SchedulerService:
    """APScheduler-based proactive scheduler.

    Manages cron jobs for:
    - Main scheduler reasoning (every N hours during active hours)
    - Calendar sync (every 30 minutes)
    - Meeting check (every 5 minutes)
    - Retry failed enrichments/transcriptions (every 10 minutes)
    """

    def __init__(self, services: dict) -> None:
        self.services = services
        self.session_factory: sessionmaker = services["db_session_factory"]
        self.anthropic_client: AnthropicClient | None = services.get("anthropic_client")
        self.scheduler = AsyncIOScheduler()
        self._pending_escalations: list = []

    def setup_scheduler(self, bot_data: dict) -> None:
        """Register all cron jobs and start the scheduler.

        Args:
            bot_data: The bot's data dict, giving jobs access to services
                and the ability to send Telegram messages.
        """
        self.bot_data = bot_data

        # Read intervals from config
        with self.session_factory() as session:
            scheduler_hours = get_config_int(session, "scheduler_interval_hours") or 2
            start_hour = get_config_int(session, "scheduler_start_hour") or 8
            end_hour = get_config_int(session, "scheduler_end_hour") or 21
            calendar_minutes = (
                get_config_int(session, "calendar_sync_interval_minutes") or 30
            )
            meeting_minutes = (
                get_config_int(session, "meeting_check_interval_minutes") or 5
            )
            retry_minutes = (
                get_config_int(session, "enrichment_retry_interval_minutes") or 10
            )

        # Main scheduler: every N hours during active hours
        self.scheduler.add_job(
            self._run_main_scheduler,
            "cron",
            hour=f"{start_hour}-{end_hour}/{scheduler_hours}",
            id="main_scheduler",
            replace_existing=True,
        )

        # Calendar sync
        self.scheduler.add_job(
            self._run_calendar_sync,
            "interval",
            minutes=calendar_minutes,
            id="calendar_sync",
            replace_existing=True,
        )

        # Meeting check (pre-meeting briefs)
        self.scheduler.add_job(
            self._run_meeting_check,
            "interval",
            minutes=meeting_minutes,
            id="meeting_check",
            replace_existing=True,
        )

        # Retry failed enrichments / transcriptions
        self.scheduler.add_job(
            self._run_retries,
            "interval",
            minutes=retry_minutes,
            id="retry_jobs",
            replace_existing=True,
        )

        self.scheduler.start()
        logger.info(
            "Scheduler started: main every %dh (%d-%d), "
            "calendar every %dm, meetings every %dm, retries every %dm",
            scheduler_hours,
            start_hour,
            end_hour,
            calendar_minutes,
            meeting_minutes,
            retry_minutes,
        )

    async def _run_main_scheduler(self) -> None:
        """Main scheduler job: pull data, pass to Sonnet, optionally send nudge."""
        if not self.anthropic_client:
            logger.debug("Main scheduler skipped: no anthropic_client")
            return

        logger.info("Main scheduler running")

        try:
            now = datetime.now(timezone.utc)

            with self.session_factory() as session:
                # 1. Pull open loops
                open_loops = (
                    session.query(Entry)
                    .filter(
                        Entry.is_open_loop.is_(True),
                        Entry.status == "open",
                    )
                    .all()
                )

                # 2. Pull recent entries (last 7 days)
                seven_days_ago = now - timedelta(days=7)
                recent_entries = (
                    session.query(Entry)
                    .filter(Entry.created_at >= seven_days_ago)
                    .order_by(Entry.created_at.desc())
                    .all()
                )

                # 3. Pull upcoming calendar events (next 24 hours)
                tomorrow = now + timedelta(hours=24)
                upcoming_events = (
                    session.query(CalendarEvent)
                    .filter(
                        CalendarEvent.start_time >= now,
                        CalendarEvent.start_time <= tomorrow,
                    )
                    .order_by(CalendarEvent.start_time)
                    .all()
                )

            # Format data for the prompt
            open_loops_text = self._format_open_loops(open_loops)
            recent_text = self._format_recent_entries(recent_entries)
            events_text = self._format_calendar_events(upcoming_events)

            user_prompt = SCHEDULER_USER_PROMPT_TEMPLATE.format(
                current_time=now.strftime("%Y-%m-%d %H:%M UTC"),
                open_loops=open_loops_text or "(none)",
                recent_entries=recent_text or "(none)",
                calendar_events=events_text or "(none)",
            )

            # 4. Pass to Sonnet with strict filtering
            decision: SchedulerDecision = self.anthropic_client.call_sonnet(
                system_prompt=SCHEDULER_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                response_model=SchedulerDecision,
            )

            # 5. Most runs produce no message
            if not decision.should_nudge:
                logger.info("Main scheduler: no nudge needed")
                return

            # 6. Fire a nudge
            logger.info(
                "Main scheduler: nudge triggered type=%s entry_id=%s",
                decision.nudge_type,
                decision.entry_id,
            )
            await self._send_nudge(
                entry_id=decision.entry_id,
                nudge_type=decision.nudge_type or "open_loop",
                message=decision.message or "",
                escalation_level=decision.escalation_level,
            )

        except Exception:
            logger.exception("Main scheduler job failed")

    async def _run_calendar_sync(self) -> None:
        """Calendar sync job — delegates to CalendarSyncService if available."""
        calendar_sync = self.services.get("calendar_sync")
        if not calendar_sync:
            logger.debug("Calendar sync skipped: service not available")
            return

        try:
            logger.info("Calendar sync running")
            # CalendarSyncService.sync() will be implemented by T14
            if hasattr(calendar_sync, "sync"):
                await calendar_sync.sync()
        except Exception:
            logger.exception("Calendar sync job failed")

    async def _run_meeting_check(self) -> None:
        """Meeting check job — sends pre-meeting briefs for upcoming meetings.

        Delegates to the meeting brief service (T15) if available.
        """
        meeting_brief = self.services.get("meeting_brief")
        if not meeting_brief:
            logger.debug("Meeting check skipped: service not available")
            return

        try:
            logger.info("Meeting check running")
            if hasattr(meeting_brief, "check_upcoming_meetings"):
                await meeting_brief.check_upcoming_meetings()
        except Exception:
            logger.exception("Meeting check job failed")

    async def _run_retries(self) -> None:
        """Retry job — retries failed enrichments and transcriptions.

        Delegates to the retry manager service (T17) if available.
        """
        retry_manager = self.services.get("retry_manager")
        if not retry_manager:
            logger.debug("Retry job skipped: service not available")
            return

        try:
            logger.info("Retry job running")
            if hasattr(retry_manager, "retry_pending"):
                await retry_manager.retry_pending()
        except Exception:
            logger.exception("Retry job failed")

    async def _send_nudge(
        self,
        entry_id: int | None,
        nudge_type: str,
        message: str,
        escalation_level: int = 1,
    ) -> None:
        """Create a nudge via NudgeManager and send it via Telegram.

        If the NudgeManager or Telegram bot is not available, just logs.
        """
        nudge_manager = self.services.get("nudge_manager")
        if not nudge_manager:
            logger.warning("Cannot send nudge: nudge_manager not available")
            return

        nudge, formatted_text, keyboard = nudge_manager.create_nudge(
            entry_id=entry_id,
            nudge_type=nudge_type,
            message=message,
            escalation_level=escalation_level,
        )

        # Send via Telegram if bot is available
        bot = self.bot_data.get("bot")
        chat_id = self.bot_data.get("chat_id")
        if bot and chat_id:
            try:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup

                inline_keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                btn["text"], callback_data=btn["callback_data"]
                            )
                            for btn in row
                        ]
                        for row in keyboard
                    ]
                )
                sent = await bot.send_message(
                    chat_id=chat_id,
                    text=formatted_text,
                    reply_markup=inline_keyboard,
                )
                nudge_manager.set_telegram_message_id(nudge.id, sent.message_id)
            except Exception:
                logger.exception("Failed to send nudge via Telegram")
        else:
            logger.info(
                "Nudge created but not sent (no bot/chat_id): %s", formatted_text
            )

    async def run_escalation_check(self) -> None:
        """Check for nudges that need escalation and send them."""
        nudge_manager = self.services.get("nudge_manager")
        if not nudge_manager:
            return

        try:
            escalated = nudge_manager.check_escalations()
            for nudge, formatted, keyboard in escalated:
                await self._send_nudge_message(nudge, formatted, keyboard)

            # Also check snoozed nudges that have expired
            snoozed_due = nudge_manager.get_snoozed_due()
            for old_nudge in snoozed_due:
                # Re-nudge at level 1
                await self._send_nudge(
                    entry_id=old_nudge.entry_id,
                    nudge_type=old_nudge.nudge_type,
                    message=old_nudge.message_text,
                    escalation_level=1,
                )
        except Exception:
            logger.exception("Escalation check failed")

    async def _send_nudge_message(
        self,
        nudge: object,
        formatted_text: str,
        keyboard: list[list[dict]],
    ) -> None:
        """Send an already-created nudge message via Telegram."""
        bot = self.bot_data.get("bot") if hasattr(self, "bot_data") else None
        chat_id = self.bot_data.get("chat_id") if hasattr(self, "bot_data") else None

        if bot and chat_id:
            try:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup

                inline_keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                btn["text"], callback_data=btn["callback_data"]
                            )
                            for btn in row
                        ]
                        for row in keyboard
                    ]
                )
                sent = await bot.send_message(
                    chat_id=chat_id,
                    text=formatted_text,
                    reply_markup=inline_keyboard,
                )
                nudge_manager = self.services.get("nudge_manager")
                if nudge_manager and hasattr(nudge, "id"):
                    nudge_manager.set_telegram_message_id(nudge.id, sent.message_id)
            except Exception:
                logger.exception("Failed to send escalated nudge via Telegram")

    def shutdown(self) -> None:
        """Gracefully shut down the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler shut down")

    # --- Formatting helpers ---

    @staticmethod
    def _format_open_loops(entries: list[Entry]) -> str:
        lines = []
        for e in entries:
            text = e.clean_text or e.raw_text
            snippet = text[:150] if text else "(empty)"
            follow_up = (
                f" (follow-up: {e.follow_up_date.isoformat()})"
                if e.follow_up_date
                else ""
            )
            created = e.created_at.strftime("%Y-%m-%d")
            lines.append(
                f"- [Entry {e.id}, {created}]{follow_up}: {snippet}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_recent_entries(entries: list[Entry]) -> str:
        lines = []
        for e in entries:
            text = e.clean_text or e.raw_text
            snippet = text[:150] if text else "(empty)"
            created = e.created_at.strftime("%Y-%m-%d")
            lines.append(
                f"- [Entry {e.id}, {e.entry_type}, {created}]: {snippet}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_calendar_events(events: list[CalendarEvent]) -> str:
        lines = []
        for ev in events:
            start = ev.start_time.strftime("%Y-%m-%d %H:%M")
            attendees = ""
            if ev.attendees:
                try:
                    attendee_list = json.loads(ev.attendees)
                    names = [a.get("name", a.get("email", "")) for a in attendee_list]
                    attendees = f" (with: {', '.join(names)})"
                except (json.JSONDecodeError, TypeError):
                    pass
            lines.append(f"- [{start}] {ev.title}{attendees}")
        return "\n".join(lines)
