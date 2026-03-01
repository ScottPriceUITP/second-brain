"""Integration tests for error handling and retry flows.

Tests: API failure -> entry stored as pending -> retry -> success notification.
Covers both enrichment and transcription failure paths.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from second_brain.bot.handlers.message import handle_text_message
from second_brain.bot.handlers.voice import handle_voice_message
from second_brain.models.entry import Entry
from second_brain.prompts.enrichment import EnrichmentResult, ExtractedEntity
from second_brain.services.retry_manager import RetryManager
from second_brain.services.whisper_client import TranscriptionResult
from second_brain.utils.time import utc_now


@pytest.fixture
def enrichment_result():
    """Successful enrichment result for retry tests."""
    return EnrichmentResult(
        intent="capture",
        clean_text="Updated meeting notes with Reynolds Electric about supply chain.",
        entry_type="meeting_note",
        entities=[ExtractedEntity(name="Reynolds Electric", type="company")],
        is_open_loop=False,
        follow_up_date=None,
        tags=["reynolds", "supply-chain"],
        calendar_event_id=None,
    )


@pytest.fixture
def transcription_result():
    """Successful transcription result for retry tests."""
    return TranscriptionResult(
        text="Meeting notes about the supply chain",
        confidence=0.95,
        language="en",
    )


class TestEnrichmentErrorHandling:
    """Tests for enrichment failure and retry paths."""

    @pytest.mark.asyncio
    async def test_enrichment_failure_stores_pending_entry(
        self,
        session_factory,
        session,
    ):
        """Test that enrichment failure leaves entry as pending_enrichment."""
        failing_enrichment = MagicMock()
        failing_enrichment.enrich_text = MagicMock(side_effect=Exception("Anthropic API rate limit"))

        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "Test message for retry"
        update.message.message_id = 300
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot_data = {
            "db_session_factory": session_factory,
            "enrichment": failing_enrichment,
        }
        context.user_data = {}

        await handle_text_message(update, context)

        with session_factory() as s:
            entries = s.query(Entry).all()
            assert len(entries) == 1
            assert entries[0].status == "pending_enrichment"
            assert entries[0].raw_text == "Test message for retry"

        # User gets error notification
        update.message.reply_text.assert_called()
        reply_text = update.message.reply_text.call_args[0][0]
        assert "WARNING" in reply_text
        assert "saved" in reply_text.lower() or "retried" in reply_text.lower()

    @pytest.mark.asyncio
    async def test_enrichment_retry_succeeds(
        self,
        session_factory,
        session,
        enrichment_result,
    ):
        """Test that retry manager successfully retries a pending enrichment."""
        # Create a pending_enrichment entry
        with session_factory() as s:
            entry = Entry(
                raw_text="Test message for retry",
                source="telegram_text",
                status="pending_enrichment",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            s.add(entry)
            s.commit()
            entry_id = entry.id

        # Set up enrichment service that succeeds
        mock_enrichment = MagicMock()
        mock_enrichment.enrich_text = MagicMock(return_value=enrichment_result)

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        retry_manager = RetryManager(
            session_factory=session_factory,
            enrichment_service=mock_enrichment,
            bot=mock_bot,
            chat_id=12345,
        )

        await retry_manager.retry_pending_enrichments()

        # Entry should now be enriched and open
        with session_factory() as s:
            entry = s.get(Entry, entry_id)
            assert entry.status == "open"
            assert entry.clean_text == enrichment_result.clean_text
            assert entry.entry_type == "meeting_note"

        # Recovery notification should be sent
        mock_bot.send_message.assert_called()
        call_kwargs = mock_bot.send_message.call_args[1]
        assert "RECOVERED" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_enrichment_retry_failure_increments_count(
        self,
        session_factory,
        session,
    ):
        """Test that failed enrichment retry increments count and keeps status."""
        with session_factory() as s:
            entry = Entry(
                raw_text="Failing entry",
                source="telegram_text",
                status="pending_enrichment",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            s.add(entry)
            s.commit()
            entry_id = entry.id

        mock_enrichment = MagicMock()
        mock_enrichment.enrich_text = MagicMock(side_effect=RuntimeError("API error"))

        retry_manager = RetryManager(
            session_factory=session_factory,
            enrichment_service=mock_enrichment,
        )

        await retry_manager.retry_pending_enrichments()

        # Entry should still be pending
        with session_factory() as s:
            entry = s.get(Entry, entry_id)
            assert entry.status == "pending_enrichment"

        # Retry count should have been tracked
        assert retry_manager._enrichment_retries[entry_id] == 1

    @pytest.mark.asyncio
    async def test_enrichment_retry_exhaustion(
        self,
        session_factory,
        session,
    ):
        """Test that max retries exhaustion marks entry as open and notifies."""
        # Create a pending_enrichment entry
        with session_factory() as s:
            entry = Entry(
                raw_text="Test message that keeps failing",
                source="telegram_text",
                status="pending_enrichment",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            s.add(entry)
            s.commit()
            entry_id = entry.id

        # Set up enrichment service that always fails
        failing_enrichment = MagicMock()
        failing_enrichment.enrich_text = MagicMock(side_effect=Exception("Persistent API failure"))

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        retry_manager = RetryManager(
            session_factory=session_factory,
            enrichment_service=failing_enrichment,
            bot=mock_bot,
            chat_id=12345,
        )

        # Simulate having already used all retries
        retry_manager._enrichment_retries[entry_id] = 3

        await retry_manager.retry_pending_enrichments()

        # Entry should be marked as open (fallback)
        with session_factory() as s:
            entry = s.get(Entry, entry_id)
            assert entry.status == "open"
            # clean_text should still be None (enrichment never succeeded)
            assert entry.clean_text is None

        # Error notification should have been sent
        mock_bot.send_message.assert_called()
        last_call = mock_bot.send_message.call_args[1]
        assert "WARNING" in last_call["text"]

        # Retry tracking should be cleaned up
        assert entry_id not in retry_manager._enrichment_retries

    @pytest.mark.asyncio
    async def test_enrichment_retry_with_no_pending_entries(
        self,
        session_factory,
        session,
    ):
        """Test that retry manager handles no pending entries gracefully."""
        mock_enrichment = MagicMock()
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        retry_manager = RetryManager(
            session_factory=session_factory,
            enrichment_service=mock_enrichment,
            bot=mock_bot,
            chat_id=12345,
        )

        await retry_manager.retry_pending_enrichments()

        # Enrichment should not have been called
        mock_enrichment.enrich_text.assert_not_called()
        mock_bot.send_message.assert_not_called()


class TestTranscriptionErrorHandling:
    """Tests for transcription failure and retry paths."""

    @pytest.mark.asyncio
    async def test_transcription_failure_stores_pending_entry(
        self,
        session_factory,
        session,
    ):
        """Test that transcription failure leaves entry as pending_transcription."""
        failing_whisper = MagicMock()
        failing_whisper.transcribe = MagicMock(side_effect=Exception("Whisper API timeout"))

        mock_telegram_file = MagicMock()
        mock_telegram_file.download_as_bytearray = AsyncMock(
            return_value=bytearray(b"fake audio")
        )

        update = MagicMock()
        update.message = MagicMock()
        update.message.text = None
        update.message.message_id = 301
        update.message.reply_text = AsyncMock()
        voice = MagicMock()
        voice.file_id = "retry_test_file_id"
        voice.file_unique_id = "retry_test_unique_id"
        update.message.voice = voice

        context = MagicMock()
        context.bot_data = {
            "db_session_factory": session_factory,
            "whisper_client": failing_whisper,
        }
        context.user_data = {}
        context.bot = MagicMock()
        context.bot.get_file = AsyncMock(return_value=mock_telegram_file)

        await handle_voice_message(update, context)

        with session_factory() as s:
            entries = s.query(Entry).all()
            assert len(entries) == 1
            assert entries[0].status == "pending_transcription"
            assert entries[0].audio_file_id == "retry_test_file_id"

    @pytest.mark.asyncio
    async def test_transcription_retry_succeeds(
        self,
        session_factory,
        session,
        transcription_result,
        enrichment_result,
    ):
        """Test that retry manager retries transcription and feeds into enrichment."""
        # Create a pending_transcription entry
        with session_factory() as s:
            entry = Entry(
                raw_text="",
                source="telegram_voice",
                status="pending_transcription",
                audio_file_id="retry_audio_file_id",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            s.add(entry)
            s.commit()
            entry_id = entry.id

        # Mock successful whisper + enrichment
        mock_whisper = MagicMock()
        mock_whisper.transcribe = MagicMock(return_value=transcription_result)

        mock_enrichment = MagicMock()
        mock_enrichment.enrich_text = MagicMock(return_value=enrichment_result)

        # Mock bot for audio download
        mock_file = MagicMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"audio data"))
        mock_bot = MagicMock()
        mock_bot.get_file = AsyncMock(return_value=mock_file)
        mock_bot.send_message = AsyncMock()

        retry_manager = RetryManager(
            session_factory=session_factory,
            enrichment_service=mock_enrichment,
            whisper_client=mock_whisper,
            bot=mock_bot,
            chat_id=12345,
        )

        await retry_manager.retry_pending_transcriptions()

        # Entry should be fully processed
        with session_factory() as s:
            entry = s.get(Entry, entry_id)
            assert entry.status == "open"
            assert entry.raw_text == transcription_result.text
            assert entry.clean_text == enrichment_result.clean_text
            assert entry.entry_type == "meeting_note"

        # Recovery notification should be sent
        mock_bot.send_message.assert_called()
        call_kwargs = mock_bot.send_message.call_args[1]
        assert "RECOVERED" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_transcription_retry_exhaustion(
        self,
        session_factory,
        session,
    ):
        """Test that max transcription retries marks entry as open."""
        # Create a pending_transcription entry
        with session_factory() as s:
            entry = Entry(
                raw_text="",
                source="telegram_voice",
                status="pending_transcription",
                audio_file_id="failing_audio_file_id",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            s.add(entry)
            s.commit()
            entry_id = entry.id

        # Mock failing download
        mock_bot = MagicMock()
        mock_bot.get_file = AsyncMock(side_effect=Exception("Download failed"))
        mock_bot.send_message = AsyncMock()

        mock_whisper = MagicMock()

        retry_manager = RetryManager(
            session_factory=session_factory,
            whisper_client=mock_whisper,
            bot=mock_bot,
            chat_id=12345,
        )

        # Pre-set retry count to max
        retry_manager._transcription_retries[entry_id] = 3

        await retry_manager.retry_pending_transcriptions()

        # Entry should be marked as open (fallback)
        with session_factory() as s:
            entry = s.get(Entry, entry_id)
            assert entry.status == "open"

        # Error notification should have been sent
        mock_bot.send_message.assert_called()
        last_call = mock_bot.send_message.call_args[1]
        assert "WARNING" in last_call["text"]

    @pytest.mark.asyncio
    async def test_transcription_succeeds_but_enrichment_fails_queues_for_enrichment_retry(
        self,
        session_factory,
        session,
        transcription_result,
    ):
        """Test that successful transcription but failed enrichment queues for enrichment retry."""
        # Create a pending_transcription entry
        with session_factory() as s:
            entry = Entry(
                raw_text="",
                source="telegram_voice",
                status="pending_transcription",
                audio_file_id="partial_success_file_id",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            s.add(entry)
            s.commit()
            entry_id = entry.id

        # Mock successful whisper but failing enrichment
        mock_whisper = MagicMock()
        mock_whisper.transcribe = MagicMock(return_value=transcription_result)

        failing_enrichment = MagicMock()
        failing_enrichment.enrich_text = MagicMock(side_effect=Exception("Enrichment API down"))

        mock_file = MagicMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"audio data"))
        mock_bot = MagicMock()
        mock_bot.get_file = AsyncMock(return_value=mock_file)
        mock_bot.send_message = AsyncMock()

        retry_manager = RetryManager(
            session_factory=session_factory,
            enrichment_service=failing_enrichment,
            whisper_client=mock_whisper,
            bot=mock_bot,
            chat_id=12345,
        )

        await retry_manager.retry_pending_transcriptions()

        # Entry should have transcription stored but status should be pending_enrichment
        with session_factory() as s:
            entry = s.get(Entry, entry_id)
            assert entry.raw_text == transcription_result.text
            assert entry.status == "pending_enrichment"

    @pytest.mark.asyncio
    async def test_retry_without_services_is_noop(
        self,
        session_factory,
        session,
    ):
        """Test that retry manager without services does nothing."""
        retry_manager = RetryManager(
            session_factory=session_factory,
            enrichment_service=None,
            whisper_client=None,
            bot=None,
            chat_id=None,
        )

        # Should not raise
        await retry_manager.retry_pending()

    @pytest.mark.asyncio
    async def test_retry_no_bot_for_transcription_is_noop(
        self,
        session_factory,
        session,
    ):
        """Test that transcription retry without bot does nothing."""
        # Create a pending_transcription entry
        with session_factory() as s:
            entry = Entry(
                raw_text="",
                source="telegram_voice",
                status="pending_transcription",
                audio_file_id="no_bot_file_id",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            s.add(entry)
            s.commit()
            entry_id = entry.id

        mock_whisper = MagicMock()

        retry_manager = RetryManager(
            session_factory=session_factory,
            whisper_client=mock_whisper,
            bot=None,  # No bot available
            chat_id=12345,
        )

        await retry_manager.retry_pending_transcriptions()

        # Entry should still be pending
        with session_factory() as s:
            entry = s.get(Entry, entry_id)
            assert entry.status == "pending_transcription"

    @pytest.mark.asyncio
    async def test_retry_no_audio_file_id_skips(
        self,
        session_factory,
        session,
    ):
        """Test that entry without audio_file_id is skipped during transcription retry."""
        with session_factory() as s:
            entry = Entry(
                raw_text="",
                source="telegram_voice",
                status="pending_transcription",
                audio_file_id=None,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            s.add(entry)
            s.commit()

        mock_whisper = MagicMock()
        mock_bot = MagicMock()
        mock_bot.get_file = AsyncMock()

        retry_manager = RetryManager(
            session_factory=session_factory,
            whisper_client=mock_whisper,
            bot=mock_bot,
            chat_id=12345,
        )

        await retry_manager.retry_pending_transcriptions()

        mock_whisper.transcribe.assert_not_called()


class TestEndToEndErrorRecovery:
    """End-to-end tests for error -> retry -> recovery flow."""

    @pytest.mark.asyncio
    async def test_text_message_failure_then_retry_recovery(
        self,
        session_factory,
        session,
        enrichment_result,
    ):
        """Test full flow: message fails enrichment -> retry succeeds -> user notified."""
        # Step 1: Send a text message that fails enrichment
        failing_enrichment = MagicMock()
        failing_enrichment.enrich_text = MagicMock(side_effect=Exception("Temporary API error"))

        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "Important note about Reynolds Electric"
        update.message.message_id = 400
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot_data = {
            "db_session_factory": session_factory,
            "enrichment": failing_enrichment,
        }
        context.user_data = {}

        await handle_text_message(update, context)

        # Entry should be stored as pending_enrichment
        with session_factory() as s:
            entries = s.query(Entry).all()
            assert len(entries) == 1
            assert entries[0].status == "pending_enrichment"
            entry_id = entries[0].id

        # Step 2: Create retry manager with working enrichment
        success_enrichment = MagicMock()
        success_enrichment.enrich_text = MagicMock(return_value=enrichment_result)

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        retry_manager = RetryManager(
            session_factory=session_factory,
            enrichment_service=success_enrichment,
            bot=mock_bot,
            chat_id=12345,
        )

        await retry_manager.retry_pending_enrichments()

        # Step 3: Verify entry is now enriched
        with session_factory() as s:
            entry = s.get(Entry, entry_id)
            assert entry.status == "open"
            assert entry.clean_text == enrichment_result.clean_text
            assert entry.entry_type == "meeting_note"

        # Step 4: Verify recovery notification was sent
        mock_bot.send_message.assert_called()
        call_kwargs = mock_bot.send_message.call_args[1]
        assert "RECOVERED" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_voice_failure_then_retry_full_recovery(
        self,
        session_factory,
        session,
        transcription_result,
        enrichment_result,
    ):
        """Test full flow: voice transcription fails -> retry succeeds with enrichment."""
        # Step 1: Send a voice message that fails transcription
        failing_whisper = MagicMock()
        failing_whisper.transcribe = MagicMock(side_effect=Exception("Whisper API down"))

        mock_telegram_file = MagicMock()
        mock_telegram_file.download_as_bytearray = AsyncMock(
            return_value=bytearray(b"audio data")
        )

        update = MagicMock()
        update.message = MagicMock()
        update.message.text = None
        update.message.message_id = 401
        update.message.reply_text = AsyncMock()
        voice = MagicMock()
        voice.file_id = "recovery_test_file_id"
        voice.file_unique_id = "recovery_test_unique_id"
        update.message.voice = voice

        context = MagicMock()
        context.bot_data = {
            "db_session_factory": session_factory,
            "whisper_client": failing_whisper,
        }
        context.user_data = {}
        context.bot = MagicMock()
        context.bot.get_file = AsyncMock(return_value=mock_telegram_file)

        await handle_voice_message(update, context)

        with session_factory() as s:
            entries = s.query(Entry).all()
            assert len(entries) == 1
            assert entries[0].status == "pending_transcription"
            entry_id = entries[0].id

        # Step 2: Retry with working services
        success_whisper = MagicMock()
        success_whisper.transcribe = MagicMock(return_value=transcription_result)

        success_enrichment = MagicMock()
        success_enrichment.enrich_text = MagicMock(return_value=enrichment_result)

        mock_file = MagicMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"audio"))
        mock_bot = MagicMock()
        mock_bot.get_file = AsyncMock(return_value=mock_file)
        mock_bot.send_message = AsyncMock()

        retry_manager = RetryManager(
            session_factory=session_factory,
            enrichment_service=success_enrichment,
            whisper_client=success_whisper,
            bot=mock_bot,
            chat_id=12345,
        )

        await retry_manager.retry_pending_transcriptions()

        # Step 3: Verify full recovery
        with session_factory() as s:
            entry = s.get(Entry, entry_id)
            assert entry.status == "open"
            assert entry.raw_text == transcription_result.text
            assert entry.clean_text == enrichment_result.clean_text
            assert entry.entry_type == "meeting_note"

        # Step 4: Verify recovery notification
        mock_bot.send_message.assert_called()
        call_kwargs = mock_bot.send_message.call_args[1]
        assert "RECOVERED" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_retry_pending_calls_both_enrichment_and_transcription(
        self,
        session_factory,
        session,
    ):
        """Test that retry_pending() processes both enrichment and transcription."""
        # Create one pending enrichment and one pending transcription
        with session_factory() as s:
            e1 = Entry(
                raw_text="Pending enrichment note",
                source="telegram_text",
                status="pending_enrichment",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            e2 = Entry(
                raw_text="",
                source="telegram_voice",
                status="pending_transcription",
                audio_file_id="both_test_file_id",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            s.add_all([e1, e2])
            s.commit()

        enrichment_result_local = EnrichmentResult(
            intent="capture",
            clean_text="Pending enrichment note cleaned.",
            entry_type="personal",
            entities=[],
            is_open_loop=False,
            tags=[],
        )
        transcription_result_local = TranscriptionResult(
            text="Voice note text",
            confidence=0.9,
            language="en",
        )

        mock_enrichment = MagicMock()
        mock_enrichment.enrich_text = MagicMock(return_value=enrichment_result_local)

        mock_whisper = MagicMock()
        mock_whisper.transcribe = MagicMock(return_value=transcription_result_local)

        mock_file = MagicMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"audio"))
        mock_bot = MagicMock()
        mock_bot.get_file = AsyncMock(return_value=mock_file)
        mock_bot.send_message = AsyncMock()

        retry_manager = RetryManager(
            session_factory=session_factory,
            enrichment_service=mock_enrichment,
            whisper_client=mock_whisper,
            bot=mock_bot,
            chat_id=12345,
        )

        await retry_manager.retry_pending()

        # Both should have been processed
        mock_enrichment.enrich_text.assert_called()
        mock_whisper.transcribe.assert_called()
