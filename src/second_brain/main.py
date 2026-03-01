"""Entry point for Second Brain bot.

Loads config, creates DB engine, runs migrations, builds services,
creates bot application, and starts polling.
"""

import logging
import os
import subprocess
import sys

from second_brain.bot.app import create_application
from second_brain.config import ANTHROPIC_API_KEY, OPENAI_API_KEY, TELEGRAM_BOT_TOKEN
from second_brain.logging_setup import setup_logging
from second_brain.models.base import create_db_engine, create_session_factory

logger = logging.getLogger(__name__)


def run_migrations() -> None:
    """Run Alembic migrations (upgrade head)."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            check=True,
        )
        logger.info("Migrations completed: %s", result.stdout.strip())
    except subprocess.CalledProcessError as e:
        logger.error("Migration failed: %s", e.stderr)
        raise


def build_services(session_factory) -> dict:
    """Build the services dict, skipping unavailable services gracefully.

    Services are imported and instantiated individually. If a service's
    dependencies are not met (e.g., missing API key, unimplemented module),
    it is logged and skipped.

    Args:
        session_factory: SQLAlchemy sessionmaker for DB access.

    Returns:
        Dict of service name -> service instance.
    """
    services: dict = {
        "db_session_factory": session_factory,
    }

    # Anthropic client
    if ANTHROPIC_API_KEY:
        try:
            from second_brain.services.anthropic_client import AnthropicClient

            services["anthropic_client"] = AnthropicClient(api_key=ANTHROPIC_API_KEY)
            logger.info("Service loaded: anthropic_client")
        except Exception:
            logger.info("Service not available (skipped): anthropic_client")
    else:
        logger.info("Service skipped (no API key): anthropic_client")

    # Whisper / OpenAI client
    if OPENAI_API_KEY:
        try:
            from second_brain.services.whisper_client import WhisperClient

            services["whisper_client"] = WhisperClient(api_key=OPENAI_API_KEY)
            logger.info("Service loaded: whisper_client")
        except ImportError:
            logger.info("Service not available (skipped): whisper_client")
        except Exception:
            logger.exception("Service failed to load: whisper_client")
    else:
        logger.info("Service skipped (no API key): whisper_client")

    # Enrichment service
    try:
        from second_brain.services.enrichment import EnrichmentService

        if "anthropic_client" in services:
            services["enrichment"] = EnrichmentService(
                anthropic_client=services["anthropic_client"],
                session_factory=session_factory,
            )
            logger.info("Service loaded: enrichment")
        else:
            logger.info("Service skipped (no anthropic_client): enrichment")
    except ImportError:
        logger.info("Service not available (skipped): enrichment")
    except Exception:
        logger.exception("Service failed to load: enrichment")

    # Entity resolution
    try:
        from second_brain.services.entity_resolution import EntityResolutionService

        services["entity_resolution"] = EntityResolutionService(
            session_factory=session_factory,
        )
        logger.info("Service loaded: entity_resolution")
    except ImportError:
        logger.info("Service not available (skipped): entity_resolution")
    except Exception:
        logger.exception("Service failed to load: entity_resolution")

    # Connection scoring
    try:
        from second_brain.services.connection_scoring import ConnectionScoringService

        if "anthropic_client" in services:
            services["connection_scoring"] = ConnectionScoringService(
                anthropic_client=services["anthropic_client"],
                session_factory=session_factory,
            )
            logger.info("Service loaded: connection_scoring")
        else:
            logger.info("Service skipped (no anthropic_client): connection_scoring")
    except ImportError:
        logger.info("Service not available (skipped): connection_scoring")
    except Exception:
        logger.exception("Service failed to load: connection_scoring")

    # Query engine
    try:
        from second_brain.services.query_engine import QueryEngine

        if "anthropic_client" in services:
            services["query_engine"] = QueryEngine(
                anthropic_client=services["anthropic_client"],
                session_factory=session_factory,
            )
            logger.info("Service loaded: query_engine")
        else:
            logger.info("Service skipped (no anthropic_client): query_engine")
    except ImportError:
        logger.info("Service not available (skipped): query_engine")
    except Exception:
        logger.exception("Service failed to load: query_engine")

    # Nudge manager
    try:
        from second_brain.services.nudge_manager import NudgeManager

        if "anthropic_client" in services:
            services["nudge_manager"] = NudgeManager(
                session_factory=session_factory,
                anthropic_client=services["anthropic_client"],
            )
            logger.info("Service loaded: nudge_manager")
        else:
            logger.info("Service skipped (no anthropic_client): nudge_manager")
    except ImportError:
        logger.info("Service not available (skipped): nudge_manager")
    except Exception:
        logger.exception("Service failed to load: nudge_manager")

    # Scheduler
    try:
        from second_brain.services.scheduler import SchedulerService

        services["scheduler"] = SchedulerService(services=services)
        logger.info("Service loaded: scheduler")
    except ImportError:
        logger.info("Service not available (skipped): scheduler")
    except Exception:
        logger.exception("Service failed to load: scheduler")

    # Calendar sync
    try:
        from second_brain.services.calendar_sync import CalendarSyncService

        services["calendar_sync"] = CalendarSyncService(
            session_factory=session_factory,
        )
        logger.info("Service loaded: calendar_sync")
    except ImportError:
        logger.info("Service not available (skipped): calendar_sync")
    except Exception:
        logger.exception("Service failed to load: calendar_sync")

    return services


def main() -> None:
    """Main entry point: setup logging, DB, migrations, services, and start bot."""
    setup_logging()

    logger.info("Starting Second Brain bot...")

    # Validate required env var
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Exiting.")
        sys.exit(1)

    # Create DB engine and session factory
    engine = create_db_engine()
    session_factory = create_session_factory(engine)
    logger.info("Database engine created: %s", engine.url)

    # Run migrations
    run_migrations()

    # Seed config defaults (in case migration didn't cover new keys)
    from second_brain.config import seed_config_defaults

    with session_factory() as session:
        seed_config_defaults(session)

    # Build services
    services = build_services(session_factory)
    logger.info("Services built: %s", list(services.keys()))

    # Create and run bot
    application = create_application(
        token=TELEGRAM_BOT_TOKEN,
        services=services,
    )

    logger.info("Starting Telegram polling...")
    application.run_polling()


if __name__ == "__main__":
    main()
