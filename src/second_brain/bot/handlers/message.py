"""Text message handler — receives text from Telegram, enriches, and stores entries."""

import logging
from datetime import date, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, MessageHandler, filters

from second_brain.bot.formatting import (
    format_capture_confirmation,
    format_error,
    format_query_response,
)
from second_brain.utils.time import utc_now

logger = logging.getLogger(__name__)


async def handle_text_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle an incoming text message through the capture/query pipeline.

    Flow:
    1. Create Entry with status='pending_enrichment'
    2. Enrich via EnrichmentService
    3. Route based on intent (query vs capture)
    4. For captures: resolve entities, score connections, confirm
    5. On failure: keep pending_enrichment, notify user
    """
    message = update.message
    if not message or not message.text:
        return

    raw_text = message.text
    session_factory = context.bot_data.get("db_session_factory")
    enrichment_service = context.bot_data.get("enrichment")

    if not session_factory:
        await message.reply_text(format_error("Database not available."))
        return

    # Step 1: Store entry with pending_enrichment status
    from second_brain.models.entry import Entry

    with session_factory() as session:
        entry = Entry(
            raw_text=raw_text,
            source="telegram_text",
            status="pending_enrichment",
            telegram_message_id=message.message_id,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(entry)
        session.commit()
        entry_id = entry.id

    if not enrichment_service:
        await message.reply_text(format_error("Enrichment service not available."))
        return

    # Step 2: Enrich
    try:
        # Fetch recent calendar events for context
        calendar_events = _get_recent_calendar_events(session_factory)

        enrichment_result = enrichment_service.enrich_text(
            raw_text=raw_text,
            calendar_events=calendar_events,
        )
    except Exception:
        logger.exception("Enrichment failed for entry %d", entry_id)
        await message.reply_text(
            format_error("Could not process your message. It has been saved and will be retried.")
        )
        return

    # Step 3: Route based on intent
    if enrichment_result.intent == "query":
        await _handle_query(update, context, raw_text, entry_id, session_factory)
        return

    # Step 4: Capture — update entry with enrichment results
    try:
        resolved = None
        strong_connections = []

        with session_factory() as session:
            entry = session.get(Entry, entry_id)
            if not entry:
                logger.error("Entry %d not found after enrichment", entry_id)
                return

            entry.clean_text = enrichment_result.clean_text
            entry.entry_type = enrichment_result.entry_type
            entry.is_open_loop = enrichment_result.is_open_loop
            entry.status = "open"

            if enrichment_result.follow_up_date:
                try:
                    entry.follow_up_date = date.fromisoformat(
                        enrichment_result.follow_up_date
                    )
                except ValueError:
                    logger.warning(
                        "Invalid follow_up_date from enrichment: %s",
                        enrichment_result.follow_up_date,
                    )

            if enrichment_result.calendar_event_id:
                entry.calendar_event_id = enrichment_result.calendar_event_id

            # Store tags
            _store_tags(session, entry, enrichment_result.tags)

            # Resolve entities (creates service with current session)
            resolved = _resolve_entities(
                context, session, entry, enrichment_result.entities
            )

            # Score connections (creates service with current session)
            strong_connections = _score_connections(context, session, entry)

            session.commit()

        # Step 5: Send confirmation
        has_connections = bool(strong_connections)
        confirmation = format_capture_confirmation(
            enrichment_result.entry_type, has_connections=has_connections
        )

        if strong_connections:
            connection_summaries = []
            for conn in strong_connections[:3]:
                if conn.entry and conn.entry.clean_text:
                    preview = conn.entry.clean_text[:60]
                else:
                    preview = f"entry #{conn.entry_id}"
                connection_summaries.append(f"  - {preview}")
            confirmation += "\n\nRelated:\n" + "\n".join(connection_summaries)

        # Send entity disambiguation prompts if any
        if resolved and resolved.ambiguous:
            await _send_disambiguation_prompts(
                message, entry_id, resolved.ambiguous
            )

        await message.reply_text(confirmation)

    except Exception:
        logger.exception("Failed to store enrichment results for entry %d", entry_id)
        await message.reply_text(
            format_error("Could not process your message. It has been saved and will be retried.")
        )


async def _handle_query(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_text: str,
    entry_id: int,
    session_factory,
) -> None:
    """Route a query-intent message to the query engine."""
    query_engine = context.bot_data.get("query_engine")

    if not query_engine:
        await update.message.reply_text("Query system not available.")
        # Mark entry as a query that couldn't be processed
        from second_brain.models.entry import Entry

        with session_factory() as session:
            entry = session.get(Entry, entry_id)
            if entry:
                entry.entry_type = "personal"
                entry.status = "open"
                entry.clean_text = raw_text
                session.commit()
        return

    try:
        session_manager = context.bot_data.get("query_session_manager")
        session_ctx = None
        if session_manager:
            session_ctx = session_manager.session

        result = query_engine.handle_query(raw_text, session_context=session_ctx)

        # Update session
        if session_manager:
            source_ids = [s.entry_id for s in result.sources]
            session_manager.update(raw_text, result.answer, source_ids)

        response_text = format_query_response(
            result.answer, result.sources if hasattr(result, "sources") else []
        )
        await update.message.reply_text(response_text)
    except Exception:
        logger.exception("Query engine failed for entry %d", entry_id)
        await update.message.reply_text(
            format_error("Could not process your query. Please try again.")
        )


def _get_recent_calendar_events(session_factory) -> list[dict] | None:
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


def _store_tags(session, entry, tag_names: list[str]) -> None:
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


def _resolve_entities(context, session, entry, extracted_entities):
    """Resolve extracted entities via EntityResolutionService and link to entry.

    Creates a per-request EntityResolutionService with the current session
    so that entity creation and linking happen within the same transaction.
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


def _score_connections(context, session, entry):
    """Score connections between this entry and existing entries.

    Creates a per-request ConnectionScoringService with the current session
    so that relation creation happens within the same transaction.
    """
    anthropic_client = context.bot_data.get("anthropic_client")
    if not anthropic_client:
        return []

    try:
        from second_brain.services.connection_scoring import ConnectionScoringService

        service = ConnectionScoringService(client=anthropic_client, session=session)
        return service.score_connections(entry=entry)
    except Exception:
        logger.exception("Connection scoring failed for entry %d", entry.id)
        return []


async def _send_disambiguation_prompts(message, entry_id, ambiguous_entities):
    """Send inline keyboard prompts for ambiguous entity matches."""
    for amb in ambiguous_entities:
        buttons = []
        for entity_id, entity_name, score in amb.candidates[:3]:
            buttons.append(
                InlineKeyboardButton(
                    f"{entity_name} ({score:.0%})",
                    callback_data=f"entity:{entry_id}:{entity_id}",
                )
            )
        # Add a "Create new" button
        buttons.append(
            InlineKeyboardButton(
                f"New: {amb.extracted_name}",
                callback_data=f"entity:{entry_id}:new:{amb.extracted_name}:{amb.extracted_type}",
            )
        )

        keyboard = InlineKeyboardMarkup([buttons])
        await message.reply_text(
            f"Who is '{amb.extracted_name}'?",
            reply_markup=keyboard,
        )


def register(application) -> None:
    """Register the text message handler."""
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text_message,
        )
    )
    logger.info("Text message handler registered")
