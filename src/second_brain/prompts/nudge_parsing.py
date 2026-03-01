"""Haiku prompt for parsing natural language nudge responses.

Used by NudgeManager to extract user intent and snooze dates from
free-text replies to nudge messages.
"""

from pydantic import BaseModel, Field

NUDGE_PARSING_SYSTEM_PROMPT = """\
You parse natural language responses to reminder/nudge messages.

The user has received a nudge about an open loop and is responding. \
Extract their intent and any snooze date.

INTENTS:
- done: The user has handled it or it's no longer relevant.
  Examples: "I already handled this", "done", "it's resolved", "taken care of"
- snooze: The user wants to be reminded later.
  Examples: "remind me next Thursday", "snooze until Monday", "come back in a week"
- drop: The user wants to stop being reminded entirely.
  Examples: "stop telling me about this", "drop it", "never mind", "archive this"

For snooze, extract the target date as an ISO date string (YYYY-MM-DD). \
Use the provided current date to compute relative dates like "tomorrow", \
"next week", "in 3 days".

If the intent is unclear, default to snooze with a 1-day delay.\
"""

NUDGE_PARSING_USER_PROMPT_TEMPLATE = """\
Current date: {current_date}
Original nudge: {nudge_message}
User response: {user_response}\
"""


class NudgeParsingResult(BaseModel):
    """Structured response from nudge response parsing."""

    intent: str = Field(
        description="User intent: done, snooze, or drop."
    )
    snooze_until: str | None = Field(
        default=None,
        description="ISO date (YYYY-MM-DD) for snooze. Null if not snoozing.",
    )
