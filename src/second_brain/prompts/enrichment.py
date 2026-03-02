"""Prompts and response models for the enrichment pipeline.

A single Haiku call classifies intent, cleans text, extracts entities,
detects open loops, and optionally associates with a calendar event.
"""

from pydantic import BaseModel, Field


ENRICHMENT_SYSTEM_PROMPT = """\
You are an enrichment engine for a personal knowledge base. You receive raw \
text (either typed or transcribed from voice) and produce structured metadata.

YOUR TASKS:
1. Intent classification — decide if this is a "capture" (note, task, idea, \
observation) or a "query" (the user is asking a question about their stored \
knowledge).
2. Text cleanup — fix punctuation, capitalization, and sentence boundaries. \
For voice transcriptions: remove filler words (um, uh, like, you know) but \
preserve the user's phrasing and word choice. Do NOT restructure paragraphs \
or rewrite sentences.
3. Entry type classification:
   - task: an action item, something to do
   - idea: a thought, concept, or brainstorm
   - meeting_note: notes from or about a meeting
   - project_context: background info, decisions, or status about a project
   - personal: anything else — personal reflection, life note, etc.
4. Entity extraction — identify people, companies, projects, and technologies \
mentioned. Return each as {name, type} where type is one of: person, company, \
project, technology.
5. Open loop detection — does this text imply a follow-up action, unresolved \
task, or commitment? Set is_open_loop accordingly.
6. Follow-up date — if there is a concrete or implied deadline/date, extract \
it as an ISO date (YYYY-MM-DD). Otherwise null.
7. Tags — extract 1-5 short, lowercase tags that capture the key topics.
8. Calendar event association — if calendar events are provided as context \
and the content clearly relates to one of them (matching attendee names, \
topics, or companies), return that event's ID. Base this on content overlap, \
not just timing. If no clear match, return null.

RESPOND WITH VALID JSON matching this exact schema:
{
  "intent": "capture" or "query",
  "clean_text": "cleaned version of the text",
  "entry_type": "task" | "idea" | "meeting_note" | "project_context" | "personal",
  "entities": [{"name": "...", "type": "person|company|project|technology"}, ...],
  "is_open_loop": true/false,
  "follow_up_date": "YYYY-MM-DD" or null,
  "tags": ["tag1", "tag2", ...],
  "calendar_event_id": "event_id" or null
}

RULES:
- Default to intent "capture" unless the text is clearly a question about \
past knowledge.
- Be conservative with open loop detection — only flag true if there is a \
clear action or commitment implied.
- Tags should be lowercase, no spaces (use hyphens for multi-word tags).
- Keep clean_text faithful to the original — cleanup only, not rewriting.\
"""


def build_enrichment_user_prompt(
    raw_text: str,
    calendar_events: list[dict] | None = None,
    current_date: str | None = None,
) -> str:
    """Build the user prompt for the enrichment call.

    Args:
        raw_text: The raw text to enrich.
        calendar_events: Optional list of calendar event dicts with keys
            like id, title, attendees, start_time, description.
        current_date: Current date as ISO string for date inference.

    Returns:
        Formatted user prompt.
    """
    parts = []

    if current_date:
        parts.append(f"Current date: {current_date}")

    if calendar_events:
        parts.append("\nRECENT/UPCOMING CALENDAR EVENTS:")
        for event in calendar_events:
            event_line = f"- [{event.get('id', '')}] {event.get('title', 'Untitled')}"
            if event.get("start_time"):
                event_line += f" (starts: {event['start_time']})"
            if event.get("attendees"):
                event_line += f" — attendees: {event['attendees']}"
            if event.get("description"):
                desc = event["description"][:200]
                event_line += f" — description: {desc}"
            parts.append(event_line)

    parts.append(f"\nRAW TEXT:\n{raw_text}")

    return "\n".join(parts)


class ExtractedEntity(BaseModel):
    """A single entity extracted from text."""

    name: str = Field(description="Entity name as mentioned in the text.")
    type: str = Field(
        description="Entity type: person, company, project, or technology."
    )


class EnrichmentResult(BaseModel):
    """Structured result from the enrichment Haiku call."""

    intent: str = Field(
        description="Intent classification: 'capture' or 'query'."
    )
    clean_text: str = Field(
        description="Cleaned/punctuated version of the raw text."
    )
    entry_type: str = Field(
        default="personal",
        description="Entry type: task, idea, meeting_note, project_context, or personal.",
    )
    entities: list[ExtractedEntity] = Field(
        default_factory=list,
        description="Entities extracted from the text.",
    )
    is_open_loop: bool = Field(
        default=False,
        description="Whether the text implies a follow-up or unresolved task.",
    )
    follow_up_date: str | None = Field(
        default=None,
        description="Suggested follow-up date as ISO string (YYYY-MM-DD), or null.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="1-5 lowercase topic tags.",
    )
    calendar_event_id: str | None = Field(
        default=None,
        description="ID of a matching calendar event, or null.",
    )
