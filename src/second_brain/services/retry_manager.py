"""Retry manager — retries failed enrichments and transcriptions on a schedule."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker

from second_brain.bot.formatting import format_error, format_recovery
from second_brain.config import get_config_int

logger = logging.getLogger(__name__)


class RetryManager:
    """Retries entries stuck in pending_enrichment or pending_transcription.

    Called periodically by the scheduler. Tracks retry counts in memory
    per entry ID and stops retrying after a configurable max retry count.
    """

    def __init__(
        self,
        session_factory: sessionmaker,
        enrichment_service=None,
        whisper_client=None,
    ) -> None:
        self.session_factory = session_factory
        self.enrichment_service = enrichment_service
        self.whisper_client = whisper_client
        # In-memory retry count tracking: entry_id -> count
        self._enrichment_retries: dict[int, int] = {}
        self._transcription_retries: dict[int, int] = {}

    def _get_max_enrichment_retries(self) -> int:
        with self.session_factory() as session:
            return get_config_int(session, "enrichment_retry_count") or 3

    def _get_max_transcription_retries(self) -> int:
        with self.session_factory() as session:
            return get_config_int(session, "transcription_retry_count") or 3

    async def retry_pending(self) -> None:
        """Retry all pending enrichments and transcriptions.

        Called by the scheduler's retry job.
        """
        await self.retry_pending_enrichments()
        await self.retry_pending_transcriptions()

    async def retry_pending_enrichments(self) -> None:
        """Find entries with status='pending_enrichment' and retry enrichment."""
        if not self.enrichment_service:
            logger.debug("Enrichment retry skipped: no enrichment service")
            return

        from second_brain.models.entry import Entry

        max_retries = self._get_max_enrichment_retries()

        with self.session_factory() as session:
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
                    logger.warning(
                        "Max enrichment retries (%d) exhausted for entry %d",
                        max_retries,
                        entry.id,
                    )
                    entry.status = "open"
                    self._enrichment_retries.pop(entry.id, None)
                    session.commit()

                    # Queue notification for exhaustion
                    created_str = entry.created_at.strftime("%Y-%m-%d %H:%M")
                    await self._notify(
                        format_error(
                            f"I still can't enrich your note from {created_str}. "
                            "It's stored as-is."
                        )
                    )
                    continue

                self._enrichment_retries[entry.id] = count + 1

                try:
                    result = self.enrichment_service.enrich_text(
                        raw_text=entry.raw_text,
                    )

                    entry.clean_text = result.clean_text
                    entry.entry_type = result.entry_type
                    entry.is_open_loop = result.is_open_loop
                    entry.status = "open"

                    if result.follow_up_date:
                        try:
                            from datetime import date

                            entry.follow_up_date = date.fromisoformat(
                                result.follow_up_date
                            )
                        except ValueError:
                            pass

                    if result.calendar_event_id:
                        entry.calendar_event_id = result.calendar_event_id

                    # Store tags
                    _store_tags(session, entry, result.tags)

                    session.commit()

                    # Clean up retry tracking
                    self._enrichment_retries.pop(entry.id, None)

                    created_str = entry.created_at.strftime("%Y-%m-%d %H:%M")
                    await self._notify(
                        format_recovery(
                            f"Your note from {created_str} has been fully processed."
                        )
                    )

                    logger.info(
                        "Enrichment retry succeeded for entry %d on attempt %d",
                        entry.id,
                        count + 1,
                    )

                except Exception:
                    logger.warning(
                        "Enrichment retry %d/%d failed for entry %d",
                        count + 1,
                        max_retries,
                        entry.id,
                    )

    async def retry_pending_transcriptions(self) -> None:
        """Find entries with status='pending_transcription' and retry transcription."""
        if not self.whisper_client:
            logger.debug("Transcription retry skipped: no whisper client")
            return

        from second_brain.models.entry import Entry

        max_retries = self._get_max_transcription_retries()

        with self.session_factory() as session:
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
                    logger.warning(
                        "Max transcription retries (%d) exhausted for entry %d",
                        max_retries,
                        entry.id,
                    )
                    entry.status = "open"
                    self._transcription_retries.pop(entry.id, None)
                    session.commit()

                    created_str = entry.created_at.strftime("%Y-%m-%d %H:%M")
                    await self._notify(
                        format_error(
                            f"I still can't transcribe your voice note from "
                            f"{created_str}. Audio is saved for manual review."
                        )
                    )
                    continue

                self._transcription_retries[entry.id] = count + 1

                try:
                    # Download audio via stored file_id
                    audio_bytes = await self._download_audio(entry.audio_file_id)
                    if audio_bytes is None:
                        logger.warning(
                            "Could not re-download audio for entry %d",
                            entry.id,
                        )
                        continue

                    result = self.whisper_client.transcribe(
                        audio_file=audio_bytes,
                        filename=f"retry_{entry.id}.ogg",
                    )

                    entry.raw_text = result.text
                    entry.status = "pending_enrichment"
                    session.commit()

                    # Clean up retry tracking
                    self._transcription_retries.pop(entry.id, None)

                    logger.info(
                        "Transcription retry succeeded for entry %d on attempt %d",
                        entry.id,
                        count + 1,
                    )

                    # Enrichment will be picked up by the next enrichment retry cycle

                except Exception:
                    logger.warning(
                        "Transcription retry %d/%d failed for entry %d",
                        count + 1,
                        max_retries,
                        entry.id,
                    )

    async def _download_audio(self, file_id: str) -> bytes | None:
        """Download audio from Telegram using stored file_id.

        Requires the bot instance to be available via bot_data.
        """
        if not hasattr(self, "_bot") or self._bot is None:
            logger.debug("Cannot download audio: no bot instance available")
            return None

        try:
            file = await self._bot.get_file(file_id)
            data = await file.download_as_bytearray()
            return bytes(data)
        except Exception:
            logger.exception("Failed to download audio file %s", file_id)
            return None

    def set_bot(self, bot) -> None:
        """Set the Telegram bot instance for audio re-downloads.

        Args:
            bot: The telegram.Bot instance.
        """
        self._bot = bot

    async def _notify(self, text: str) -> None:
        """Send a notification to the user via Telegram.

        Requires bot and chat_id to be set.
        """
        if not hasattr(self, "_bot") or self._bot is None:
            logger.info("Retry notification (no bot): %s", text)
            return

        chat_id = getattr(self, "_chat_id", None)
        if not chat_id:
            logger.info("Retry notification (no chat_id): %s", text)
            return

        try:
            await self._bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.exception("Failed to send retry notification")

    def set_chat_id(self, chat_id: int) -> None:
        """Set the chat ID for notifications.

        Args:
            chat_id: Telegram chat ID to send notifications to.
        """
        self._chat_id = chat_id


def _store_tags(session, entry, tag_names: list[str]) -> None:
    """Create or get-existing tags and link them to the entry."""
    if not tag_names:
        return

    from second_brain.models.tag import Tag

    for tag_name in tag_names:
        tag_name = tag_name.strip().lower()
        if not tag_name:
            continue

        tag = session.query(Tag).filter(Tag.name == tag_name).first()
        if not tag:
            tag = Tag(name=tag_name)
            session.add(tag)
            session.flush()

        if tag not in entry.tags:
            entry.tags.append(tag)
