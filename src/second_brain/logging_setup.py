"""Structured logging setup with daily rotation."""

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def setup_logging(log_level: str | None = None, log_dir: str = "logs") -> None:
    """Configure structured logging to file with daily rotation.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR). Defaults to
            LOG_LEVEL env var or INFO.
        log_dir: Directory for log files. Created if it doesn't exist.
    """
    level = (log_level or os.environ.get("LOG_LEVEL", "INFO")).upper()

    Path(log_dir).mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = TimedRotatingFileHandler(
        filename=os.path.join(log_dir, "second_brain.log"),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("slack_bolt").setLevel(logging.WARNING)
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Logging initialized at %s level", level
    )
