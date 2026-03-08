"""Personality service — random personality messages and daily end-of-day summaries."""

import logging
import random
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import sessionmaker
from zoneinfo import ZoneInfo

from second_brain.bot.formatting import format_daily_summary_blocks
from second_brain.config import get_config, get_config_bool, get_config_int
from second_brain.models.calendar_event import CalendarEvent
from second_brain.models.entry import Entry
from second_brain.models.entity import Entity, entry_entities
from second_brain.models.nudge import NudgeHistory
from second_brain.prompts.daily_summary import (
    DAILY_SUMMARY_SYSTEM_PROMPT,
    DailySummaryResponse,
    build_daily_summary_user_prompt,
)
from second_brain.prompts.personality import (
    PERSONALITY_SYSTEM_PROMPT,
    PersonalityMessage,
    build_personality_user_prompt,
)
from second_brain.services.anthropic_client import AnthropicClient
from second_brain.utils.time import to_local, utc_now

TIMEZONE = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)


def _local_day_start() -> datetime:
    """Return start of today in local time, as a UTC datetime for DB queries."""
    local_now = datetime.now(TIMEZONE)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(ZoneInfo("UTC"))


class PersonalityService:
    """Generates and sends random personality messages and daily summaries."""

    def __init__(
        self,
        session_factory: sessionmaker,
        anthropic_client: AnthropicClient,
    ) -> None:
        self.session_factory = session_factory
        self.anthropic_client = anthropic_client

    def should_send_personality_message(self) -> bool:
        """Check config, roll dice, and check daily cap."""
        with self.session_factory() as session:
            if not get_config_bool(session, "personality_enabled"):
                return False

            chance = get_config_int(session, "personality_chance_percent") or 30
            if random.randint(1, 100) > chance:
                return False

            today_start = _local_day_start()
            today_count = (
                session.query(func.count(NudgeHistory.id))
                .filter(
                    NudgeHistory.nudge_type == "personality",
                    NudgeHistory.sent_at >= today_start,
                )
                .scalar()
            )

            entries_today = (
                session.query(func.count(Entry.id))
                .filter(Entry.created_at >= today_start)
                .scalar()
            )

            if entries_today == 0:
                cap = get_config_int(session, "personality_quiet_day_cap") or 1
            else:
                cap = get_config_int(session, "personality_daily_cap") or 3

            if today_count >= cap:
                return False

        return True

    def gather_personality_context(self) -> dict:
        """Gather context for generating a personality message."""
        now = utc_now()
        today_start = _local_day_start()
        seven_days_ago = now - timedelta(days=7)

        with self.session_factory() as session:
            entries_today_count = (
                session.query(func.count(Entry.id))
                .filter(Entry.created_at >= today_start)
                .scalar()
            )

            # Random old entry via SQL — avoids loading all rows into memory
            old_entry = (
                session.query(Entry)
                .filter(Entry.created_at < seven_days_ago)
                .order_by(func.random())
                .limit(1)
                .first()
            )
            random_old_entry = None
            if old_entry:
                text = old_entry.clean_text or old_entry.raw_text
                random_old_entry = text[:300] if text else None

            # Entity frequency (last 7 days)
            entity_freq = (
                session.query(Entity.name, func.count(entry_entities.c.entry_id))
                .join(entry_entities, Entity.id == entry_entities.c.entity_id)
                .join(Entry, Entry.id == entry_entities.c.entry_id)
                .filter(Entry.created_at >= seven_days_ago)
                .group_by(Entity.name)
                .order_by(func.count(entry_entities.c.entry_id).desc())
                .limit(5)
                .all()
            )
            entity_frequency = None
            if entity_freq:
                entity_frequency = "\n".join(
                    f"- {name}: {count} mentions" for name, count in entity_freq
                )

            last_entry = (
                session.query(Entry)
                .order_by(Entry.created_at.desc())
                .first()
            )
            last_interaction = None
            if last_entry:
                last_interaction = to_local(last_entry.created_at).strftime(
                    "%Y-%m-%d %-I:%M %p"
                )

        local_now = to_local(now)
        return {
            "current_time": local_now.strftime("%Y-%m-%d %-I:%M %p"),
            "entries_today_count": entries_today_count,
            "last_interaction": last_interaction,
            "random_old_entry": random_old_entry,
            "entity_frequency": entity_frequency,
            "day_of_week": local_now.strftime("%A"),
        }

    def generate_personality_message(self, context: dict) -> str:
        """Call Haiku to generate a personality message."""
        user_prompt = build_personality_user_prompt(**context)
        result: PersonalityMessage = self.anthropic_client.call_haiku(
            system_prompt=PERSONALITY_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_model=PersonalityMessage,
        )
        return result.message

    async def send_personality_message(self, slack_client, channel_id: str) -> bool:
        """Orchestrate: check → gather → generate → post → record."""
        if not self.should_send_personality_message():
            return False

        try:
            context = self.gather_personality_context()
            message = self.generate_personality_message(context)

            response = await slack_client.chat_postMessage(
                channel=channel_id,
                text=message,
                metadata={
                    "event_type": "personality_message",
                    "event_payload": {},
                },
            )

            # Record in nudge_history
            with self.session_factory() as session:
                nudge = NudgeHistory(
                    entry_id=None,
                    nudge_type="personality",
                    message_text=message,
                    platform_message_id=response["ts"],
                    sent_at=utc_now(),
                    escalation_level=1,
                )
                session.add(nudge)
                session.commit()

            logger.info("Personality message sent")
            return True

        except Exception:
            logger.exception("Failed to send personality message")
            return False

    def gather_summary_data(self) -> dict:
        """Gather data for the daily summary."""
        now = utc_now()
        today_start = _local_day_start()
        tomorrow_start = today_start + timedelta(days=1)
        tomorrow_end = tomorrow_start + timedelta(days=1)

        with self.session_factory() as session:
            # Today's entries
            entries = (
                session.query(Entry)
                .filter(Entry.created_at >= today_start)
                .order_by(Entry.created_at)
                .all()
            )
            entries_text = "\n".join(
                f"- [{e.entry_type}] {(e.clean_text or e.raw_text or '(empty)')[:150]}"
                for e in entries
            )

            # Open loops created today
            new_loops = [e for e in entries if e.is_open_loop]
            loops_created_text = "\n".join(
                f"- {(e.clean_text or e.raw_text or '(empty)')[:150]}"
                for e in new_loops
            )

            # Open loops resolved today (status changed to resolved, updated today)
            resolved = (
                session.query(Entry)
                .filter(
                    Entry.status == "resolved",
                    Entry.updated_at >= today_start,
                )
                .all()
            )
            loops_resolved_text = "\n".join(
                f"- {(e.clean_text or e.raw_text or '(empty)')[:150]}"
                for e in resolved
            )

            # Entities mentioned today
            entity_names = (
                session.query(Entity.name)
                .join(entry_entities, Entity.id == entry_entities.c.entity_id)
                .join(Entry, Entry.id == entry_entities.c.entry_id)
                .filter(Entry.created_at >= today_start)
                .distinct()
                .all()
            )
            entities_text = ", ".join(name for (name,) in entity_names) if entity_names else ""

            # Tomorrow's calendar events
            tomorrow_events = (
                session.query(CalendarEvent)
                .filter(
                    CalendarEvent.start_time >= tomorrow_start,
                    CalendarEvent.start_time < tomorrow_end,
                )
                .order_by(CalendarEvent.start_time)
                .all()
            )
            events_text = "\n".join(
                f"- [{to_local(ev.start_time).strftime('%-I:%M %p')}] {ev.title}"
                for ev in tomorrow_events
            )

            # Count open loops for button logic
            open_loop_count = (
                session.query(func.count(Entry.id))
                .filter(Entry.is_open_loop.is_(True), Entry.status == "open")
                .scalar()
            )

        local_now = to_local(now)
        return {
            "entries_today": entries_text,
            "open_loops_created": loops_created_text,
            "open_loops_resolved": loops_resolved_text,
            "entities_mentioned": entities_text,
            "tomorrow_events": events_text,
            "current_date": local_now.strftime("%A, %B %-d, %Y"),
            "open_loop_count": open_loop_count,
        }

    def generate_daily_summary(self, data: dict) -> str:
        """Call Sonnet to generate the daily summary."""
        user_prompt = build_daily_summary_user_prompt(
            entries_today=data["entries_today"],
            open_loops_created=data["open_loops_created"],
            open_loops_resolved=data["open_loops_resolved"],
            entities_mentioned=data["entities_mentioned"],
            tomorrow_events=data["tomorrow_events"],
            current_date=data["current_date"],
        )
        result: DailySummaryResponse = self.anthropic_client.call_sonnet(
            system_prompt=DAILY_SUMMARY_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_model=DailySummaryResponse,
        )
        return result.summary

    async def send_daily_summary(self, slack_client, channel_id: str) -> None:
        """Orchestrate: check config → gather → generate → post with buttons → record."""
        with self.session_factory() as session:
            if not get_config_bool(session, "daily_summary_enabled"):
                logger.info("Daily summary disabled")
                return

        try:
            data = self.gather_summary_data()
            summary = self.generate_daily_summary(data)

            # Record nudge first to get ID for button values
            with self.session_factory() as session:
                nudge = NudgeHistory(
                    entry_id=None,
                    nudge_type="daily_summary",
                    message_text=summary,
                    sent_at=utc_now(),
                    escalation_level=1,
                )
                session.add(nudge)
                session.commit()
                nudge_id = nudge.id

            blocks = format_daily_summary_blocks(
                summary_text=summary,
                nudge_id=nudge_id,
                open_loop_count=data["open_loop_count"],
            )

            response = await slack_client.chat_postMessage(
                channel=channel_id,
                text=summary,
                blocks=blocks,
            )

            # Update with platform message ID
            with self.session_factory() as session:
                nudge = session.get(NudgeHistory, nudge_id)
                if nudge:
                    nudge.platform_message_id = response["ts"]
                    session.commit()

            logger.info("Daily summary sent (nudge_id=%d)", nudge_id)

        except Exception:
            logger.exception("Failed to send daily summary")
