"""Slack slash-command handlers for /ask, /note, /config, /bot-status, /open-loops, /search-entries."""

import logging

from second_brain.bot.formatting import (
    format_capture_confirmation,
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


async def ask_command(ack, command, say, context):
    """/ask <question> -- force query mode, bypass intent detection."""
    await ack()

    question = (command.get("text") or "").strip()
    if not question:
        await say(text="Usage: /ask <question>")
        return

    services = context.get("services", {})
    query_engine = services.get("query_engine")
    if not query_engine:
        await say(text="Query system not yet available.")
        return

    session_manager = services.get("query_session_manager")
    session_ctx = None
    if session_manager:
        session_ctx = session_manager.session

    try:
        result = query_engine.handle_query(question, session_context=session_ctx)

        # Update session
        if session_manager:
            source_ids = [s.entry_id for s in result.sources]
            session_manager.update(question, result.answer, source_ids)

        # Format and send
        sources = [
            {"date": s.date, "entry_id": s.entry_id}
            for s in result.sources
        ]
        formatted = format_query_response(result.answer, sources)
        await say(text=formatted)

    except Exception:
        logger.exception("Error handling /ask command")
        await say(text="An error occurred while processing your query.")


async def note_command(ack, command, say, context):
    """/note <text> -- force capture mode, bypass intent detection."""
    await ack()

    text = (command.get("text") or "").strip()
    if not text:
        await say(text="Usage: /note <text>")
        return

    services = context.get("services", {})
    enrichment = services.get("enrichment")
    if not enrichment:
        await say(text="Enrichment service not yet available.")
        return

    session_factory = services.get("db_session_factory")
    if not session_factory:
        await say(text="Database not available.")
        return

    try:
        from second_brain.models.entry import Entry

        # Persist entry first so raw text is never lost
        with session_factory() as session:
            entry = Entry(
                raw_text=text,
                source="slack_text",
                status="pending_enrichment",
                platform_message_id=None,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            session.add(entry)
            session.commit()
            entry_id = entry.id

        # Enrich with calendar context
        calendar_events = get_recent_calendar_events(session_factory)
        result = enrichment.enrich_text(
            raw_text=text,
            calendar_events=calendar_events,
        )

        with session_factory() as session:
            entry = session.get(Entry, entry_id)
            if not entry:
                logger.error("Entry %d not found after enrichment", entry_id)
                return

            entry.clean_text = result.clean_text
            entry.entry_type = result.entry_type
            entry.is_open_loop = result.is_open_loop
            entry.status = "open"

            if result.follow_up_date:
                from datetime import date as date_type

                try:
                    entry.follow_up_date = date_type.fromisoformat(result.follow_up_date)
                except ValueError:
                    logger.warning(
                        "Invalid follow_up_date from enrichment: %s",
                        result.follow_up_date,
                    )

            if result.calendar_event_id:
                entry.calendar_event_id = result.calendar_event_id

            # Store tags
            store_tags(session, entry, result.tags)

            # Resolve entities
            resolve_entities(session, entry, result.entities)

            # Score connections
            anthropic_client = services.get("anthropic_client")
            score_connections(anthropic_client, session, entry)

            session.commit()

            confirmation = format_capture_confirmation(result.entry_type)
            await say(text=confirmation)

    except Exception:
        logger.exception("Error handling /note command")
        await say(text="An error occurred while saving your note.")


async def config_command(ack, command, say, context):
    """/config -- show or set config values."""
    await ack()

    services = context.get("services", {})
    session_factory = services.get("db_session_factory")
    if not session_factory:
        await say(text="Database not available.")
        return

    from second_brain.config import CONFIG_DEFAULTS, get_config, set_config

    args_text = (command.get("text") or "").strip()

    # If an argument is provided in key=value format, update the setting
    if args_text and "=" in args_text:
        eq_idx = args_text.index("=")
        key = args_text[:eq_idx].strip()
        value = args_text[eq_idx + 1:].strip()

        if key not in CONFIG_DEFAULTS:
            await say(text=f"Unknown config key: {key}")
            return

        with session_factory() as session:
            set_config(session, key, value)
        await say(text=f"Config updated: {key} = {value}")
        return

    # Otherwise show all config values
    with session_factory() as session:
        lines = []
        for key in sorted(CONFIG_DEFAULTS.keys()):
            current = get_config(session, key)
            lines.append(f"  {key} = {current}")

    header = "Current configuration:\n"
    await say(text=header + "\n".join(lines))


async def status_command(ack, command, say, context):
    """/status -- system health: pending counts, last scheduler run, API status."""
    await ack()

    services = context.get("services", {})
    session_factory = services.get("db_session_factory")
    if not session_factory:
        await say(text="Database not available.")
        return

    from second_brain.models.entry import Entry

    lines = ["System Status:"]

    with session_factory() as session:
        # Pending enrichment count
        pending_enrichment = (
            session.query(Entry)
            .filter(Entry.status == "pending_enrichment")
            .count()
        )
        lines.append(f"  Pending enrichment: {pending_enrichment}")

        # Total entries
        total = session.query(Entry).count()
        lines.append(f"  Total entries: {total}")

        # Open loops
        open_loops = (
            session.query(Entry)
            .filter(Entry.is_open_loop.is_(True), Entry.status == "open")
            .count()
        )
        lines.append(f"  Open loops: {open_loops}")

    # Scheduler info
    scheduler = services.get("scheduler")
    if scheduler and hasattr(scheduler, "scheduler") and scheduler.scheduler.running:
        lines.append("  Scheduler: running")
    else:
        lines.append("  Scheduler: not running")

    # API connectivity check
    anthropic_client = services.get("anthropic_client")
    if anthropic_client:
        lines.append("  Anthropic API: configured")
    else:
        lines.append("  Anthropic API: not configured")

    await say(text="\n".join(lines))


async def open_command(ack, command, say, context):
    """/open -- list current open loops."""
    await ack()

    services = context.get("services", {})
    session_factory = services.get("db_session_factory")
    if not session_factory:
        await say(text="Database not available.")
        return

    from second_brain.models.entry import Entry

    with session_factory() as session:
        open_entries = (
            session.query(Entry)
            .filter(Entry.is_open_loop.is_(True), Entry.status == "open")
            .order_by(Entry.created_at.desc())
            .limit(20)
            .all()
        )

        if not open_entries:
            await say(text="No open loops.")
            return

        lines = [f"Open loops ({len(open_entries)}):"]
        for entry in open_entries:
            text = entry.clean_text or entry.raw_text or "(empty)"
            snippet = text[:100]
            if len(text) > 100:
                snippet += "..."
            date_str = entry.created_at.strftime("%Y-%m-%d")
            line = f"\n[{date_str}] {snippet}"
            if entry.follow_up_date:
                line += f"\n  Follow-up: {entry.follow_up_date.isoformat()}"
            lines.append(line)

    await say(text="\n".join(lines))


async def search_command(ack, command, say, context):
    """/search <term> -- explicit FTS search."""
    await ack()

    term = (command.get("text") or "").strip()
    if not term:
        await say(text="Usage: /search <term>")
        return

    services = context.get("services", {})
    session_factory = services.get("db_session_factory")
    if not session_factory:
        await say(text="Database not available.")
        return

    from second_brain.utils.fts import fts_search

    with session_factory() as session:
        results = fts_search(session, term, limit=10)

        if not results:
            await say(text=f"No results for: {term}")
            return

        lines = [f"Search results for '{term}' ({len(results)}):"]
        for entry in results:
            text = entry.clean_text or entry.raw_text or "(empty)"
            snippet = text[:120]
            if len(text) > 120:
                snippet += "..."
            date_str = entry.created_at.strftime("%Y-%m-%d")
            lines.append(f"\n[{date_str}, {entry.entry_type}] {snippet}")

    await say(text="\n".join(lines))


def register(app) -> None:
    """Register all slash-command handlers on the Slack Bolt AsyncApp."""
    app.command("/ask")(ask_command)
    app.command("/note")(note_command)
    app.command("/config")(config_command)
    app.command("/bot-status")(status_command)
    app.command("/open-loops")(open_command)
    app.command("/search-entries")(search_command)
    logger.info("Command handlers registered")
