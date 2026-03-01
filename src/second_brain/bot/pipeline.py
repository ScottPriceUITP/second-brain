"""Shared capture pipeline helpers used by message, voice, and command handlers.

These functions are transaction-aware: they accept an active SQLAlchemy session
and operate within the caller's transaction boundary.
"""

import logging
from datetime import timedelta

from second_brain.utils.time import utc_now

logger = logging.getLogger(__name__)


def get_recent_calendar_events(session_factory) -> list[dict] | None:
    """Fetch recent/upcoming calendar events for enrichment context."""
    try:
        from second_brain.models.calendar_event import CalendarEvent

        now = utc_now()
        window_start = now - timedelta(hours=2)
        window_end = now + timedelta(hours=4)

        with session_factory() as session:
            events = (
                session.query(CalendarEvent)
                .filter(
                    CalendarEvent.start_time >= window_start,
                    CalendarEvent.start_time <= window_end,
                )
                .order_by(CalendarEvent.start_time)
                .limit(5)
                .all()
            )

            if not events:
                return None

            return [
                {
                    "id": e.id,
                    "title": e.title,
                    "start_time": e.start_time.isoformat(),
                    "attendees": e.attendees,
                    "description": e.description,
                }
                for e in events
            ]
    except Exception:
        logger.debug("Could not fetch calendar events for enrichment context")
        return None


def store_tags(session, entry, tag_names: list[str]) -> None:
    """Create or get-existing tags and link them to the entry."""
    if not tag_names:
        return

    from second_brain.models.tag import Tag

    for tag_name in tag_names:
        tag_name = tag_name.strip().lower()
        if not tag_name:
            continue

        tag = session.query(Tag).filter(Tag.name == tag_name).first()
        if not tag:
            tag = Tag(name=tag_name)
            session.add(tag)
            session.flush()

        if tag not in entry.tags:
            entry.tags.append(tag)


def resolve_entities(session, entry, extracted_entities):
    """Resolve extracted entities via EntityResolutionService and link to entry.

    Creates a per-request EntityResolutionService with the current session
    so that entity creation and linking happen within the same transaction.

    Returns:
        ResolvedEntities or None on failure.
    """
    if not extracted_entities:
        return None

    try:
        from second_brain.services.entity_resolution import EntityResolutionService

        service = EntityResolutionService(session=session)

        entity_dicts = [
            {"name": e.name, "type": e.type} for e in extracted_entities
        ]
        resolved = service.resolve_entities(
            extracted_entities=entity_dicts,
        )

        # Link auto-linked and new entities to the entry
        from second_brain.models.entity import Entity

        for linked in resolved.auto_linked:
            entity = session.get(Entity, linked.entity_id)
            if entity and entity not in entry.entities:
                entry.entities.append(entity)

        for new_ent in resolved.new_created:
            entity = session.get(Entity, new_ent.entity_id)
            if entity and entity not in entry.entities:
                entry.entities.append(entity)

        return resolved
    except Exception:
        logger.exception("Entity resolution failed for entry %d", entry.id)
        return None


def score_connections(anthropic_client, session, entry):
    """Score connections between this entry and existing entries.

    Creates a per-request ConnectionScoringService with the current session
    so that relation creation happens within the same transaction.

    Returns:
        List of ScoredConnection objects, or empty list.
    """
    if not anthropic_client:
        return []

    try:
        from second_brain.services.connection_scoring import ConnectionScoringService

        service = ConnectionScoringService(client=anthropic_client, session=session)
        return service.score_connections(entry=entry)
    except Exception:
        logger.exception("Connection scoring failed for entry %d", entry.id)
        return []
