"""Entry relation model for connections between entries."""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.models.base import Base
from second_brain.utils.time import utc_now


class EntryRelation(Base):
    __tablename__ = "entry_relations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_entry_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("entries.id"), nullable=False
    )
    to_entry_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("entries.id"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # related/follow_up_of/contradicts/resolves
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: utc_now(), nullable=False
    )
