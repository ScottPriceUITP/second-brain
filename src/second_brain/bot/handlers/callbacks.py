"""Action and view handlers for Slack interactive components (nudges and entity disambiguation)."""

import logging
import re
from datetime import date, timedelta

from second_brain.utils.time import utc_now

logger = logging.getLogger(__name__)


async def nudge_done_handler(ack, action, body, say, context, client):
    """Handle 'Done' button press on a nudge message."""
    await ack()

    nudge_id = int(action["value"])

    services = context.get("services", {})
    nudge_manager = services.get("nudge_manager")
    if not nudge_manager:
        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text="Service unavailable.",
            blocks=[],
        )
        return

    confirmation = nudge_manager.handle_nudge_action(nudge_id, "done")
    original_text = body["message"].get("text", "")
    await client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=f"{original_text}\n\n-- {confirmation}",
        blocks=[],
    )


async def nudge_snooze_handler(ack, action, body, say, context, client):
    """Handle 'Snooze' button press -- show snooze sub-menu."""
    await ack()

    nudge_id = action["value"]
    original_text = body["message"].get("text", "")

    snooze_buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Tomorrow"},
            "action_id": "snooze_tomorrow",
            "value": nudge_id,
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Next Week"},
            "action_id": "snooze_week",
            "value": nudge_id,
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Custom Date..."},
            "action_id": "snooze_custom",
            "value": nudge_id,
        },
    ]

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": original_text},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "Snooze until:"},
        },
        {"type": "actions", "elements": snooze_buttons},
    ]

    await client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=original_text,
        blocks=blocks,
    )


async def nudge_drop_handler(ack, action, body, say, context, client):
    """Handle 'Drop' button press on a nudge message."""
    await ack()

    nudge_id = int(action["value"])

    services = context.get("services", {})
    nudge_manager = services.get("nudge_manager")
    if not nudge_manager:
        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text="Service unavailable.",
            blocks=[],
        )
        return

    confirmation = nudge_manager.handle_nudge_action(nudge_id, "dropped")
    original_text = body["message"].get("text", "")
    await client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=f"{original_text}\n\n-- {confirmation}",
        blocks=[],
    )


async def snooze_tomorrow_handler(ack, action, body, say, context, client):
    """Handle 'Tomorrow' snooze selection."""
    await ack()

    nudge_id = int(action["value"])

    services = context.get("services", {})
    nudge_manager = services.get("nudge_manager")
    if not nudge_manager:
        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text="Service unavailable.",
            blocks=[],
        )
        return

    snooze_until = date.today() + timedelta(days=1)
    confirmation = nudge_manager.handle_nudge_action(
        nudge_id, "snoozed", snooze_until=snooze_until
    )
    original_text = body["message"].get("text", "")
    await client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=f"{original_text}\n\n-- {confirmation}",
        blocks=[],
    )


async def snooze_week_handler(ack, action, body, say, context, client):
    """Handle 'Next Week' snooze selection."""
    await ack()

    nudge_id = int(action["value"])

    services = context.get("services", {})
    nudge_manager = services.get("nudge_manager")
    if not nudge_manager:
        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text="Service unavailable.",
            blocks=[],
        )
        return

    snooze_until = date.today() + timedelta(days=7)
    confirmation = nudge_manager.handle_nudge_action(
        nudge_id, "snoozed", snooze_until=snooze_until
    )
    original_text = body["message"].get("text", "")
    await client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=f"{original_text}\n\n-- {confirmation}",
        blocks=[],
    )


async def snooze_custom_handler(ack, action, body, say, context, client):
    """Handle 'Custom Date...' snooze selection -- open a modal with a date picker."""
    await ack()

    nudge_id = action["value"]
    trigger_id = body["trigger_id"]

    view = {
        "type": "modal",
        "callback_id": "snooze_date_modal",
        "title": {"type": "plain_text", "text": "Custom Snooze Date"},
        "submit": {"type": "plain_text", "text": "Snooze"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": nudge_id,
        "blocks": [
            {
                "type": "input",
                "block_id": "date_block",
                "element": {
                    "type": "datepicker",
                    "action_id": "snooze_date_picker",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Select a date",
                    },
                },
                "label": {"type": "plain_text", "text": "Remind me on:"},
            }
        ],
    }

    await client.views_open(trigger_id=trigger_id, view=view)


