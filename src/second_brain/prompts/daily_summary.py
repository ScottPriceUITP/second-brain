"""Prompts and response model for the daily end-of-day summary."""

from pydantic import BaseModel, Field

DAILY_SUMMARY_SYSTEM_PROMPT = """\
You are generating a daily end-of-day summary for a personal knowledge base. \
Your primary job is to be informative — cover everything the user captured today. \
Your secondary job is to have personality (dry wit, occasional insight).

STRUCTURE:
- Start with a brief opening line that sets the tone for the day (one sentence).
- Summarize what was captured: entries by type, key topics, notable items.
- Mention open loops: new ones created today, any resolved today.
- If entities were mentioned, note the most active ones briefly.
- If there are calendar events tomorrow, mention them.
- End with a casual prompt to capture anything else before end of day.

TONE:
- Informative first, personality second.
- Same personality traits as the bot: dry wit primary, wise mentor secondary.
- No bullet points or rigid formatting — write it as a conversational narrative.
- Use Slack mrkdwn: *bold* for emphasis, _italic_ for asides.
- Keep it concise but complete. Aim for 3-6 short paragraphs.

If it was a quiet day (no entries), acknowledge that briefly and still mention \
tomorrow's calendar if available. Don't be weird about quiet days.

RESPOND WITH VALID JSON matching this exact schema:
{"summary": "your daily summary here"}\
"""


def build_daily_summary_user_prompt(
    entries_today: str,
    open_loops_created: str,
    open_loops_resolved: str,
    entities_mentioned: str,
    tomorrow_events: str,
    current_date: str,
) -> str:
    """Build the user prompt for the daily summary."""
    parts = [f"Date: {current_date}"]

    parts.append(f"\nENTRIES CAPTURED TODAY:\n{entries_today or '(none)'}")
    parts.append(f"\nOPEN LOOPS CREATED TODAY:\n{open_loops_created or '(none)'}")
    parts.append(f"\nOPEN LOOPS RESOLVED TODAY:\n{open_loops_resolved or '(none)'}")
    parts.append(f"\nENTITIES MENTIONED TODAY:\n{entities_mentioned or '(none)'}")
    parts.append(f"\nTOMORROW'S CALENDAR:\n{tomorrow_events or '(nothing scheduled)'}")

    parts.append("\nGenerate the daily summary.")
    return "\n".join(parts)


class DailySummaryResponse(BaseModel):
    """Response model for daily summary generation."""

    summary: str = Field(description="The daily summary text (Slack mrkdwn, conversational narrative).")
