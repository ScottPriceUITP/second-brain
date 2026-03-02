"""Slack Bolt async application with handler registry pattern.

The handler registry lists all handler module paths upfront. Each handler
module exposes a register(app) function. Missing modules are skipped
gracefully so agents can merge independently.
"""

import importlib
import logging

from slack_bolt.async_app import AsyncApp

logger = logging.getLogger(__name__)

# All handler module paths — listed upfront, missing ones skipped gracefully
HANDLER_MODULES = [
    "second_brain.bot.handlers.message",
    "second_brain.bot.handlers.commands",
    "second_brain.bot.handlers.callbacks",
]


def register_handlers(app: AsyncApp) -> list[str]:
    """Import and register all available handler modules.

    Each handler module must expose a register(app) function.
    Missing or broken modules are logged and skipped.

    Returns:
        List of successfully registered module names.
    """
    registered = []
    for module_path in HANDLER_MODULES:
        try:
            module = importlib.import_module(module_path)
            module.register(app)
            registered.append(module_path)
            logger.info("Registered handler: %s", module_path)
        except ImportError:
            logger.info("Handler not available (skipped): %s", module_path)
        except Exception:
            logger.exception("Failed to register handler: %s", module_path)

    return registered


def create_app(
    bot_token: str,
    services: dict,
) -> AsyncApp:
    """Build the Slack Bolt AsyncApp with all available handlers.

    Services are injected into every request's context via middleware,
    making them available to all handlers as context["services"].

    Args:
        bot_token: Slack bot OAuth token (xoxb-...).
        services: Dict of available services to inject into context.

    Returns:
        Configured AsyncApp ready for Socket Mode.
    """
    app = AsyncApp(token=bot_token)

    # Middleware: inject services dict into every request context
    async def inject_services(context, next):
        context["services"] = services
        await next()

    app.middleware(inject_services)

    # Register all available handlers
    registered = register_handlers(app)
    logger.info(
        "Slack app created with %d handler(s): %s",
        len(registered),
        registered,
    )

    return app
