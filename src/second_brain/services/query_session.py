"""Query session manager — tracks active query session state."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from second_brain.utils.time import utc_now

logger = logging.getLogger(__name__)


@dataclass
class QuerySession:
    """State for an active query session."""

    query: str = ""
    response: str = ""
    source_entry_ids: list[int] = field(default_factory=list)
    last_activity: datetime = field(default_factory=lambda: utc_now())


class QuerySessionManager:
    """Manages query session state for the user.

    Tracks the most recent query, response, and source entries so that
    follow-up queries can include prior context.
    """

    def __init__(self) -> None:
        self._session: QuerySession | None = None

    @property
    def session(self) -> QuerySession | None:
        """Return the current session, or None if inactive/expired."""
        if self._session and self.is_active():
            return self._session
        return None

    def is_active(self, timeout_minutes: int = 10) -> bool:
        """Check if the session hasn't timed out.

        Args:
            timeout_minutes: Minutes of inactivity before session expires.

        Returns:
            True if session exists and is within the timeout window.
        """
        if self._session is None:
            return False

        elapsed = (utc_now() - self._session.last_activity).total_seconds()
        return elapsed < timeout_minutes * 60

    def update(self, query: str, response: str, source_entry_ids: list[int]) -> None:
        """Update session with a new query/response pair.

        Args:
            query: The query text.
            response: The response text.
            source_entry_ids: IDs of entries used in the response.
        """
        self._session = QuerySession(
            query=query,
            response=response,
            source_entry_ids=source_entry_ids,
            last_activity=utc_now(),
        )
        logger.debug("Query session updated: query=%s", query[:80])

    def reset(self) -> None:
        """Clear session, returning to capture mode."""
        self._session = None
        logger.debug("Query session reset")
