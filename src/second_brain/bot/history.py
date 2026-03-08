"""Fetch Slack conversation history for multi-turn query context."""

import logging

from second_brain.config import get_config_int

logger = logging.getLogger(__name__)


async def fetch_conversation_history(
    slack_client,
    channel_id: str,
    limit: int = 10,
    bot_truncate_chars: int = 200,
    max_chars: int = 1000,
    exclude_latest_ts: str | None = None,
) -> list[dict]:
    """Fetch recent messages from a Slack channel as conversation context.

    Args:
        slack_client: Slack AsyncWebClient instance.
        channel_id: The Slack channel ID to fetch history from.
        limit: Maximum number of messages to fetch from Slack.
        bot_truncate_chars: Truncate bot messages to this many characters.
        max_chars: Total character budget; oldest messages trimmed first.
        exclude_latest_ts: If set, exclude the message with this timestamp
            (used to avoid including the current triggering message).

    Returns:
        List of {"role": "user"|"assistant", "text": "..."} in chronological order.
        Returns [] on any error.
    """
    try:
        response = await slack_client.conversations_history(
            channel=channel_id, limit=limit
        )
        messages = response.get("messages", [])
    except Exception:
        logger.exception("Failed to fetch conversation history for %s", channel_id)
        return []

    # Slack returns newest-first; reverse to chronological
    messages = list(reversed(messages))

    result: list[dict] = []
    for msg in messages:
        # Skip system messages (joins, topic changes, pins, etc.)
        if msg.get("subtype") is not None and not msg.get("bot_id"):
            continue

        # Skip the triggering message to avoid duplication in the prompt
        if exclude_latest_ts and msg.get("ts") == exclude_latest_ts:
            continue

        text = msg.get("text", "")
        if not text.strip():
            continue

        role = "assistant" if msg.get("bot_id") else "user"

        if role == "assistant" and len(text) > bot_truncate_chars:
            text = text[:bot_truncate_chars] + "..."

        result.append({"role": role, "text": text})

    # Apply total max_chars cap by trimming oldest messages first
    total_chars = sum(len(m["text"]) for m in result)
    while result and total_chars > max_chars:
        removed = result.pop(0)
        total_chars -= len(removed["text"])

    return result


async def get_conversation_context(
    services: dict,
    channel_id: str,
    session_factory=None,
    exclude_ts: str | None = None,
) -> list[dict] | None:
    """Read history config and fetch conversation context from Slack.

    Shared helper used by both the message handler and /ask command.
    Returns None if prerequisites (slack_client, channel_id) are missing.
    """
    slack_client = services.get("slack_client")
    if not slack_client or not channel_id:
        return None

    sf = session_factory or services.get("db_session_factory")
    if not sf:
        return None

    with sf() as session:
        hist_limit = get_config_int(session, "conversation_history_messages") or 10
        max_chars = get_config_int(session, "conversation_history_max_chars") or 1000
        bot_truncate = get_config_int(session, "conversation_history_bot_truncate_chars") or 200

    return await fetch_conversation_history(
        slack_client,
        channel_id,
        limit=hist_limit,
        bot_truncate_chars=bot_truncate,
        max_chars=max_chars,
        exclude_latest_ts=exclude_ts,
    )
