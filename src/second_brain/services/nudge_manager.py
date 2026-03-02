"""Nudge manager — creates nudges, handles user actions, manages escalations."""

import logging
from datetime import date, timedelta

from sqlalchemy.orm import Session, sessionmaker

from second_brain.bot.formatting import format_nudge, format_nudge_blocks
from second_brain.config import get_config_int
from second_brain.models.entry import Entry
from second_brain.models.nudge import NudgeHistory
from second_brain.prompts.nudge_parsing import (
    NUDGE_PARSING_SYSTEM_PROMPT,
    NUDGE_PARSING_USER_PROMPT_TEMPLATE,
    NudgeParsingResult,
)
from second_brain.services.anthropic_client import AnthropicClient
from second_brain.utils.time import utc_now

logger = logging.getLogger(__name__)


class NudgeManager:
    """Manages the lifecycle of nudges: creation, user actions, and escalation."""

    def __init__(
        self,
        session_factory: sessionmaker,
        anthropic_client: AnthropicClient,
    ) -> None:
        self.session_factory = session_factory
        self.anthropic_client = anthropic_client

    def create_nudge(
        self,
        entry_id: int | None,
        nudge_type: str,
        message: str,
        escalation_level: int = 1,
    ) -> tuple[int, str, list[dict]]:
        """Create a nudge record and prepare the message with Block Kit buttons.

        Args:
            entry_id: The entry this nudge is about (None for pattern nudges).
            nudge_type: open_loop, timely_connection, or pattern_insight.
            message: The nudge message text.
            escalation_level: 1=neutral, 2=urgent, 3=direct.

        Returns:
            Tuple of (nudge_id, formatted message text, Block Kit blocks).
            The caller (bot handler / scheduler) is responsible for actually
            sending the message and updating platform_message_id.
        """
        with self.session_factory() as session:
            nudge = NudgeHistory(
                entry_id=entry_id,
                nudge_type=nudge_type,
                message_text=message,
                escalation_level=escalation_level,
                sent_at=utc_now(),
            )
            session.add(nudge)
            session.commit()

            nudge_id = nudge.id

        formatted = format_nudge(message, escalation_level)
        blocks = format_nudge_blocks(message, nudge_id, escalation_level)

        logger.info(
            "Nudge created: id=%d type=%s entry_id=%s level=%d",
            nudge_id,
            nudge_type,
            entry_id,
            escalation_level,
        )

        return nudge_id, formatted, blocks

    def set_platform_message_id(self, nudge_id: int, message_id: str) -> None:
        """Update the nudge with the platform message ID after sending."""
        with self.session_factory() as session:
            nudge = session.get(NudgeHistory, nudge_id)
            if nudge:
                nudge.platform_message_id = message_id
                session.commit()

    def handle_nudge_action(
        self,
        nudge_id: int,
        action: str,
        snooze_until: date | None = None,
    ) -> str:
        """Process a user action on a nudge (done/snoozed/dropped).

        Args:
            nudge_id: The nudge record ID.
            action: One of 'done', 'snoozed', 'dropped'.
            snooze_until: Required if action is 'snoozed'.

        Returns:
            A confirmation message string.
        """
        with self.session_factory() as session:
            nudge = session.get(NudgeHistory, nudge_id)
            if not nudge:
                logger.warning("Nudge not found: %d", nudge_id)
                return "Nudge not found."

            now = utc_now()
            nudge.user_action = action
            nudge.user_action_at = now

            confirmation = ""

            if action == "done":
                confirmation = "Marked as done."
                if nudge.entry_id:
                    entry = session.get(Entry, nudge.entry_id)
                    if entry:
                        entry.status = "resolved"
                        entry.is_open_loop = False

            elif action == "snoozed":
                if snooze_until is None:
                    snooze_until = (now + timedelta(days=1)).date()
                nudge.snooze_until = snooze_until
                confirmation = f"Snoozed until {snooze_until.isoformat()}."

            elif action == "dropped":
                confirmation = "Dropped. Won't remind you again."
                if nudge.entry_id:
                    entry = session.get(Entry, nudge.entry_id)
                    if entry:
                        entry.status = "archived"
                        entry.is_open_loop = False

            session.commit()
            logger.info(
                "Nudge action: id=%d action=%s snooze_until=%s",
                nudge_id,
                action,
                snooze_until,
            )
            return confirmation

    def parse_natural_language_response(
        self,
        nudge_id: int,
        user_response: str,
    ) -> tuple[str, date | None]:
        """Parse a free-text reply to a nudge using Haiku.

        Args:
            nudge_id: The nudge being responded to.
            user_response: The user's natural language reply.

        Returns:
            Tuple of (action, snooze_until_date).
        """
        with self.session_factory() as session:
            nudge = session.get(NudgeHistory, nudge_id)
            if not nudge:
                return "done", None

            nudge_message = nudge.message_text

        today = utc_now().date()

        def _esc(s: str) -> str:
            return s.replace("{", "{{").replace("}", "}}")

        user_prompt = NUDGE_PARSING_USER_PROMPT_TEMPLATE.format(
            current_date=today.isoformat(),
            nudge_message=_esc(nudge_message),
            user_response=_esc(user_response),
        )

        result: NudgeParsingResult = self.anthropic_client.call_haiku(
            system_prompt=NUDGE_PARSING_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_model=NudgeParsingResult,
        )

        snooze_date = None
        if result.intent == "snooze" and result.snooze_until:
            snooze_date = date.fromisoformat(result.snooze_until)

        action_map = {"done": "done", "snooze": "snoozed", "drop": "dropped"}
        action = action_map.get(result.intent, "done")

        return action, snooze_date

    def check_escalations(self) -> list[tuple[int, str, list[list[dict]]]]:
        """Find unactioned nudges past escalation thresholds and create escalated nudges.

        Returns:
            List of (nudge_id, formatted_message, keyboard) tuples for sending.
        """
        now = utc_now()
        pending: list[dict] = []

        with self.session_factory() as session:
            escalation_days = get_config_int(session, "nudge_escalation_days") or 3

            # Find nudges that have not been acted on and haven't reached max level
            unactioned = (
                session.query(NudgeHistory)
                .filter(
                    NudgeHistory.user_action.is_(None),
                    NudgeHistory.escalation_level < 3,
                )
                .all()
            )

            for nudge in unactioned:
                # Skip snoozed nudges that haven't expired
                if nudge.snooze_until and nudge.snooze_until > now.date():
                    continue

                days_since = (now - nudge.sent_at).days
                current_level = nudge.escalation_level

                if current_level == 1 and days_since >= escalation_days:
                    next_level = 2
                elif current_level == 2 and days_since >= escalation_days:
                    next_level = 3
                else:
                    continue

                # Check no existing escalation at this level for the same entry
                # Use IS for NULL-safe comparison (SQL NULL == NULL is false)
                if nudge.entry_id is not None:
                    entry_filter = NudgeHistory.entry_id == nudge.entry_id
                else:
                    entry_filter = NudgeHistory.entry_id.is_(None)
                existing = (
                    session.query(NudgeHistory)
                    .filter(
                        entry_filter,
                        NudgeHistory.escalation_level == next_level,
                        NudgeHistory.user_action.is_(None),
                    )
                    .first()
                )
                if existing:
                    continue

                # Mark old nudge as superseded
                nudge.user_action = "no_action"
                nudge.user_action_at = now

                # Build message from entry context
                message = self._build_escalation_message(
                    session, nudge, next_level, days_since
                )
                pending.append(
                    {
                        "entry_id": nudge.entry_id,
                        "nudge_type": nudge.nudge_type,
                        "message": message,
                        "level": next_level,
                    }
                )

            session.commit()

        # Create new nudges outside the original session
        escalated = []
        for data in pending:
            new_nudge, formatted, keyboard = self.create_nudge(
                entry_id=data["entry_id"],
                nudge_type=data["nudge_type"],
                message=data["message"],
                escalation_level=data["level"],
            )
            escalated.append((new_nudge, formatted, keyboard))

        return escalated

    def _build_escalation_message(
        self,
        session: Session,
        original_nudge: NudgeHistory,
        next_level: int,
        days_open: int,
    ) -> str:
        """Build an escalated nudge message based on the level."""
        entry_text = ""
        if original_nudge.entry_id:
            entry = session.get(Entry, original_nudge.entry_id)
            if entry:
                entry_text = entry.clean_text or entry.raw_text

        if next_level == 2:
            if entry_text:
                return f"This has been open for {days_open} days: {entry_text[:100]}"
            return f"An open loop has been waiting for {days_open} days."

        # level 3
        if entry_text:
            return (
                f"This has been open for {days_open} days. "
                f"Should this be resolved or dropped? {entry_text[:100]}"
            )
        return (
            f"An open loop has been waiting for {days_open} days. "
            "Should this be resolved or dropped?"
        )

    def get_snoozed_due(self) -> list[dict]:
        """Find snoozed nudges whose snooze period has expired.

        Marks found nudges as 're_nudged' so they are not picked up again
        on the next scheduler cycle.

        Returns:
            List of dicts with entry_id, nudge_type, message_text keys,
            ready to be re-nudged at level 1.
        """
        today = utc_now().date()
        with self.session_factory() as session:
            nudges = (
                session.query(NudgeHistory)
                .filter(
                    NudgeHistory.user_action == "snoozed",
                    NudgeHistory.snooze_until <= today,
                )
                .all()
            )
            result = [
                {
                    "entry_id": n.entry_id,
                    "nudge_type": n.nudge_type,
                    "message_text": n.message_text,
                }
                for n in nudges
            ]
            # Mark as processed to prevent infinite re-nudging
            for n in nudges:
                n.user_action = "re_nudged"
                n.user_action_at = utc_now()
            session.commit()
            return result