async def snooze_date_modal_handler(ack, body, context, client):
    """Handle submission of the custom snooze date modal."""
    await ack()

    nudge_id = int(body["view"]["private_metadata"])
    selected_date_str = (
        body["view"]["state"]["values"]["date_block"]["snooze_date_picker"][
            "selected_date"
        ]
    )

    services = context.get("services", {})
    nudge_manager = services.get("nudge_manager")
    if not nudge_manager:
        return

    snooze_until = date.fromisoformat(selected_date_str)
    confirmation = nudge_manager.handle_nudge_action(
        nudge_id, "snoozed", snooze_until=snooze_until
    )

    # Post confirmation to the user via DM
    user_id = body["user"]["id"]
    await client.chat_postMessage(
        channel=user_id,
        text=confirmation,
    )


async def summary_review_loops_handler(ack, action, body, say, context, client):
    """Handle 'Review open loops' button on daily summary."""
    await ack()

    services = context.get("services", {})
    session_factory = services.get("db_session_factory")
    if not session_factory:
        return

    from second_brain.models.entry import Entry
    from second_brain.bot.formatting import format_nudge_blocks

    nudge_manager = services.get("nudge_manager")

    # Extract loop data inside session, then close before creating nudges
    # (create_nudge opens its own session; SQLite can deadlock with nested sessions)
    with session_factory() as session:
        open_loops = (
            session.query(Entry)
            .filter(Entry.is_open_loop.is_(True), Entry.status == "open")
            .order_by(Entry.created_at.desc())
            .all()
        )

        if not open_loops:
            await client.chat_update(
                channel=body["channel"]["id"],
                ts=body["message"]["ts"],
                text=body["message"].get("text", "") + "\n\n_No open loops right now._",
                blocks=[],
            )
            return

        # Collect data while session is open, then close it
        loop_data = [
            {
                "entry_id": entry.id,
                "snippet": (entry.clean_text or entry.raw_text or "(empty)")[:150],
                "created": entry.created_at.strftime("%Y-%m-%d"),
            }
            for entry in open_loops
        ]

    # Create nudges outside the query session
    loop_blocks = []
    for item in loop_data:
        if nudge_manager:
            nudge_id, _, _ = nudge_manager.create_nudge(
                entry_id=item["entry_id"],
                nudge_type="open_loop",
                message=item["snippet"],
            )
            loop_blocks.extend(format_nudge_blocks(
                f"[{item['created']}] {item['snippet']}", nudge_id
            ))
        else:
            loop_blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"• [{item['created']}] {item['snippet']}"},
            })

    # Update original message with expanded loop list
    original_text = body["message"].get("text", "")
    await client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=original_text,
        blocks=loop_blocks,
    )


async def summary_dismiss_handler(ack, action, body, say, context, client):
    """Handle 'Looks good' button on daily summary — remove buttons."""
    await ack()

    original_text = body["message"].get("text", "")
    await client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=original_text,
        blocks=[],
    )


async def entity_select_handler(ack, action, body, say, context, client):
    """Handle entity disambiguation -- user selected an existing entity."""
    await ack()

    # action_id format: entity_select:{entry_id}:{entity_id}
    parts = action["action_id"].split(":")
    if len(parts) != 3:
        logger.warning("Invalid entity_select action_id: %s", action["action_id"])
        return

    entry_id = int(parts[1])
    entity_id = int(parts[2])

    services = context.get("services", {})
    session_factory = services.get("db_session_factory")
    if not session_factory:
        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text="Service unavailable.",
            blocks=[],
        )
        return

    with session_factory() as session:
        from second_brain.models.entity import Entity, entry_entities
        from sqlalchemy import select

        # Check if link already exists
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
        entity = session.get(Entity, entity_id)
        entity_name = entity.name if entity else f"#{entity_id}"

    logger.info(
        "Entity disambiguation resolved: entry=%d linked to entity=%d",
        entry_id,
        entity_id,
    )
    await client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=f"Linked to: {entity_name}",
        blocks=[],
    )


