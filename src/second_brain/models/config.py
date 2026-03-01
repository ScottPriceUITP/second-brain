"""Config model — runtime-tunable settings stored in SQLite."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.models.base import Base


class ConfigSetting(Base):
    __tablename__ = "config"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
