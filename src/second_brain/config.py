"""Environment variable reader and config table accessor."""

import logging
import os
from sqlalchemy import text
from sqlalchemy.orm import Session
from second_brain.utils.time import utc_now

logger = logging.getLogger(__name__)

# Required environment variables (secrets only)
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")


# Default config values (seeded into config table on first run)
CONFIG_DEFAULTS: dict[str, str] = {
    "connection_score_threshold": "4",
    "connection_min_count": "2",
    "entity_match_confidence_threshold": "0.8",
    "scheduler_interval_hours": "2",
    "scheduler_start_hour": "8",
    "scheduler_end_hour": "21",
    "conversation_history_messages": "10",
    "conversation_history_max_chars": "1000",
    "conversation_history_bot_truncate_chars": "200",
    "query_max_entries": "30",
    "nudge_escalation_days": "3",
    "pre_meeting_brief_minutes": "15",
    "calendar_sync_interval_minutes": "30",
    "meeting_check_interval_minutes": "5",
    "enrichment_retry_count": "3",
    "enrichment_retry_interval_minutes": "10",
    "escalation_check_interval_minutes": "15",
    "notify_on_token_refresh": "true",
}


def get_config(session: Session, key: str) -> str | None:
    """Read a config value from the config table.

    Falls back to CONFIG_DEFAULTS if the key is not in the database.
    Returns None if the key is unknown.
    """
    row = session.execute(
        text("SELECT value FROM config WHERE key = :key"), {"key": key}
    ).fetchone()
    if row:
        return row[0]
    return CONFIG_DEFAULTS.get(key)


def get_config_int(session: Session, key: str) -> int | None:
    """Read a config value as an integer."""
    val = get_config(session, key)
    if val is None:
        return None
    return int(val)


def get_config_float(session: Session, key: str) -> float | None:
    """Read a config value as a float."""
    val = get_config(session, key)
    if val is None:
        return None
    return float(val)


def get_config_bool(session: Session, key: str) -> bool | None:
    """Read a config value as a boolean."""
    val = get_config(session, key)
    if val is None:
        return None
    return val.lower() in ("true", "1", "yes")


def set_config(session: Session, key: str, value: str) -> None:
    """Write a config value to the config table (upsert)."""
    now = utc_now().isoformat()
    session.execute(
        text(
            "INSERT INTO config (key, value, updated_at) VALUES (:key, :value, :now) "
            "ON CONFLICT(key) DO UPDATE SET value = :value, updated_at = :now"
        ),
        {"key": key, "value": value, "now": now},
    )
    session.commit()
    logger.info("Config updated: %s = %s", key, value)


def seed_config_defaults(session: Session) -> None:
    """Insert default config values for any keys not already present."""
    now = utc_now().isoformat()
    for key, value in CONFIG_DEFAULTS.items():
        session.execute(
            text(
                "INSERT OR IGNORE INTO config (key, value, updated_at) "
                "VALUES (:key, :value, :now)"
            ),
            {"key": key, "value": value, "now": now},
        )
    session.commit()
    logger.info("Config defaults seeded (%d keys)", len(CONFIG_DEFAULTS))
