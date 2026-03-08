"""personality_config

Revision ID: e8ca441b39d3
Revises: a2b3c4d5e6f7
Create Date: 2026-03-08 16:50:42.527890

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e8ca441b39d3'
down_revision: Union[str, Sequence[str], None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PERSONALITY_CONFIG_KEYS = {
    "personality_enabled": "true",
    "personality_daily_cap": "3",
    "personality_chance_percent": "30",
    "personality_quiet_day_cap": "1",
    "daily_summary_enabled": "true",
    "daily_summary_time": "16:30",
}


def upgrade() -> None:
    """Seed personality and daily summary config defaults."""
    for key, value in PERSONALITY_CONFIG_KEYS.items():
        op.execute(
            f"INSERT OR IGNORE INTO config (key, value, updated_at) "
            f"VALUES ('{key}', '{value}', datetime('now'))"
        )


def downgrade() -> None:
    """Remove personality config keys."""
    keys = ", ".join(f"'{k}'" for k in PERSONALITY_CONFIG_KEYS)
    op.execute(f"DELETE FROM config WHERE key IN ({keys})")
