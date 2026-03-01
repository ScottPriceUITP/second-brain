"""Voice message handler — transcribes audio via Whisper and feeds into enrichment."""

import logging
from datetime import date

from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters

from second_brain.bot.formatting import (
    format_capture_confirmation,
    format_error,
    format_query_response,
)
from second_brain.bot.pipeline import (
    get_recent_calendar_events,
    resolve_entities,
    score_connections,
    store_tags,
)
from second_brain.utils.time import utc_now

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
            created_at=utc_now(),
            updated_at=utc_now(),
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
        calendar_events = get_recent_calendar_events(session_factory)

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
            session_manager = context.bot_data.get("query_session_manager")
            session_ctx = None
            if session_manager:
                session_ctx = session_manager.session

            result = query_engine.handle_query(
                transcribed_text, session_context=session_ctx
            )

            # Update session
            if session_manager:
                source_ids = [s.entry_id for s in result.sources]
                session_manager.update(transcribed_text, result.answer, source_ids)

            sources = [
                {"date": s.date, "entry_id": s.entry_id}
                for s in result.sources
            ]
            response_text = format_query_response(result.answer, sources)
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
                    logger.warning(
                        "Invalid follow_up_date from enrichment: %s",
                        enrichment_result.follow_up_date,
                    )

            if enrichment_result.calendar_event_id:
                entry.calendar_event_id = enrichment_result.calendar_event_id

            # Store tags
            store_tags(session, entry, enrichment_result.tags)

            # Resolve entities
            resolve_entities(session, entry, enrichment_result.entities)

            # Score connections
            anthropic_client = context.bot_data.get("anthropic_client")
            strong_connections = score_connections(anthropic_client, session, entry)

            # Extract connection previews while session is still open
            connection_previews = []
            for conn in strong_connections[:3]:
                if conn.entry and conn.entry.clean_text:
                    preview = conn.entry.clean_text[:60]
                else:
                    preview = f"entry #{conn.entry_id}"
                connection_previews.append(f"  - {preview}")

            session.commit()

        confirmation = format_capture_confirmation(enrichment_result.entry_type)

        if connection_previews:
            confirmation += "\n\nRelated:\n" + "\n".join(connection_previews)

        await message.reply_text(confirmation)

    except Exception:
        logger.exception("Failed to store enrichment for voice entry %d", entry_id)
        await message.reply_text(
            format_error("Could not process transcription. It has been saved for retry.")
        )


def register(application) -> None:
    """Register the voice message handler."""
    application.add_handler(
        MessageHandler(
            filters.VOICE,
            handle_voice_message,
        )
    )
    logger.info("Voice message handler registered")
