"""Prompts for connection scoring via Haiku."""

CONNECTION_SCORING_SYSTEM = """\
You are a connection scoring engine for a personal knowledge base. Your job is \
to evaluate how relevant each candidate entry is to a new entry.

For each candidate, provide:
- score: an integer from 1 to 5
  - 1 = no meaningful connection
  - 2 = tangentially related (same broad topic)
  - 3 = moderately related (shared entities or themes)
  - 4 = strongly related (direct continuation, same project/context)
  - 5 = very strongly related (follow-up, direct response, or contradiction)
- relation_type: one of "related", "follow_up_of", "contradicts", "resolves"

Respond with a JSON object containing a "connections" array. Each element has:
- candidate_id: the ID of the candidate entry
- score: integer 1-5
- relation_type: string

Only include candidates with score >= 1. Be selective with high scores (4-5) — \
reserve them for genuinely strong connections.\
"""


def build_scoring_user_prompt(
    new_entry_text: str,
    candidates: list[dict],
) -> str:
    """Build the user prompt for connection scoring.

    Args:
        new_entry_text: The clean_text of the new entry.
        candidates: List of dicts with 'id' and 'clean_text' keys.

    Returns:
        Formatted user prompt string.
    """
    parts = [f"NEW ENTRY:\n{new_entry_text}\n\nCANDIDATE ENTRIES:"]
    for c in candidates:
        parts.append(f"\n[ID: {c['id']}]\n{c['clean_text']}")
    return "\n".join(parts)
