"""Visual formatting helpers for Telegram messages.

All formatting uses Telegram MarkdownV2 or plain text.
Each function returns a string ready to send via bot.send_message().
"""


def format_capture_confirmation(entry_type: str, has_connections: bool = False) -> str:
    """Brief capture confirmation with checkmark.

    Args:
        entry_type: The classified entry type (task, idea, meeting_note, etc.)
        has_connections: Whether strong connections were found.
    """
    type_labels = {
        "task": "Task",
        "idea": "Idea",
        "meeting_note": "Meeting note",
        "project_context": "Project context",
        "personal": "Note",
    }
    label = type_labels.get(entry_type, "Note")
    msg = f"Captured. [{label}]"
    return msg


def format_nudge(nudge_text: str, escalation_level: int = 1) -> str:
    """Format a proactive nudge — visually distinct from captures.

    Args:
        nudge_text: The nudge message content.
        escalation_level: 1=neutral, 2=urgent, 3=direct.
    """
    prefixes = {
        1: "REMINDER",
        2: "ATTENTION",
        3: "ACTION NEEDED",
    }
    prefix = prefixes.get(escalation_level, "REMINDER")
    return f"[{prefix}]\n{nudge_text}"


def format_error(message: str) -> str:
    """Format an error notification — warning style.

    Args:
        message: The error description.
    """
    return f"[WARNING] {message}"


def format_recovery(message: str) -> str:
    """Format a recovery notification — confirms a previously failed operation succeeded.

    Args:
        message: The recovery description.
    """
    return f"[RECOVERED] {message}"


def format_query_response(response: str, sources: list[dict]) -> str:
    """Format a query response with source attribution.

    Args:
        response: The answer text from the LLM.
        sources: List of dicts with 'date' and optionally 'entry_type' keys.
    """
    if not sources:
        return response

    source_lines = []
    for src in sources:
        date_str = src.get("date", "unknown date")
        source_lines.append(f"  - {date_str}")

    attribution = "Sources:\n" + "\n".join(source_lines)
    return f"{response}\n\n{attribution}"


def format_meeting_brief(
    meeting_title: str,
    start_time: str,
    brief_text: str,
    attendees: list[str] | None = None,
) -> str:
    """Format a pre-meeting brief.

    Args:
        meeting_title: The calendar event title.
        start_time: Human-readable start time.
        brief_text: The Sonnet-generated brief content.
        attendees: Optional list of attendee names.
    """
    header = f"[MEETING BRIEF] {meeting_title} at {start_time}"
    parts = [header]

    if attendees:
        parts.append("Attendees: " + ", ".join(attendees))

    parts.append("")
    parts.append(brief_text)

    return "\n".join(parts)
