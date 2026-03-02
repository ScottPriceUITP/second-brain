"""Text message handler -- receives text from Slack, enriches, and stores entries."""

import logging
from datetime import date

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


async def handle_text_message(event, say, context):
    """Handle an incoming text message through the capture/query pipeline.

    Flow:
    1. Create Entry with status='pending_enrichment'
    2. Enrich via EnrichmentService
    3. Route based on intent (query vs capture)
    4. For captures: resolve entities, score connections, confirm
    5. On failure: keep pending_enrichment, notify user
    """
    # Filter out messages with subtypes (edits, bot messages, joins, etc.)
    if event.get("subtype") is not None:
        return

    # Skip threaded replies — those are handled by nudge_reply_handler in callbacks.py
    if event.get("thread_ts"):
        return

    raw_text = event.get("text", "")
    if not raw_text:
        return

    services = context.get("services", {})
    session_factory = services.get("db_session_factory")
    enrichment_service = services.get("enrichment")

    if not session_factory:
        await say(text=format_error("Database not available."))
        return

    platform_message_id = event.get("ts")

    # Step 1: Store entry with pending_enrichment status
    from second_brain.models.entry import Entry

    with session_factory() as session:
        entry = Entry(
            raw_text=raw_text,
            source="slack_text",
            status="pending_enrichment",
            platform_message_id=platform_message_id,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(entry)
        session.commit()
        entry_id = entry.id

    if not enrichment_service:
        await say(text=format_error("Enrichment service not available."))
        return

    # Step 2: Enrich
    try:
        calendar_events = get_recent_calendar_events(session_factory)

        enrichment_result = enrichment_service.enrich_text(
            raw_text=raw_text,
            calendar_events=calendar_events,
        )
    except Exception:
        logger.exception("Enrichment failed for entry %d", entry_id)
        await say(
            text=format_error("Could not process your message. It has been saved and will be retried.")
        )
        return

    # Step 3: Route based on intent
    if enrichment_result.intent == "query":
        await _handle_query(say, context, raw_text, entry_id, session_factory)
        return

    # Step 4: Capture -- update entry with enrichment results
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
            store_tags(session, entry, enrichment_result.tags)

            # Resolve entities (creates service with current session)
            resolved = resolve_entities(
                session, entry, enrichment_result.entities
            )

            # Score connections (creates service with current session)
            anthropic_client = services.get("anthropic_client")
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

        # Step 5: Send confirmation
        confirmation = format_capture_confirmation(enrichment_result.entry_type)

        if connection_previews:
            confirmation += "\n\nRelated:\n" + "\n".join(connection_previews)

        # Send entity disambiguation prompts if any
        if resolved and resolved.ambiguous:
            await _send_disambiguation_prompts(
                say, entry_id, resolved.ambiguous
            )

        await say(text=confirmation)

    except Exception:
        logger.exception("Failed to store enrichment results for entry %d", entry_id)
        await say(
            text=format_error("Could not process your message. It has been saved and will be retried.")
        )


async def _handle_query(say, context, raw_text, entry_id, session_factory):
    """Route a query-intent message to the query engine."""
    services = context.get("services", {})
    query_engine = services.get("query_engine")

    if not query_engine:
        await say(text="Query system not available.")
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
        session_manager = services.get("query_session_manager")
        session_ctx = None
        if session_manager:
            session_ctx = session_manager.session

        result = query_engine.handle_query(raw_text, session_context=session_ctx)

        # Update session
        if session_manager:
            source_ids = [s.entry_id for s in result.sources]
            session_manager.update(raw_text, result.answer, source_ids)

        # Mark entry as a processed query
        from second_brain.models.entry import Entry

        with session_factory() as session:
            entry = session.get(Entry, entry_id)
            if entry:
                entry.entry_type = "personal"
                entry.status = "open"
                entry.clean_text = raw_text
                session.commit()

        sources = [
            {"date": s.date, "entry_id": s.entry_id}
            for s in result.sources
        ]
        response_text = format_query_response(result.answer, sources)
        await say(text=response_text)
    except Exception:
        logger.exception("Query engine failed for entry %d", entry_id)
        await say(
            text=format_error("Could not process your query. Please try again.")
        )


async def _send_disambiguation_prompts(say, entry_id, ambiguous_entities):
    """Send Block Kit action prompts for ambiguous entity matches."""
    for amb in ambiguous_entities:
        buttons = []
        for entity_id, entity_name, score in amb.candidates[:3]:
            buttons.append(
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": f"{entity_name} ({score:.0%})",
                    },
                    "action_id": f"entity_select:{entry_id}:{entity_id}",
                    "value": f"{entity_id}",
                }
            )
        # Add a "Create new" button
        # Slack limits action_id to 255 chars; truncate name to fit
        new_action_prefix = f"entity_new:{entry_id}:"
        new_action_suffix = f":{amb.extracted_type}"
        max_name_len = 255 - len(new_action_prefix) - len(new_action_suffix)
        truncated_name = amb.extracted_name[:max_name_len]
        display_name = amb.extracted_name[:30] + "..." if len(amb.extracted_name) > 30 else amb.extracted_name
        buttons.append(
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": f"New: {display_name}",
                },
                "action_id": f"{new_action_prefix}{truncated_name}{new_action_suffix}",
                "value": "new",
            }
        )

        name = amb.extracted_name
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"Who is '{name}'?"},
            },
            {"type": "actions", "elements": buttons},
        ]
        await say(text=f"Who is '{name}'?", blocks=blocks)


def register(app) -> None:
    """Register the text message handler on the Slack Bolt AsyncApp."""

    @app.event("message")
    async def _message_listener(event, say, context):
        await handle_text_message(event, say, context)

    logger.info("Text message handler registered")
