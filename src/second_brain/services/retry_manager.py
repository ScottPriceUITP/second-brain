"""Retry manager — retries failed enrichments and transcriptions.

Runs as an APScheduler job (registered in scheduler.py) to periodically
retry entries stuck in pending_enrichment or pending_transcription status.
Tracks retry counts in memory and notifies the user on recovery or exhaustion.
"""

import logging
from datetime import date, datetime

from sqlalchemy.orm import sessionmaker

from second_brain.bot.formatting import format_error, format_recovery
from second_brain.config import get_config_int
from second_brain.models.entry import Entry
from second_brain.services.enrichment import EnrichmentService
from second_brain.services.whisper_client import WhisperClient
from second_brain.utils.tags import store_tags

logger = logging.getLogger(__name__)


class RetryManager:
    """Retries entries stuck in pending_enrichment or pending_transcription.

    Called periodically by the scheduler. Tracks retry counts in memory
    per entry ID and stops retrying after a configurable max retry count.
    Counts reset on process restart, which is acceptable because a restart
    likely means external APIs have recovered.
    """

    def __init__(
        self,
        session_factory: sessionmaker,
        enrichment_service: EnrichmentService | None = None,
        whisper_client: WhisperClient | None = None,
        bot: object | None = None,
        chat_id: int | str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.enrichment_service = enrichment_service
        self.whisper_client = whisper_client
        self._bot = bot
        self._chat_id = chat_id
        # In-memory retry count tracking: entry_id -> count
        self._enrichment_retries: dict[int, int] = {}
        self._transcription_retries: dict[int, int] = {}

    def set_bot(self, bot: object) -> None:
        """Set or update the Telegram bot instance for audio re-downloads."""
        self._bot = bot

    def set_chat_id(self, chat_id: int | str) -> None:
        """Set or update the Telegram chat ID for notifications."""
        self._chat_id = chat_id

    async def retry_pending(self) -> None:
        """Retry all pending enrichments and transcriptions.

        Called by the scheduler's retry job.
        """
        await self.retry_pending_enrichments()
        await self.retry_pending_transcriptions()

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

    async def retry_pending_transcriptions(self) -> None:
        """Find entries with status='pending_transcription' and retry.

        Uses stored audio_file_id to re-download from Telegram, then
        sends to WhisperClient.transcribe().  On success, feeds directly
        into the enrichment pipeline.  On failure, increments retry count
        and notifies user on exhaustion.
        """
        if not self.whisper_client:
            logger.debug("Transcription retry skipped: no whisper client")
            return

        if not self._bot:
            logger.debug("Transcription retry skipped: no bot for audio download")
            return

        with self.session_factory() as session:
            max_retries = get_config_int(session, "transcription_retry_count") or 3

            pending = (
                session.query(Entry)
                .filter(Entry.status == "pending_transcription")
                .all()
            )

            if not pending:
                return

            logger.info("Retrying transcription for %d entries", len(pending))

            for entry in pending:
                if not entry.audio_file_id:
                    logger.warning(
                        "No audio_file_id for entry %d, cannot retry transcription",
                        entry.id,
                    )
                    continue

                count = self._transcription_retries.get(entry.id, 0)

                if count >= max_retries:
                    entry.status = "open"
                    self._transcription_retries.pop(entry.id, None)
                    session.commit()

                    time_str = _format_time(entry.created_at)
                    await self._notify(
                        format_error(
                            f"I still can't transcribe your voice note from "
                            f"{time_str}. It's stored as-is."
                        )
                    )
                    logger.warning(
                        "Max transcription retries (%d) exhausted for entry %d",
                        max_retries,
                        entry.id,
                    )
                    continue

                self._transcription_retries[entry.id] = count + 1

                # Step 1: Re-download audio from Telegram
                try:
                    file = await self._bot.get_file(entry.audio_file_id)
                    audio_bytes = bytes(await file.download_as_bytearray())
                except Exception:
                    logger.warning(
                        "Audio download retry %d/%d failed for entry %d",
                        count + 1,
                        max_retries,
                        entry.id,
                    )
                    continue

                # Step 2: Transcribe
                try:
                    transcription = self.whisper_client.transcribe(
                        audio_file=audio_bytes,
                        filename=f"retry_{entry.id}.ogg",
                    )
                except Exception:
                    logger.warning(
                        "Transcription retry %d/%d failed for entry %d",
                        count + 1,
                        max_retries,
                        entry.id,
                    )
                    continue

                # Step 3: Store raw transcription
                entry.raw_text = transcription.text

                # Step 4: Feed into enrichment pipeline
                if self.enrichment_service:
                    try:
                        result = self.enrichment_service.enrich_text(
                            raw_text=transcription.text,
                        )
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
                    except Exception:
                        # Transcription succeeded but enrichment failed —
                        # hand off to enrichment retry
                        logger.warning(
                            "Enrichment after transcription retry failed for entry %d, "
                            "queuing for enrichment retry",
                            entry.id,
                        )
                        entry.status = "pending_enrichment"
                        session.commit()
                        self._transcription_retries.pop(entry.id, None)
                        continue
                else:
                    # No enrichment service — queue for enrichment retry
                    entry.status = "pending_enrichment"
                    session.commit()

                self._transcription_retries.pop(entry.id, None)

                time_str = _format_time(entry.created_at)
                await self._notify(
                    format_recovery(
                        f"Your voice note from {time_str} has been fully processed."
                    )
                )
                logger.info(
                    "Transcription retry succeeded for entry %d on attempt %d",
                    entry.id,
                    count + 1,
                )

    async def _notify(self, text: str) -> None:
        """Send a notification to the user via Telegram bot."""
        if not self._bot or not self._chat_id:
            logger.info("Retry notification (no bot/chat_id): %s", text)
            return

        try:
            await self._bot.send_message(chat_id=self._chat_id, text=text)
        except Exception:
            logger.exception("Failed to send retry notification")


def _format_time(dt: datetime) -> str:
    """Format a datetime for user-facing messages (e.g. '2:15 PM')."""
    return dt.strftime("%-I:%M %p")


