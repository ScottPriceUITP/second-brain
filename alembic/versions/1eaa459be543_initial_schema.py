"""initial_schema

Revision ID: 1eaa459be543
Revises:
Create Date: 2026-03-01 19:56:48.500174
"""

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1eaa459be543"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- entries ---
    op.create_table(
        "entries",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("raw_text", sa.Text, nullable=False),
        sa.Column("clean_text", sa.Text, nullable=True),
        sa.Column("entry_type", sa.Text, nullable=False, server_default="personal"),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("is_open_loop", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("follow_up_date", sa.Date, nullable=True),
        sa.Column("telegram_message_id", sa.Integer, nullable=True),
        sa.Column("calendar_event_id", sa.Text, nullable=True),
        sa.Column("audio_file_id", sa.Text, nullable=True),
        sa.Column("embedding", sa.LargeBinary, nullable=True),
    )

    # --- entities ---
    op.create_table(
        "entities",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column(
            "merged_into_id",
            sa.Integer,
            sa.ForeignKey("entities.id"),
            nullable=True,
        ),
    )

    # --- entry_entities junction ---
    op.create_table(
        "entry_entities",
        sa.Column(
            "entry_id", sa.Integer, sa.ForeignKey("entries.id"), primary_key=True
        ),
        sa.Column(
            "entity_id", sa.Integer, sa.ForeignKey("entities.id"), primary_key=True
        ),
    )

    # --- entity_merges ---
    op.create_table(
        "entity_merges",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "source_entity_id",
            sa.Integer,
            sa.ForeignKey("entities.id"),
            nullable=False,
        ),
        sa.Column(
            "target_entity_id",
            sa.Integer,
            sa.ForeignKey("entities.id"),
            nullable=False,
        ),
        sa.Column("merged_at", sa.DateTime, nullable=False),
    )

    # --- entry_relations ---
    op.create_table(
        "entry_relations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "from_entry_id", sa.Integer, sa.ForeignKey("entries.id"), nullable=False
        ),
        sa.Column(
            "to_entry_id", sa.Integer, sa.ForeignKey("entries.id"), nullable=False
        ),
        sa.Column("relation_type", sa.Text, nullable=False),
        sa.Column("confidence_score", sa.Float, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    # --- tags ---
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text, unique=True, nullable=False),
    )

    # --- entry_tags junction ---
    op.create_table(
        "entry_tags",
        sa.Column(
            "entry_id", sa.Integer, sa.ForeignKey("entries.id"), primary_key=True
        ),
        sa.Column("tag_id", sa.Integer, sa.ForeignKey("tags.id"), primary_key=True),
    )

    # --- calendar_events ---
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("calendar_id", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("start_time", sa.DateTime, nullable=False),
        sa.Column("end_time", sa.DateTime, nullable=False),
        sa.Column("location", sa.Text, nullable=True),
        sa.Column("video_link", sa.Text, nullable=True),
        sa.Column("attendees", sa.Text, nullable=True),
        sa.Column("synced_at", sa.DateTime, nullable=False),
    )

    # --- nudge_history ---
    op.create_table(
        "nudge_history",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "entry_id", sa.Integer, sa.ForeignKey("entries.id"), nullable=True
        ),
        sa.Column("nudge_type", sa.Text, nullable=False),
        sa.Column("message_text", sa.Text, nullable=False),
        sa.Column("telegram_message_id", sa.Integer, nullable=True),
        sa.Column("sent_at", sa.DateTime, nullable=False),
        sa.Column(
            "escalation_level", sa.Integer, nullable=False, server_default="1"
        ),
        sa.Column("user_action", sa.Text, nullable=True),
        sa.Column("user_action_at", sa.DateTime, nullable=True),
        sa.Column("snooze_until", sa.Date, nullable=True),
    )

    # --- config ---
    op.create_table(
        "config",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    # --- FTS5 virtual table on entries.clean_text ---
    op.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5("
        "clean_text, content='entries', content_rowid='id'"
        ")"
    )

    # --- FTS5 triggers to keep the index in sync ---
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN "
        "INSERT INTO entries_fts(rowid, clean_text) VALUES (new.id, new.clean_text); "
        "END;"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN "
        "INSERT INTO entries_fts(entries_fts, rowid, clean_text) "
        "VALUES ('delete', old.id, old.clean_text); "
        "END;"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN "
        "INSERT INTO entries_fts(entries_fts, rowid, clean_text) "
        "VALUES ('delete', old.id, old.clean_text); "
        "INSERT INTO entries_fts(rowid, clean_text) VALUES (new.id, new.clean_text); "
        "END;"
    )

    # --- Seed config defaults ---
    now = datetime.now(timezone.utc)
    config_defaults = [
        ("connection_score_threshold", "4"),
        ("connection_min_count", "2"),
        ("entity_match_confidence_threshold", "0.8"),
        ("scheduler_interval_hours", "2"),
        ("scheduler_start_hour", "8"),
        ("scheduler_end_hour", "21"),
        ("query_session_timeout_minutes", "10"),
        ("query_max_entries", "30"),
        ("nudge_escalation_days", "3"),
        ("pre_meeting_brief_minutes", "15"),
        ("calendar_sync_interval_minutes", "30"),
        ("meeting_check_interval_minutes", "5"),
        ("enrichment_retry_count", "3"),
        ("enrichment_retry_interval_minutes", "10"),
        ("transcription_retry_count", "3"),
        ("transcription_retry_interval_minutes", "10"),
        ("notify_on_token_refresh", "true"),
    ]
    op.bulk_insert(
        sa.table(
            "config",
            sa.column("key", sa.Text),
            sa.column("value", sa.Text),
            sa.column("updated_at", sa.DateTime),
        ),
        [{"key": k, "value": v, "updated_at": now} for k, v in config_defaults],
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS entries_au")
    op.execute("DROP TRIGGER IF EXISTS entries_ad")
    op.execute("DROP TRIGGER IF EXISTS entries_ai")
    op.execute("DROP TABLE IF EXISTS entries_fts")
    op.drop_table("config")
    op.drop_table("nudge_history")
    op.drop_table("calendar_events")
    op.drop_table("entry_tags")
    op.drop_table("tags")
    op.drop_table("entry_relations")
    op.drop_table("entity_merges")
    op.drop_table("entry_entities")
    op.drop_table("entities")
    op.drop_table("entries")
