"""SQLAlchemy models — import all models here to register them with Base.metadata."""

from second_brain.models.base import Base
from second_brain.models.calendar_event import CalendarEvent
from second_brain.models.config import ConfigSetting
from second_brain.models.entity import Entity, entry_entities
from second_brain.models.entity_merge import EntityMerge
from second_brain.models.entry import Entry
from second_brain.models.nudge import NudgeHistory
from second_brain.models.relation import EntryRelation
from second_brain.models.tag import Tag, entry_tags

__all__ = [
    "Base",
    "CalendarEvent",
    "ConfigSetting",
    "Entity",
    "EntityMerge",
    "Entry",
    "EntryRelation",
    "NudgeHistory",
    "Tag",
    "entry_entities",
    "entry_tags",
]
