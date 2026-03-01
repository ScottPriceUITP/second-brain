"""Sonnet prompt for generating pre-meeting briefs.

Used by MeetingBriefService to create focused, actionable summaries
before meetings, based on relevant brain entries and calendar context.
"""

from pydantic import BaseModel, Field

MEETING_BRIEF_SYSTEM_PROMPT = """\
You generate focused pre-meeting briefs for a personal knowledge management system.

Given an upcoming meeting and relevant past notes/entries, create a concise brief \
that helps the user prepare. Focus on what is actionable and relevant.

RULES:
1. Be concise — aim for 3-6 bullet points, not an essay.
2. Prioritize:
   - Open items or follow-ups with attendees
   - Recent conversations or notes involving attendees or meeting topics
   - Relevant project context or decisions
3. Reference specific dates and details from the provided entries.
4. If there are open loops related to attendees, highlight them.
5. Do NOT make up information not present in the provided data.
6. If there is genuinely nothing useful to brief, set has_content to false.
7. Write in second person ("You discussed...", "You have an open item...").
"""

MEETING_BRIEF_USER_PROMPT_TEMPLATE = """\
=== UPCOMING MEETING ===
Title: {meeting_title}
Time: {meeting_time}
Attendees: {attendees}
Description: {description}

=== RELEVANT ENTRIES FROM YOUR BRAIN ===
{relevant_entries}

Generate a brief for this meeting. If the entries don't contain anything \
genuinely useful for preparation, set has_content to false.\
"""


class MeetingBriefResult(BaseModel):
    """Structured response from the meeting brief model."""

    has_content: bool = Field(
        description="Whether there is meaningful content for a brief."
    )
    brief: str = Field(
        default="",
        description="The meeting brief text, formatted with bullet points.",
    )
