"""Entity merge tracking model."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from second_brain.models.base import Base
from second_brain.utils.time import utc_now


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
        DateTime, default=lambda: utc_now(), nullable=False
    )
