"""Migrate from Telegram to Slack: rename telegram_message_id to platform_message_id,
change type from Integer to Text, remove audio_file_id, remove transcription config.

Revision ID: a2b3c4d5e6f7
Revises: 1eaa459be543
Create Date: 2026-03-01 22:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "1eaa459be543"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite doesn't support ALTER COLUMN or RENAME COLUMN (before 3.25) or
    # type changes, so we use batch mode which recreates the table.
    # batch_alter_table drops & recreates the table, which breaks FTS5 triggers.
    # We drop them first and recreate after.

    op.execute("DROP TRIGGER IF EXISTS entries_ai")
    op.execute("DROP TRIGGER IF EXISTS entries_ad")
    op.execute("DROP TRIGGER IF EXISTS entries_au")

    with op.batch_alter_table("entries") as batch_op:
        batch_op.add_column(sa.Column("platform_message_id", sa.Text, nullable=True))
        batch_op.drop_column("telegram_message_id")
        batch_op.drop_column("audio_file_id")

    # Recreate FTS5 triggers on the new entries table
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

    with op.batch_alter_table("nudge_history") as batch_op:
        batch_op.add_column(sa.Column("platform_message_id", sa.Text, nullable=True))
        batch_op.drop_column("telegram_message_id")

    # Remove transcription config keys
    op.execute(
        "DELETE FROM config WHERE key IN "
        "('transcription_retry_count', 'transcription_retry_interval_minutes')"
    )

    # Update source values from telegram to slack
    op.execute("UPDATE entries SET source = 'slack_text' WHERE source = 'telegram_text'")
    op.execute("UPDATE entries SET source = 'slack_text' WHERE source = 'telegram_voice'")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS entries_ai")
    op.execute("DROP TRIGGER IF EXISTS entries_ad")
    op.execute("DROP TRIGGER IF EXISTS entries_au")

    with op.batch_alter_table("entries") as batch_op:
        batch_op.add_column(sa.Column("telegram_message_id", sa.Integer, nullable=True))
        batch_op.add_column(sa.Column("audio_file_id", sa.Text, nullable=True))
        batch_op.drop_column("platform_message_id")

    # Recreate FTS5 triggers on the rebuilt entries table
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

    with op.batch_alter_table("nudge_history") as batch_op:
        batch_op.add_column(sa.Column("telegram_message_id", sa.Integer, nullable=True))
        batch_op.drop_column("platform_message_id")

    # Note: rows originally sourced from 'telegram_voice' cannot be distinguished
    # from 'telegram_text' after upgrade — both were mapped to 'slack_text'.
    op.execute("UPDATE entries SET source = 'telegram_text' WHERE source = 'slack_text'")
