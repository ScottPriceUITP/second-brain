"""Sonnet prompt for proactive scheduler reasoning.

Used by SchedulerService to decide whether to surface a nudge. Sonnet acts as
a strict filter: most runs should produce no message.
"""

from pydantic import BaseModel, Field

SCHEDULER_SYSTEM_PROMPT = """\
You are the proactive scheduler for a personal knowledge management system.

Your job is to review the user's open loops, recent entries, and upcoming \
calendar events and decide whether ANYTHING is worth surfacing right now.

CRITICAL RULES:
1. MOST of the time, the answer is NOTHING. Do NOT fabricate nudges.
2. Only surface something if it is:
   - Genuinely overdue (past its follow-up date and still open)
   - Time-sensitive (connected to an upcoming event within 24 hours)
   - A meaningful pattern the user would want to know about (NOT trivial)
3. Surface ONE focused item at a time, never a list.
4. Write in a conversational but concise tone. Address the user directly.
5. Reference specific details (names, dates, topics) from the data.
6. Do NOT make up information not present in the provided data.

NUDGE TYPES:
- open_loop: An overdue or aging open loop that needs attention.
- timely_connection: A connection between a recent entry and an upcoming event.
- pattern_insight: A recurring theme or contradiction across recent entries.

If nothing qualifies, set should_nudge to false and leave other fields empty.\
"""

SCHEDULER_USER_PROMPT_TEMPLATE = """\
Current time: {current_time}

=== OPEN LOOPS ===
{open_loops}

=== RECENT ENTRIES (last 7 days) ===
{recent_entries}

=== UPCOMING CALENDAR EVENTS (next 24 hours) ===
{calendar_events}

Based on the above, should I surface anything to the user right now? \
Remember: most of the time the answer is NO.\
"""


class SchedulerDecision(BaseModel):
    """Structured response from the scheduler reasoning model."""

    should_nudge: bool = Field(
        description="Whether to send a nudge. False means stay silent."
    )
    nudge_type: str | None = Field(
        default=None,
        description="Type: open_loop, timely_connection, or pattern_insight.",
    )
    entry_id: int | None = Field(
        default=None,
        description="The entry ID this nudge relates to (null for pattern insights).",
    )
    message: str | None = Field(
        default=None,
        description="The nudge message to send to the user.",
    )
    escalation_level: int = Field(
        default=1,
        description="Escalation level: 1=neutral, 2=urgent, 3=direct.",
    )
