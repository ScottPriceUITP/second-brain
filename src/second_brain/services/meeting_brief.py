"""Meeting brief service — generates pre-meeting briefs from brain entries."""

import json
import logging
from datetime import timedelta

from sqlalchemy.orm import sessionmaker

from second_brain.bot.formatting import format_meeting_brief
from second_brain.config import get_config_int
from second_brain.models.calendar_event import CalendarEvent
from second_brain.models.entry import Entry
from second_brain.models.entity import Entity, entry_entities
from second_brain.models.nudge import NudgeHistory
from second_brain.prompts.meeting_brief import (
    MEETING_BRIEF_SYSTEM_PROMPT,
    MEETING_BRIEF_USER_PROMPT_TEMPLATE,
    MeetingBriefResult,
)
from second_brain.services.anthropic_client import AnthropicClient
from second_brain.utils.fts import fts_search
from second_brain.utils.time import to_local, utc_now

logger = logging.getLogger(__name__)


class MeetingBriefService:
    """Generates and sends pre-meeting briefs based on brain entries.

    Called by the scheduler's meeting check job every 5 minutes.
    """

    def __init__(
        self,
        anthropic_client: AnthropicClient,
        session_factory: sessionmaker,
        calendar_sync=None,
    ) -> None:
        self.anthropic_client = anthropic_client
        self.session_factory = session_factory
        self.calendar_sync = calendar_sync
        self._send_callback = None

    def set_send_callback(self, callback) -> None:
        """Set the async callback for sending messages.

        Args:
            callback: Async callable(text: str) -> dict with 'ts' key.
        """
        self._send_callback = callback

    async def check_upcoming_meetings(self) -> int:
        """Check for meetings starting soon and generate briefs.

        Returns:
            Number of briefs sent.
        """
        with self.session_factory() as session:
            brief_minutes = (
                get_config_int(session, "pre_meeting_brief_minutes") or 15
            )

        now = utc_now()
        cutoff = now + timedelta(minutes=brief_minutes)

        with self.session_factory() as session:
            upcoming = (
                session.query(CalendarEvent)
                .filter(
                    CalendarEvent.start_time >= now,
                    CalendarEvent.start_time <= cutoff,
                )
                .order_by(CalendarEvent.start_time)
                .all()
            )

            briefs_sent = 0
            for event in upcoming:
                if self._already_briefed(session, event.id):
                    continue

                brief_text = self._generate_brief(session, event)
                if brief_text:
                    await self._send_brief(session, event, brief_text)
                    session.commit()  # Commit each brief individually to prevent rollback
                    briefs_sent += 1

        if briefs_sent:
            logger.info("Sent %d pre-meeting brief(s)", briefs_sent)
        return briefs_sent

    def _already_briefed(self, session, event_id: str) -> bool:
        """Check if a brief was already sent for this meeting."""
        # Match exact event tag format to prevent partial ID false positives
        tag = f"[event:{event_id}]"
        existing = (
            session.query(NudgeHistory)
            .filter(
                NudgeHistory.nudge_type == "pre_meeting_brief",
                NudgeHistory.message_text.startswith(tag),
            )
            .first()
        )
        return existing is not None

    def _generate_brief(self, session, event: CalendarEvent) -> str | None:
        """Generate a brief for a single meeting if relevant entries exist.

        Returns:
            Brief text string, or None if no relevant content.
        """
        # Collect search terms from event
        search_terms = [event.title]

        attendees = []
        if event.attendees:
            try:
                attendee_list = json.loads(event.attendees)
                for a in attendee_list:
                    name = a.get("name", "")
                    if name:
                        search_terms.append(name)
                        attendees.append(name)
            except (json.JSONDecodeError, TypeError):
                pass

        # Search brain entries using FTS
        combined_query = " ".join(search_terms)
        relevant_entries = fts_search(session, combined_query, limit=15)

        # Also search by entity matching
        entity_entries = self._find_entries_by_attendee_entities(session, attendees)

        # Merge and deduplicate
        seen_ids = {e.id for e in relevant_entries}
        for entry in entity_entries:
            if entry.id not in seen_ids:
                relevant_entries.append(entry)
                seen_ids.add(entry.id)

        if not relevant_entries:
            logger.debug("No relevant entries for meeting: %s", event.title)
            return None

        # Format entries for the prompt
        entries_text = self._format_entries(relevant_entries)
        attendees_str = ", ".join(attendees) if attendees else "(none listed)"
        meeting_time = to_local(event.start_time).strftime("%Y-%m-%d %I:%M %p")

        def _esc(s: str) -> str:
            return s.replace("{", "{{").replace("}", "}}")

        user_prompt = MEETING_BRIEF_USER_PROMPT_TEMPLATE.format(
            meeting_title=_esc(event.title),
            meeting_time=meeting_time,
            attendees=_esc(attendees_str),
            description=_esc(event.description or "(no description)"),
            relevant_entries=_esc(entries_text),
        )

        result: MeetingBriefResult = self.anthropic_client.call_sonnet(
            system_prompt=MEETING_BRIEF_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_model=MeetingBriefResult,
        )

        if not result.has_content:
            logger.debug("Sonnet found no useful brief content for: %s", event.title)
            return None

        return result.brief

    def _find_entries_by_attendee_entities(
        self, session, attendee_names: list[str]
    ) -> list[Entry]:
        """Find entries linked to entities matching attendee names."""
        if not attendee_names:
            return []

        # Find person entities matching attendee names (case-insensitive)
        matching_entities = (
            session.query(Entity)
            .filter(
                Entity.type == "person",
                Entity.merged_into_id.is_(None),
            )
            .all()
        )

        # Simple name matching (lowercased contains check)
        attendee_lower = {n.lower() for n in attendee_names}
        matched_entity_ids = []
        for entity in matching_entities:
            entity_lower = entity.name.lower()
            for attendee in attendee_lower:
                if attendee in entity_lower or entity_lower in attendee:
                    matched_entity_ids.append(entity.id)
                    break

        if not matched_entity_ids:
            return []

        # Find entries linked to these entities
        from sqlalchemy import select

        entry_ids_query = (
            select(entry_entities.c.entry_id)
            .where(entry_entities.c.entity_id.in_(matched_entity_ids))
            .distinct()
        )
        entry_ids = [
            row[0] for row in session.execute(entry_ids_query).fetchall()
        ]

        if not entry_ids:
            return []

        return (
            session.query(Entry)
            .filter(Entry.id.in_(entry_ids))
            .order_by(Entry.created_at.desc())
            .limit(15)
            .all()
        )

    async def _send_brief(
        self, session, event: CalendarEvent, brief_text: str
    ) -> None:
        """Send the brief via Slack and track it in nudge_history."""
        attendees = []
        if event.attendees:
            try:
                attendee_list = json.loads(event.attendees)
                attendees = [a.get("name", "") for a in attendee_list if a.get("name")]
            except (json.JSONDecodeError, TypeError):
                pass

        formatted = format_meeting_brief(
            meeting_title=event.title,
            start_time=to_local(event.start_time).strftime("%-I:%M %p"),
            brief_text=brief_text,
            attendees=attendees or None,
        )

        # Track in nudge_history to avoid duplicates
        nudge = NudgeHistory(
            entry_id=None,
            nudge_type="pre_meeting_brief",
            message_text=f"[event:{event.id}] {brief_text[:200]}",
            escalation_level=1,
            sent_at=utc_now(),
        )
        session.add(nudge)
        session.flush()

        # Send via Slack if callback is set
        if self._send_callback:
            try:
                sent = await self._send_callback(formatted)
                nudge.platform_message_id = sent.get("ts") if isinstance(sent, dict) else None
            except Exception:
                logger.exception("Failed to send meeting brief via Slack")
        else:
            logger.info("Meeting brief generated but not sent (no callback): %s", event.title)

        logger.info("Meeting brief sent for: %s", event.title)

    @staticmethod
    def _format_entries(entries: list[Entry]) -> str:
        """Format entries for inclusion in the Sonnet prompt."""
        lines = []
        for e in entries:
            text = e.clean_text or e.raw_text
            snippet = text[:200] if text else "(empty)"
            created = e.created_at.strftime("%Y-%m-%d")
            open_loop = " [OPEN LOOP]" if e.is_open_loop and e.status == "open" else ""
            lines.append(
                f"- [{e.entry_type}, {created}]{open_loop}: {snippet}"
            )
        return "\n".join(lines)
