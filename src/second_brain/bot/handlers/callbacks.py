"""Callback query handlers for inline buttons (nudges and entity disambiguation)."""

import logging
from datetime import date, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters
from second_brain.utils.time import utc_now

logger = logging.getLogger(__name__)


async def nudge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle nudge inline button presses: Done, Snooze, Drop."""
    query = update.callback_query
    await query.answer()

    data = query.data  # Format: nudge:{action}:{nudge_id}
    parts = data.split(":")
    if len(parts) != 3:
        logger.warning("Invalid nudge callback data: %s", data)
        return

    _, action, nudge_id_str = parts
    nudge_id = int(nudge_id_str)

    nudge_manager = context.bot_data.get("nudge_manager")
    if not nudge_manager:
        await query.edit_message_text("Service unavailable.")
        return

    if action == "done":
        confirmation = nudge_manager.handle_nudge_action(nudge_id, "done")
        await query.edit_message_text(
            f"{query.message.text}\n\n-- {confirmation}"
        )

    elif action == "snooze":
        # Show snooze sub-menu
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Tomorrow",
                        callback_data=f"snooze:tomorrow:{nudge_id}",
                    ),
                    InlineKeyboardButton(
                        "Next Week",
                        callback_data=f"snooze:week:{nudge_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "Custom Date...",
                        callback_data=f"snooze:custom:{nudge_id}",
                    ),
                ],
            ]
        )
        await query.edit_message_reply_markup(reply_markup=keyboard)

    elif action == "drop":
        confirmation = nudge_manager.handle_nudge_action(nudge_id, "dropped")
        await query.edit_message_text(
            f"{query.message.text}\n\n-- {confirmation}"
        )


async def snooze_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle snooze sub-menu button presses."""
    query = update.callback_query
    await query.answer()

    data = query.data  # Format: snooze:{duration}:{nudge_id}
    parts = data.split(":")
    if len(parts) != 3:
        logger.warning("Invalid snooze callback data: %s", data)
        return

    _, duration, nudge_id_str = parts
    nudge_id = int(nudge_id_str)

    nudge_manager = context.bot_data.get("nudge_manager")
    if not nudge_manager:
        await query.edit_message_text("Service unavailable.")
        return

    today = date.today()

    if duration == "tomorrow":
        snooze_until = today + timedelta(days=1)
        confirmation = nudge_manager.handle_nudge_action(
            nudge_id, "snoozed", snooze_until=snooze_until
        )
        await query.edit_message_text(
            f"{query.message.text}\n\n-- {confirmation}"
        )

    elif duration == "week":
        snooze_until = today + timedelta(days=7)
        confirmation = nudge_manager.handle_nudge_action(
            nudge_id, "snoozed", snooze_until=snooze_until
        )
        await query.edit_message_text(
            f"{query.message.text}\n\n-- {confirmation}"
        )

    elif duration == "custom":
        # Store the nudge_id so we can handle the follow-up text message
        context.user_data["pending_snooze_nudge_id"] = nudge_id
        await query.edit_message_text(
            f"{query.message.text}\n\nWhen should I remind you? "
            "(e.g., 'next Thursday', '2026-03-15')"
        )


