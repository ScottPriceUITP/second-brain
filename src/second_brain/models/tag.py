"""Tag model and entry-tag junction table."""

from sqlalchemy import Column, ForeignKey, Integer, Table, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from second_brain.models.base import Base

# Junction table for entry-tag many-to-many
entry_tags = Table(
    "entry_tags",
    Base.metadata,
    Column("entry_id", Integer, ForeignKey("entries.id"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)

    # Relationships
    entries = relationship("Entry", secondary="entry_tags", back_populates="tags")
