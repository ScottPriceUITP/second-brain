"""Telegram bot application with handler registry pattern.

The handler registry lists all handler module paths upfront. Each handler
module exposes a register(application) function. Missing modules are
skipped gracefully so agents can merge independently.
"""

import importlib
import logging

from telegram.ext import Application, MessageHandler, filters

logger = logging.getLogger(__name__)

# All handler module paths — listed upfront, missing ones skipped gracefully
HANDLER_MODULES = [
    "second_brain.bot.handlers.message",
    "second_brain.bot.handlers.voice",
    "second_brain.bot.handlers.commands",
    "second_brain.bot.handlers.callbacks",
]


async def _echo_handler(update, context):
    """Placeholder echo handler until real handlers are registered."""
    if update.message and update.message.text:
        await update.message.reply_text(f"Echo: {update.message.text}")


def register_handlers(application: Application) -> list[str]:
    """Import and register all available handler modules.

    Each handler module must expose a register(application) function.
    Missing or broken modules are logged and skipped.

    Returns:
        List of successfully registered module names.
    """
    registered = []
    for module_path in HANDLER_MODULES:
        try:
            module = importlib.import_module(module_path)
            module.register(application)
            registered.append(module_path)
            logger.info("Registered handler: %s", module_path)
        except ImportError:
            logger.info("Handler not available (skipped): %s", module_path)
        except Exception:
            logger.exception("Failed to register handler: %s", module_path)

    if not registered:
        logger.info("No handlers registered — installing echo fallback")
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, _echo_handler)
        )

    return registered


def create_application(
    token: str,
    services: dict,
) -> Application:
    """Build the Telegram bot Application with all available handlers.

    Args:
        token: Telegram bot token.
        services: Dict of available services to inject into bot_data.

    Returns:
        Configured Application ready for polling.
    """
    application = Application.builder().token(token).build()

    # Inject services into bot_data as a service container
    application.bot_data.update(services)

    # Register all available handlers
    registered = register_handlers(application)
    logger.info(
        "Bot application created with %d handler(s): %s",
        len(registered),
        registered,
    )

    return application
