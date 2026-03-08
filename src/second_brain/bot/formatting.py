"""Visual formatting helpers for Slack messages.

Uses Slack mrkdwn syntax: *bold*, _italic_, `code`, ~strikethrough~,
> blockquote, and Block Kit for interactive elements.
"""


def format_capture_confirmation(entry_type: str, has_connections: bool = False) -> str:
    type_labels = {
        "task": "Task",
        "idea": "Idea",
        "meeting_note": "Meeting note",
        "project_context": "Project context",
        "personal": "Note",
    }
    label = type_labels.get(entry_type, "Note")
    return f":white_check_mark: *Captured* — _{label}_"


def format_nudge(nudge_text: str, escalation_level: int = 1) -> str:
    prefixes = {
        1: (":bell:", "Reminder"),
        2: (":warning:", "Attention"),
        3: (":rotating_light:", "Action Needed"),
    }
    emoji, label = prefixes.get(escalation_level, (":bell:", "Reminder"))
    return f"{emoji} *{label}*\n{nudge_text}"


def format_nudge_blocks(
    message: str, nudge_id: int, escalation_level: int = 1
) -> list[dict]:
    prefixes = {
        1: (":bell:", "Reminder"),
        2: (":warning:", "Attention"),
        3: (":rotating_light:", "Action Needed"),
    }
    emoji, label = prefixes.get(escalation_level, (":bell:", "Reminder"))

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{emoji} *{label}*\n{message}"},
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Done"},
                    "action_id": "nudge_done",
                    "value": str(nudge_id),
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Snooze"},
                    "action_id": "nudge_snooze",
                    "value": str(nudge_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Drop"},
                    "action_id": "nudge_drop",
                    "value": str(nudge_id),
                    "style": "danger",
                },
            ],
        },
    ]


def format_error(message: str) -> str:
    return f":x: {message}"


def format_recovery(message: str) -> str:
    return f":large_green_circle: *Recovered* — {message}"


def format_query_response(response: str, sources: list[dict]) -> str:
    if not sources:
        return response

    source_lines = []
    for src in sources:
        date_str = src.get("date", "unknown date")
        source_lines.append(f"• _{date_str}_")

    attribution = "\n\n:mag: *Sources:*\n" + "\n".join(source_lines)
    return f"{response}{attribution}"


def format_daily_summary_blocks(
    summary_text: str, nudge_id: int, open_loop_count: int
) -> list[dict]:
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": summary_text},
        },
        {"type": "divider"},
    ]

    action_elements = []
    if open_loop_count > 0:
        action_elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Review open loops"},
                "action_id": "summary_review_loops",
                "value": str(nudge_id),
            },
        )
    action_elements.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Looks good"},
            "action_id": "summary_dismiss",
            "value": str(nudge_id),
        },
    )

    blocks.append({"type": "actions", "elements": action_elements})
    return blocks


def format_meeting_brief(
    meeting_title: str,
    start_time: str,
    brief_text: str,
    attendees: list[str] | None = None,
) -> str:
    parts = [f":calendar: *Meeting Brief — {meeting_title}* at {start_time}"]

    if attendees:
        parts.append(f"*Attendees:* {', '.join(attendees)}")

    parts.append("")
    parts.append(brief_text)

    return "\n".join(parts)
