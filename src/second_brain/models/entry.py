"""Entry model — the core knowledge unit."""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Integer, LargeBinary, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from second_brain.models.base import Base
from second_brain.utils.time import utc_now


class Entry(Base):
    __tablename__ = "entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: utc_now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: utc_now(),
        onupdate=lambda: utc_now(),
        nullable=False,
    )
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    clean_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_type: Mapped[str] = mapped_column(
        Text, nullable=False, default="personal"
    )  # task/idea/meeting_note/project_context/personal
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="open"
    )  # open/resolved/archived/pending_enrichment/pending_transcription
    source: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # slack_text
    is_open_loop: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    follow_up_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    platform_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    calendar_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # Relationships
    entities = relationship("Entity", secondary="entry_entities", back_populates="entries")
    tags = relationship("Tag", secondary="entry_tags", back_populates="entries")
