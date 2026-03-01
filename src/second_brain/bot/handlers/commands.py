"""Telegram command handlers for /ask, /note, /config, /status, /open, /search."""

import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from second_brain.bot.formatting import format_query_response

logger = logging.getLogger(__name__)


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ask <question> — force query mode, bypass intent detection."""
    if not update.message:
        return

    question = " ".join(context.args) if context.args else ""
    if not question:
        await update.message.reply_text("Usage: /ask <question>")
        return

    query_engine = context.bot_data.get("query_engine")
    if not query_engine:
        await update.message.reply_text("Query system not yet available.")
        return

    session_manager = context.bot_data.get("query_session_manager")
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
        await update.message.reply_text(formatted)

    except Exception:
        logger.exception("Error handling /ask command")
        await update.message.reply_text("An error occurred while processing your query.")


async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/note <text> — force capture mode, bypass intent detection."""
    if not update.message:
        return

    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Usage: /note <text>")
        return

    enrichment = context.bot_data.get("enrichment")
    if not enrichment:
        await update.message.reply_text("Enrichment service not yet available.")
        return

    session_factory = context.bot_data.get("db_session_factory")
    if not session_factory:
        await update.message.reply_text("Database not available.")
        return

    try:
        from second_brain.models.entry import Entry

        result = enrichment.enrich_text(text)

        with session_factory() as session:
            entry = Entry(
                raw_text=text,
                clean_text=result.clean_text,
                entry_type=result.entry_type,
                source="telegram_text",
                is_open_loop=result.is_open_loop,
                status="open",
                telegram_message_id=update.message.message_id,
            )
            if result.follow_up_date:
                from datetime import date as date_type

                try:
                    entry.follow_up_date = date_type.fromisoformat(result.follow_up_date)
                except ValueError:
                    pass

            session.add(entry)
            session.commit()

            from second_brain.bot.formatting import format_capture_confirmation

            confirmation = format_capture_confirmation(result.entry_type)
            await update.message.reply_text(confirmation)

    except Exception:
        logger.exception("Error handling /note command")
        await update.message.reply_text("An error occurred while saving your note.")


async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/config — show or set config values."""
    if not update.message:
        return

    session_factory = context.bot_data.get("db_session_factory")
    if not session_factory:
        await update.message.reply_text("Database not available.")
        return

    from second_brain.config import CONFIG_DEFAULTS, get_config, set_config

    args = context.args or []

    # If an argument is provided in key=value format, update the setting
    if args and "=" in args[0]:
        key_value = " ".join(args)
        eq_idx = key_value.index("=")
        key = key_value[:eq_idx].strip()
        value = key_value[eq_idx + 1 :].strip()

        if key not in CONFIG_DEFAULTS:
            await update.message.reply_text(f"Unknown config key: {key}")
            return

        with session_factory() as session:
            set_config(session, key, value)
        await update.message.reply_text(f"Config updated: {key} = {value}")
        return

    # Otherwise show all config values
    with session_factory() as session:
        lines = []
        for key in sorted(CONFIG_DEFAULTS.keys()):
            current = get_config(session, key)
            lines.append(f"  {key} = {current}")

    header = "Current configuration:\n"
    await update.message.reply_text(header + "\n".join(lines))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — system health: pending counts, last scheduler run, API status."""
    if not update.message:
        return

    session_factory = context.bot_data.get("db_session_factory")
    if not session_factory:
        await update.message.reply_text("Database not available.")
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

        # Pending transcription count
        pending_transcription = (
            session.query(Entry)
            .filter(Entry.status == "pending_transcription")
            .count()
        )
        lines.append(f"  Pending transcription: {pending_transcription}")

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
    scheduler = context.bot_data.get("scheduler")
    if scheduler and hasattr(scheduler, "scheduler") and scheduler.scheduler.running:
        lines.append("  Scheduler: running")
    else:
        lines.append("  Scheduler: not running")

    # API connectivity check
    anthropic_client = context.bot_data.get("anthropic_client")
    if anthropic_client:
        lines.append("  Anthropic API: configured")
    else:
        lines.append("  Anthropic API: not configured")

    await update.message.reply_text("\n".join(lines))


async def open_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/open — list current open loops."""
    if not update.message:
        return

    session_factory = context.bot_data.get("db_session_factory")
    if not session_factory:
        await update.message.reply_text("Database not available.")
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
            await update.message.reply_text("No open loops.")
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

    await update.message.reply_text("\n".join(lines))


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/search <term> — explicit FTS search."""
    if not update.message:
        return

    term = " ".join(context.args) if context.args else ""
    if not term:
        await update.message.reply_text("Usage: /search <term>")
        return

    session_factory = context.bot_data.get("db_session_factory")
    if not session_factory:
        await update.message.reply_text("Database not available.")
        return

    from second_brain.utils.fts import fts_search

    with session_factory() as session:
        results = fts_search(session, term, limit=10)

        if not results:
            await update.message.reply_text(f"No results for: {term}")
            return

        lines = [f"Search results for '{term}' ({len(results)}):"]
        for entry in results:
            text = entry.clean_text or entry.raw_text or "(empty)"
            snippet = text[:120]
            if len(text) > 120:
                snippet += "..."
            date_str = entry.created_at.strftime("%Y-%m-%d")
            lines.append(f"\n[{date_str}, {entry.entry_type}] {snippet}")

    await update.message.reply_text("\n".join(lines))


def register(application) -> None:
    """Register all command handlers."""
    application.add_handler(CommandHandler("ask", ask_command))
    application.add_handler(CommandHandler("note", note_command))
    application.add_handler(CommandHandler("config", config_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("open", open_command))
    application.add_handler(CommandHandler("search", search_command))
    logger.info("Command handlers registered")