async def entity_new_handler(ack, action, body, say, context, client):
    """Handle entity disambiguation -- user chose to create a new entity."""
    await ack()

    # action_id format: entity_new:{entry_id}:{name}:{type}
    # Use maxsplit=3 so colons inside the entity name are preserved
    parts = action["action_id"].split(":", 3)
    if len(parts) < 4:
        logger.warning("Invalid entity_new action_id: %s", action["action_id"])
        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text="Created as new entity.",
            blocks=[],
        )
        return

    entry_id = int(parts[1])
    # The type is the last segment; name is everything between entry_id and type.
    # Since type won't contain colons (person/company/project), split from right.
    name_and_type = parts[2] + ":" + parts[3] if len(parts) > 3 else parts[2]
    last_colon = name_and_type.rfind(":")
    if last_colon == -1:
        entity_name = name_and_type
        entity_type = "person"
    else:
        entity_name = name_and_type[:last_colon]
        entity_type = name_and_type[last_colon + 1:]

    services = context.get("services", {})
    session_factory = services.get("db_session_factory")
    if not session_factory:
        await client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text="Service unavailable.",
            blocks=[],
        )
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

    await client.chat_update(
        channel=body["channel"]["id"],
        ts=body["message"]["ts"],
        text=f"Created new entity: {entity_name}",
        blocks=[],
    )


async def nudge_reply_handler(event, say, context):
    """Handle natural language replies to nudge messages in threads.

    When a user replies in a thread whose parent is a nudge message,
    parse intent and route to the appropriate NudgeManager action.
    """
    # Only handle threaded replies
    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return

    # Filter out subtypes (bot messages, edits, etc.)
    if event.get("subtype") is not None:
        return

    services = context.get("services", {})
    nudge_manager = services.get("nudge_manager")
    if not nudge_manager:
        return

    session_factory = services.get("db_session_factory")
    if not session_factory:
        return

    # Look up the nudge by platform_message_id matching the thread parent
    from second_brain.models.nudge import NudgeHistory

    with session_factory() as session:
        nudge = (
            session.query(NudgeHistory)
            .filter(NudgeHistory.platform_message_id == thread_ts)
            .first()
        )
        if not nudge:
            return
        nudge_id = nudge.id

    user_text = event.get("text", "")
    if not user_text:
        return

    # Parse the natural language response
    action, snooze_date = nudge_manager.parse_natural_language_response(
        nudge_id, user_text
    )

    confirmation = nudge_manager.handle_nudge_action(
        nudge_id, action, snooze_until=snooze_date
    )
    await say(text=confirmation)


def register(app) -> None:
    """Register all action, view, and event handlers on the Slack Bolt AsyncApp."""

    # Nudge button actions
    app.action("nudge_done")(nudge_done_handler)
    app.action("nudge_snooze")(nudge_snooze_handler)
    app.action("nudge_drop")(nudge_drop_handler)

    # Snooze sub-menu actions
    app.action("snooze_tomorrow")(snooze_tomorrow_handler)
    app.action("snooze_week")(snooze_week_handler)
    app.action("snooze_custom")(snooze_custom_handler)

    # Custom snooze date modal submission
    app.view("snooze_date_modal")(snooze_date_modal_handler)

    # Daily summary actions
    app.action("summary_review_loops")(summary_review_loops_handler)
    app.action("summary_dismiss")(summary_dismiss_handler)

    # Entity disambiguation actions (regex pattern matching)
    app.action(re.compile(r"^entity_select:"))(entity_select_handler)
    app.action(re.compile(r"^entity_new:"))(entity_new_handler)

    # Nudge thread reply handler -- registered as a secondary message listener.
    # This works alongside the main message handler in message.py because
    # Bolt dispatches to all matching listeners.
    @app.event("message")
    async def _nudge_reply_listener(event, say, context):
        # Only process threaded replies here; the main message handler
        # filters these out because they have no subtype but do have thread_ts.
        if event.get("thread_ts"):
            await nudge_reply_handler(event, say, context)

    logger.info("Callback handlers registered")
