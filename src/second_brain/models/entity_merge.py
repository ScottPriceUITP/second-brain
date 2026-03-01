"""Entity merge tracking model."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.models.base import Base


class EntityMerge(Base):
    __tablename__ = "entity_merges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_entity_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("entities.id"), nullable=False
    )
    target_entity_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("entities.id"), nullable=False
    )
    merged_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
