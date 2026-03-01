"""Voice message handler — transcribes audio via Whisper and feeds into enrichment."""

import logging
from datetime import date, datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters

from second_brain.bot.formatting import format_capture_confirmation, format_error

logger = logging.getLogger(__name__)

# Confidence threshold: above this, auto-process without user confirmation
CONFIDENCE_THRESHOLD = 0.8


async def handle_voice_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle an incoming voice message.

    Flow:
    1. Store entry with status='pending_transcription'
    2. Download audio via Telegram Bot API
    3. Transcribe via WhisperClient
    4. High confidence (>= 0.8): feed into text enrichment silently
    5. Low confidence: ask user to confirm transcription
    6. On failure: keep pending_transcription, store audio_file_id for retry
    """
    message = update.message
    if not message or not message.voice:
        return

    voice = message.voice
    session_factory = context.bot_data.get("db_session_factory")
    whisper_client = context.bot_data.get("whisper_client")

    if not session_factory:
        await message.reply_text(format_error("Database not available."))
        return

    # Step 1: Store entry with pending_transcription status
    from second_brain.models.entry import Entry

    with session_factory() as session:
        entry = Entry(
            raw_text="",
            source="telegram_voice",
            status="pending_transcription",
            telegram_message_id=message.message_id,
            audio_file_id=voice.file_id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(entry)
        session.commit()
        entry_id = entry.id

    if not whisper_client:
        await message.reply_text(
            format_error("Voice transcription not available. Audio saved for later processing.")
        )
        return

    # Step 2: Download audio
    try:
        file = await context.bot.get_file(voice.file_id)
        audio_bytes = await file.download_as_bytearray()
    except Exception:
        logger.exception("Failed to download voice file for entry %d", entry_id)
        await message.reply_text(
            format_error("Could not download audio. It has been saved for retry.")
        )
        return

    # Step 3: Transcribe
    try:
        result = whisper_client.transcribe(
            audio_file=bytes(audio_bytes),
            filename=f"voice_{voice.file_unique_id}.ogg",
        )
    except Exception:
        logger.exception("Whisper transcription failed for entry %d", entry_id)
        await message.reply_text(
            format_error("Transcription failed. Audio saved for retry.")
        )
        return

    # Store raw transcription
    with session_factory() as session:
        entry = session.get(Entry, entry_id)
        if entry:
            entry.raw_text = result.text
            session.commit()

    # Step 4/5: Route based on confidence
    if result.confidence >= CONFIDENCE_THRESHOLD:
        # High confidence — feed into enrichment silently
        await _enrich_transcription(
            update, context, entry_id, result.text, session_factory
        )
    else:
        # Low confidence — ask user to confirm
        context.user_data[f"pending_voice_entry_{entry_id}"] = entry_id
        await message.reply_text(
            f"I heard: \"{result.text}\"\n\nIs this correct? "
            "Reply 'yes' to confirm, or send the corrected text."
        )


async def _enrich_transcription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    entry_id: int,
    transcribed_text: str,
    session_factory,
) -> None:
    """Run enrichment on transcribed text and store results.

    This follows the same enrichment flow as the text message handler,
    but the raw_text is the Whisper transcription output.
    """
    from second_brain.models.entry import Entry

    enrichment_service = context.bot_data.get("enrichment")
    message = update.message

    if not enrichment_service:
        with session_factory() as session:
            entry = session.get(Entry, entry_id)
            if entry:
                entry.status = "pending_enrichment"
                session.commit()
        await message.reply_text(
            format_error("Enrichment not available. Transcription saved for later processing.")
        )
        return

    try:
        calendar_events = _get_recent_calendar_events(session_factory)

        enrichment_result = enrichment_service.enrich_text(
            raw_text=transcribed_text,
            calendar_events=calendar_events,
        )
    except Exception:
        logger.exception("Enrichment failed for voice entry %d", entry_id)
        with session_factory() as session:
            entry = session.get(Entry, entry_id)
            if entry:
                entry.status = "pending_enrichment"
                session.commit()
        await message.reply_text(
            format_error("Could not process transcription. It has been saved for retry.")
        )
        return

    # Handle query intent
    if enrichment_result.intent == "query":
        query_engine = context.bot_data.get("query_engine")
        if not query_engine:
            await message.reply_text("Query system not available.")
            return
        try:
            from second_brain.bot.formatting import format_query_response

            result = query_engine.query(transcribed_text)
            response_text = format_query_response(
                result.response, result.sources if hasattr(result, "sources") else []
            )
            await message.reply_text(response_text)
        except Exception:
            logger.exception("Query failed for voice entry %d", entry_id)
            await message.reply_text(
                format_error("Could not process your query. Please try again.")
            )
        return

    # Store enrichment results
    try:
        strong_connections = []

        with session_factory() as session:
            entry = session.get(Entry, entry_id)
            if not entry:
                logger.error("Voice entry %d not found after enrichment", entry_id)
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
                    pass

            if enrichment_result.calendar_event_id:
                entry.calendar_event_id = enrichment_result.calendar_event_id

            # Store tags
            _store_tags(session, entry, enrichment_result.tags)

            # Resolve entities (creates service with current session)
            _resolve_entities(context, session, entry, enrichment_result.entities)

            # Score connections (creates service with current session)
            strong_connections = _score_connections(context, session, entry)

            session.commit()

        has_connections = bool(strong_connections)
        confirmation = format_capture_confirmation(
            enrichment_result.entry_type, has_connections=has_connections
        )

        if strong_connections:
            summaries = []
            for conn in strong_connections[:3]:
                if conn.entry and conn.entry.clean_text:
                    preview = conn.entry.clean_text[:60]
                else:
                    preview = f"entry #{conn.entry_id}"
                summaries.append(f"  - {preview}")
            confirmation += "\n\nRelated:\n" + "\n".join(summaries)

        await message.reply_text(confirmation)

    except Exception:
        logger.exception("Failed to store enrichment for voice entry %d", entry_id)
        await message.reply_text(
            format_error("Could not process transcription. It has been saved for retry.")
        )


def _get_recent_calendar_events(session_factory) -> list[dict] | None:
    """Fetch recent/upcoming calendar events for enrichment context."""
    try:
        from second_brain.models.calendar_event import CalendarEvent

        now = datetime.now(timezone.utc)
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
        logger.debug("Could not fetch calendar events for voice enrichment context")
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
        logger.exception("Entity resolution failed for voice entry %d", entry.id)
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
        logger.exception("Connection scoring failed for voice entry %d", entry.id)
        return []


def register(application) -> None:
    """Register the voice message handler."""
    application.add_handler(
        MessageHandler(
            filters.VOICE,
            handle_voice_message,
        )
    )
    logger.info("Voice message handler registered")