async def custom_snooze_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle free-text custom snooze date after user taps 'Custom Date'."""
    nudge_id = context.user_data.pop("pending_snooze_nudge_id", None)
    if nudge_id is None:
        return

    nudge_manager = context.bot_data.get("nudge_manager")
    if not nudge_manager:
        await update.message.reply_text("Service unavailable.")
        return

    user_text = update.message.text
    action, snooze_date = nudge_manager.parse_natural_language_response(
        nudge_id, user_text
    )

    if action == "snoozed" and snooze_date:
        confirmation = nudge_manager.handle_nudge_action(
            nudge_id, "snoozed", snooze_until=snooze_date
        )
    elif action == "done":
        confirmation = nudge_manager.handle_nudge_action(nudge_id, "done")
    elif action == "dropped":
        confirmation = nudge_manager.handle_nudge_action(nudge_id, "dropped")
    else:
        # Default: snooze to tomorrow
        snooze_date = date.today() + timedelta(days=1)
        confirmation = nudge_manager.handle_nudge_action(
            nudge_id, "snoozed", snooze_until=snooze_date
        )

    await update.message.reply_text(confirmation)


async def entity_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle entity disambiguation inline button presses."""
    query = update.callback_query
    await query.answer()

    data = query.data  # Format: entity:{entry_id}:{entity_id} or entity:{entry_id}:new:{name}:{type}
    parts = data.split(":")
    if len(parts) < 3:
        logger.warning("Invalid entity callback data: %s", data)
        return

    entry_id = int(parts[1])
    session_factory = context.bot_data.get("db_session_factory")
    if not session_factory:
        await query.edit_message_text("Service unavailable.")
        return

    if parts[2] == "new":
        # Create new entity — name and type passed in callback data
        if len(parts) >= 5:
            entity_name = parts[3]
            entity_type = parts[4]
        else:
            await query.edit_message_text("Created as new entity.")
            return

        with session_factory() as session:
            from second_brain.models.entity import Entity, entry_entities

            entity = Entity(
                name=entity_name,
                type=entity_type,
                created_at=utc_now(),
            )
            session.add(entity)
            session.flush()

            session.execute(
                entry_entities.insert().values(
                    entry_id=entry_id, entity_id=entity.id
                )
            )
            session.commit()
            logger.info(
                "New entity created from disambiguation: '%s' (type=%s, id=%d)",
                entity_name,
                entity_type,
                entity.id,
            )

        await query.edit_message_text(f"Created new entity: {entity_name}")

    else:
        # Link to existing entity
        entity_id = int(parts[2])

        with session_factory() as session:
            from second_brain.models.entity import entry_entities

            # Check if link already exists
            from sqlalchemy import select

            existing = session.execute(
                select(entry_entities).where(
                    entry_entities.c.entry_id == entry_id,
                    entry_entities.c.entity_id == entity_id,
                )
            ).first()

            if not existing:
                session.execute(
                    entry_entities.insert().values(
                        entry_id=entry_id, entity_id=entity_id
                    )
                )
                session.commit()

            # Get entity name for confirmation
            from second_brain.models.entity import Entity

            entity = session.get(Entity, entity_id)
            entity_name = entity.name if entity else f"#{entity_id}"

        logger.info(
            "Entity disambiguation resolved: entry=%d linked to entity=%d",
            entry_id,
            entity_id,
        )
        await query.edit_message_text(f"Linked to: {entity_name}")


async def nudge_reply_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle natural language replies to nudge messages.

    When a user replies to a nudge message with text, parse intent with Haiku
    and route to the appropriate NudgeManager action.
    """
    message = update.message
    if not message or not message.reply_to_message:
        return

    # Check if the replied-to message is a nudge (has nudge inline buttons or
    # matches a nudge telegram_message_id)
    replied_msg_id = message.reply_to_message.message_id
    nudge_manager = context.bot_data.get("nudge_manager")
    if not nudge_manager:
        return

    session_factory = context.bot_data.get("db_session_factory")
    if not session_factory:
        return

    # Look up the nudge by telegram_message_id
    from second_brain.models.nudge import NudgeHistory

    with session_factory() as session:
        nudge = (
            session.query(NudgeHistory)
            .filter(NudgeHistory.telegram_message_id == replied_msg_id)
            .first()
        )
        if not nudge:
            return
        nudge_id = nudge.id

    # Parse the natural language response
    action, snooze_date = nudge_manager.parse_natural_language_response(
        nudge_id, message.text
    )

    confirmation = nudge_manager.handle_nudge_action(
        nudge_id, action, snooze_until=snooze_date
    )
    await message.reply_text(confirmation)


def register(application) -> None:
    """Register all callback query and related message handlers."""
    # Nudge button callbacks: nudge:{action}:{id}
    application.add_handler(
        CallbackQueryHandler(nudge_callback, pattern=r"^nudge:")
    )

    # Snooze sub-menu callbacks: snooze:{duration}:{id}
    application.add_handler(
        CallbackQueryHandler(snooze_callback, pattern=r"^snooze:")
    )

    # Entity disambiguation callbacks: entity:{entry_id}:{entity_id_or_new}
    application.add_handler(
        CallbackQueryHandler(entity_callback, pattern=r"^entity:")
    )

    # Custom snooze date text input (high priority text handler).
    # The handler checks user_data internally and returns early if no pending snooze.
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            custom_snooze_text,
        ),
        group=-1,  # High priority group to intercept before normal message handlers
    )

    # Natural language reply to nudge messages
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.REPLY,
            nudge_reply_handler,
        ),
        group=-2,  # Even higher priority for reply detection
    )

    logger.info("Callback handlers registered")
