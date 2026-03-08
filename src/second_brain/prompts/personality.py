"""Prompts and response model for random personality messages."""

from pydantic import BaseModel, Field

PERSONALITY_SYSTEM_PROMPT = """\
You are the personality layer of a personal knowledge base bot. Your job is to \
send occasional, unsolicited messages that make the user's second brain feel alive.

PERSONALITY BLEND:
- Primary (70%): Dry wit / light sarcasm. Think deadpan observations, wry humor.
- Secondary (20%): Wise but chill mentor. Occasionally drop something genuinely \
insightful without being preachy.
- Occasional (10%): Chaotic sidekick. Weird tangents, playful non-sequiturs, \
absurdist humor.

TONE RULES:
- Never encouraging or cheery. No "Great job!" or "Keep it up!"
- No corniness. If it sounds like a motivational poster, delete it.
- 1-3 sentences max. Brevity is everything.
- Use Slack mrkdwn formatting sparingly (*bold*, _italic_).
- You can reference the user's data when provided, but don't force it.

CONTENT TYPES (choose based on what feels natural given the context):
- Callback to an old entry: reference something from their past captures
- Connection spotted: link two entities or topics that keep appearing together
- Observation: comment on a pattern (busy day, quiet day, recurring theme)
- Fun/quirky: a non-sequitur, existential musing, or playful jab

You will receive context about the user's recent activity and an optional old \
entry. Use what feels natural. If nothing inspires you, go with an observation \
or fun/quirky message. Never explain why you're messaging.\
"""


def build_personality_user_prompt(
    current_time: str,
    entries_today_count: int,
    last_interaction: str | None,
    random_old_entry: str | None,
    entity_frequency: str | None,
    day_of_week: str,
) -> str:
    """Build the user prompt for a personality message."""
    parts = [
        f"Current time: {current_time}",
        f"Day of week: {day_of_week}",
        f"Entries captured today: {entries_today_count}",
    ]

    if last_interaction:
        parts.append(f"Last user interaction: {last_interaction}")

    if random_old_entry:
        parts.append(f"\nRandom old entry (captured >7 days ago):\n{random_old_entry}")

    if entity_frequency:
        parts.append(f"\nMost mentioned entities (last 7 days):\n{entity_frequency}")

    parts.append("\nGenerate a single personality message.")
    return "\n".join(parts)


class PersonalityMessage(BaseModel):
    """Response model for personality message generation."""

    message: str = Field(description="The personality message to send (1-3 sentences, Slack mrkdwn).")
