"""Calendar event model — cached Google Calendar events."""

from datetime import datetime

from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.models.base import Base
from second_brain.utils.time import utc_now


class CalendarEvent(Base):
    __tablename__ = "calendar_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # Google Calendar event ID
    calendar_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    attendees: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    synced_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: utc_now(), nullable=False
    )
