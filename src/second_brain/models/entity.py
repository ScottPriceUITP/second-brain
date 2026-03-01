"""Entity model and entry-entity junction table."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Table, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from second_brain.models.base import Base

# Junction table for entry-entity many-to-many
entry_entities = Table(
    "entry_entities",
    Base.metadata,
    Column("entry_id", Integer, ForeignKey("entries.id"), primary_key=True),
    Column("entity_id", Integer, ForeignKey("entities.id"), primary_key=True),
)


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # person/company/project/technology
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    merged_into_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("entities.id"), nullable=True
    )

    # Relationships
    entries = relationship("Entry", secondary="entry_entities", back_populates="entities")
