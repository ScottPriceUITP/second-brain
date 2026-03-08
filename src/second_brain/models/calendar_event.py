"""Calendar event model — cached Google Calendar events."""

import json
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

    def attendee_names(self) -> list[str]:
        """Parse the JSON attendees field into a list of display names.

        Falls back from name to email-derived name. Returns [] on any error.
        """
        if not self.attendees:
            return []
        try:
            attendee_list = json.loads(self.attendees)
            names = []
            for a in attendee_list:
                name = a.get("name", "").strip()
                if not name:
                    email = a.get("email", "")
                    name = email.split("@")[0].replace(".", " ").title() if email else ""
                if name:
                    names.append(name)
            return names
        except (json.JSONDecodeError, TypeError):
            return []
