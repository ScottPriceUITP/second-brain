"""Nudge history model — tracks all proactive nudges sent to the user."""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.models.base import Base
from second_brain.utils.time import utc_now


class NudgeHistory(Base):
    __tablename__ = "nudge_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("entries.id"), nullable=True
    )  # Nullable for pattern nudges
    nudge_type: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # open_loop/timely_connection/pattern_insight
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    platform_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: utc_now(), nullable=False
    )
    escalation_level: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )  # 1=neutral, 2=urgent, 3=direct
    user_action: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # done/snoozed/dropped/no_action
    user_action_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    snooze_until: Mapped[date | None] = mapped_column(Date, nullable=True)
