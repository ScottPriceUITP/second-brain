"""Config model — runtime-tunable settings stored in SQLite."""

from datetime import datetime

from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.models.base import Base
from second_brain.utils.time import utc_now


class ConfigSetting(Base):
    __tablename__ = "config"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: utc_now(), nullable=False
    )
