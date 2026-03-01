"""Prompts and response models for pattern / insight detection via Sonnet."""

from pydantic import BaseModel, Field


PATTERN_DETECTION_SYSTEM_PROMPT = """\
You are a pattern detection engine for a personal knowledge base. You receive \
the user's entries from the past 7 days and identify recurring themes, \
contradictions, or patterns across them.

YOUR TASKS:
1. Read all entries carefully.
2. Identify any of the following:
   - **Recurring themes:** Topics, people, projects, or concerns that appear \
in multiple entries across different days or contexts.
   - **Contradictions:** Entries that contain conflicting information, \
intentions, or sentiments about the same subject.
   - **Patterns:** Behavioral or topical patterns the user might not notice \
themselves (e.g., repeated mentions of a problem without resolution, shifting \
priorities, emerging interests).

RESPOND WITH VALID JSON matching this exact schema:
{
  "patterns": [
    {
      "insight_text": "A concise, specific observation. Address the user directly.",
      "related_entry_ids": [1, 2, 3],
      "insight_type": "theme" | "contradiction" | "pattern"
    }
  ]
}

RULES:
- Only surface genuinely interesting patterns. If nothing stands out, return \
{"patterns": []}.
- Each insight should reference at least 2 entries by their IDs.
- Write insight_text as a single sentence or two, addressed to the user. \
Example: "You've mentioned supply chain issues three times this week across \
different projects."
- Be selective — quality over quantity. 0-3 insights per run is typical.
- Do NOT repeat what the entries already say. Synthesize across entries.
- Do NOT fabricate connections that are not supported by the entries.\
"""


def build_pattern_detection_user_prompt(entries: list[dict]) -> str:
    """Build the user prompt for pattern detection.

    Args:
        entries: List of dicts with 'id', 'clean_text', 'entry_type',
            'created_at', and 'tags' keys.

    Returns:
        Formatted user prompt string.
    """
    if not entries:
        return "No entries from the past 7 days."

    parts = [f"ENTRIES FROM THE PAST 7 DAYS ({len(entries)} total):\n"]
    for entry in entries:
        tags_str = ", ".join(entry.get("tags", []))
        header = f"[ID: {entry['id']}] ({entry.get('entry_type', 'unknown')}) — {entry.get('created_at', '')}"
        if tags_str:
            header += f" — tags: {tags_str}"
        parts.append(header)
        parts.append(entry.get("clean_text", entry.get("raw_text", "")))
        parts.append("")  # blank line between entries
    return "\n".join(parts)


class PatternInsight(BaseModel):
    """A single pattern or insight detected across recent entries."""

    insight_text: str = Field(
        description="A concise observation addressed to the user."
    )
    related_entry_ids: list[int] = Field(
        description="IDs of the entries that support this insight."
    )
    insight_type: str = Field(
        description="Type of insight: 'theme', 'contradiction', or 'pattern'."
    )


class PatternDetectionResult(BaseModel):
    """Structured result from the pattern detection Sonnet call."""

    patterns: list[PatternInsight] = Field(
        default_factory=list,
        description="List of detected patterns/insights. Empty if nothing stands out.",
    )
