"""Retry manager — retries failed enrichments.

Runs as an APScheduler job (registered in scheduler.py) to periodically
retry entries stuck in pending_enrichment status.
Tracks retry counts in memory and notifies the user on recovery or exhaustion.
"""

import logging
from datetime import date, datetime

from sqlalchemy.orm import sessionmaker

from second_brain.bot.formatting import format_error, format_recovery
from second_brain.config import get_config_int
from second_brain.models.entry import Entry
from second_brain.services.enrichment import EnrichmentService
from second_brain.utils.tags import store_tags

logger = logging.getLogger(__name__)


class RetryManager:
    """Retries entries stuck in pending_enrichment.

    Called periodically by the scheduler. Tracks retry counts in memory
    per entry ID and stops retrying after a configurable max retry count.
    Counts reset on process restart, which is acceptable because a restart
    likely means external APIs have recovered.
    """

    def __init__(
        self,
        session_factory: sessionmaker,
        enrichment_service: EnrichmentService | None = None,
        client: object | None = None,
        channel_id: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.enrichment_service = enrichment_service
        self._client = client
        self._channel_id = channel_id
        # In-memory retry count tracking: entry_id -> count
        self._enrichment_retries: dict[int, int] = {}

    def set_client(self, client: object) -> None:
        """Set or update the Slack client instance for notifications."""
        self._client = client

    def set_channel_id(self, channel_id: str) -> None:
        """Set or update the Slack channel ID for notifications."""
        self._channel_id = channel_id

    async def retry_pending(self) -> None:
        """Retry all pending enrichments.

        Called by the scheduler's retry job.
        """
        await self.retry_pending_enrichments()

    async def retry_pending_enrichments(self) -> None:
        """Find entries with status='pending_enrichment' and retry enrichment.

        On success: update entry with enrichment results, set status='open',
        notify user via format_recovery().
        On failure: increment retry count.  If max retries exhausted,
        set status='open', notify user via format_error().
        """
        if not self.enrichment_service:
            logger.debug("Enrichment retry skipped: no enrichment service")
            return

        with self.session_factory() as session:
            max_retries = get_config_int(session, "enrichment_retry_count") or 3

            pending = (
                session.query(Entry)
                .filter(Entry.status == "pending_enrichment")
                .all()
            )

            if not pending:
                return

            logger.info("Retrying enrichment for %d entries", len(pending))

            for entry in pending:
                count = self._enrichment_retries.get(entry.id, 0)

                if count >= max_retries:
                    # Max retries exhausted — mark open and notify
                    entry.status = "open"
                    self._enrichment_retries.pop(entry.id, None)
                    session.commit()

                    time_str = _format_time(entry.created_at)
                    await self._notify(
                        format_error(
                            f"I still can't enrich your note from {time_str}. "
                            "It's stored as-is."
                        )
                    )
                    logger.warning(
                        "Max enrichment retries (%d) exhausted for entry %d",
                        max_retries,
                        entry.id,
                    )
                    continue

                self._enrichment_retries[entry.id] = count + 1

                try:
                    result = self.enrichment_service.enrich_text(
                        raw_text=entry.raw_text,
                    )
                except Exception:
                    logger.warning(
                        "Enrichment retry %d/%d failed for entry %d",
                        count + 1,
                        max_retries,
                        entry.id,
                    )
                    continue

                # Success — update entry with enrichment results
                entry.clean_text = result.clean_text
                entry.entry_type = result.entry_type
                entry.is_open_loop = result.is_open_loop
                entry.status = "open"

                if result.follow_up_date:
                    try:
                        entry.follow_up_date = date.fromisoformat(
                            result.follow_up_date
                        )
                    except ValueError:
                        pass

                if result.calendar_event_id:
                    entry.calendar_event_id = result.calendar_event_id

                store_tags(session, entry, result.tags)

                session.commit()
                self._enrichment_retries.pop(entry.id, None)

                time_str = _format_time(entry.created_at)
                await self._notify(
                    format_recovery(
                        f"Your note from {time_str} has been fully processed."
                    )
                )
                logger.info(
                    "Enrichment retry succeeded for entry %d on attempt %d",
                    entry.id,
                    count + 1,
                )

    async def _notify(self, text: str) -> None:
        """Send a notification to the user via Slack."""
        if not self._client or not self._channel_id:
            logger.info("Retry notification (no client/channel_id): %s", text)
            return

        try:
            await self._client.chat_postMessage(channel=self._channel_id, text=text)
        except Exception:
            logger.exception("Failed to send retry notification")


def _format_time(dt: datetime) -> str:
    """Format a datetime for user-facing messages (e.g. '2:15 PM')."""
    return dt.strftime("%-I:%M %p")
