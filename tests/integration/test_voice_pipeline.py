"""Integration tests for the voice capture pipeline.

Full flow: voice message -> Whisper transcription -> enrichment -> store.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from second_brain.bot.handlers.voice import handle_voice_message
from second_brain.models.entry import Entry
from second_brain.models.entity import Entity
from second_brain.models.tag import Tag
from second_brain.prompts.enrichment import EnrichmentResult, ExtractedEntity
from second_brain.services.whisper_client import TranscriptionResult


@pytest.fixture
def high_confidence_transcription():
    """Transcription result with high confidence (auto-process)."""
    return TranscriptionResult(
        text="Had a meeting with Sarah Chen from Reynolds Electric about the deployment timeline",
        confidence=0.95,
        language="en",
    )


@pytest.fixture
def low_confidence_transcription():
    """Transcription result with low confidence (needs user confirmation)."""
    return TranscriptionResult(
        text="Something about Reynolds maybe supply chain",
        confidence=0.6,
        language="en",
    )


@pytest.fixture
def voice_enrichment_result():
    """EnrichmentResult for transcribed voice content."""
    return EnrichmentResult(
        intent="capture",
        clean_text="Had a meeting with Sarah Chen from Reynolds Electric about the deployment timeline.",
        entry_type="meeting_note",
        entities=[
            ExtractedEntity(name="Sarah Chen", type="person"),
            ExtractedEntity(name="Reynolds Electric", type="company"),
        ],
        is_open_loop=True,
        follow_up_date="2026-03-10",
        tags=["deployment", "reynolds", "timeline"],
        calendar_event_id=None,
    )


@pytest.fixture
def mock_voice_enrichment(voice_enrichment_result):
    """Mock enrichment service for voice pipeline."""
    service = MagicMock()
    service.enrich_text = MagicMock(return_value=voice_enrichment_result)
    return service


@pytest.fixture
def mock_telegram_file():
    """Mock Telegram file object for audio download."""
    file = MagicMock()
    file.download_as_bytearray = AsyncMock(return_value=bytearray(b"fake audio data"))
    return file


class TestVoicePipeline:
    """Full voice capture pipeline integration tests."""

    @pytest.mark.asyncio
    async def test_full_voice_pipeline_high_confidence(
        self,
        session_factory,
        session,
        mock_whisper_client,
        mock_voice_enrichment,
        high_confidence_transcription,
        mock_telegram_file,
    ):
        """Test full voice flow: download -> transcribe -> enrich -> store (high confidence)."""
        mock_whisper_client.transcribe = MagicMock(return_value=high_confidence_transcription)

        update = MagicMock()
        update.message = MagicMock()
        update.message.text = None
        update.message.message_id = 200
        update.message.reply_text = AsyncMock()
        voice = MagicMock()
        voice.file_id = "test_voice_file_id"
        voice.file_unique_id = "test_voice_unique_id"
        update.message.voice = voice

        context = MagicMock()
        context.bot_data = {
            "db_session_factory": session_factory,
            "whisper_client": mock_whisper_client,
            "enrichment": mock_voice_enrichment,
            "anthropic_client": None,
        }
        context.user_data = {}
        context.bot = MagicMock()
        context.bot.get_file = AsyncMock(return_value=mock_telegram_file)

        await handle_voice_message(update, context)

        # Verify whisper was called
        mock_whisper_client.transcribe.assert_called_once()

        # Verify enrichment was called with transcribed text
        mock_voice_enrichment.enrich_text.assert_called_once()
        call_kwargs = mock_voice_enrichment.enrich_text.call_args
        assert high_confidence_transcription.text in str(call_kwargs)

        # Verify entry was stored and enriched
        with session_factory() as s:
            entries = s.query(Entry).all()
            assert len(entries) == 1

            entry = entries[0]
            assert entry.raw_text == high_confidence_transcription.text
            assert entry.source == "telegram_voice"
            assert entry.status == "open"
            assert entry.entry_type == "meeting_note"
            assert entry.is_open_loop is True

            # Verify entities were created
            entities = s.query(Entity).all()
            assert len(entities) == 2
            entity_names = {e.name for e in entities}
            assert "Sarah Chen" in entity_names
            assert "Reynolds Electric" in entity_names

            # Verify tags were created
            tags = s.query(Tag).all()
            tag_names = {t.name for t in tags}
            assert "deployment" in tag_names
            assert "reynolds" in tag_names

        # Verify confirmation was sent
        update.message.reply_text.assert_called()
        last_reply = update.message.reply_text.call_args_list[-1][0][0]
        assert "Captured" in last_reply

    @pytest.mark.asyncio
    async def test_voice_low_confidence_asks_confirmation(
        self,
        session_factory,
        session,
        mock_whisper_client,
        low_confidence_transcription,
        mock_telegram_file,
    ):
        """Test that low confidence transcription asks for user confirmation."""
        mock_whisper_client.transcribe = MagicMock(return_value=low_confidence_transcription)

        update = MagicMock()
        update.message = MagicMock()
        update.message.text = None
        update.message.message_id = 201
        update.message.reply_text = AsyncMock()
        voice = MagicMock()
        voice.file_id = "test_voice_file_id_2"
        voice.file_unique_id = "test_voice_unique_id_2"
        update.message.voice = voice

        context = MagicMock()
        context.bot_data = {
            "db_session_factory": session_factory,
            "whisper_client": mock_whisper_client,
            "enrichment": MagicMock(),  # Enrichment shouldn't be called
        }
        context.user_data = {}
        context.bot = MagicMock()
        context.bot.get_file = AsyncMock(return_value=mock_telegram_file)

        await handle_voice_message(update, context)

        # Verify the user is asked to confirm
        update.message.reply_text.assert_called()
        reply_text = update.message.reply_text.call_args[0][0]
        assert "I heard" in reply_text
        assert low_confidence_transcription.text in reply_text
        assert "correct" in reply_text.lower()

        # Verify entry was stored with raw transcription
        with session_factory() as s:
            entries = s.query(Entry).all()
            assert len(entries) == 1
            assert entries[0].raw_text == low_confidence_transcription.text
            assert entries[0].source == "telegram_voice"

    @pytest.mark.asyncio
    async def test_voice_stores_entry_before_transcription(
        self,
        session_factory,
        session,
    ):
        """Test that entry is stored with pending_transcription status before transcription."""
        update = MagicMock()
        update.message = MagicMock()
        update.message.text = None
        update.message.message_id = 202
        update.message.reply_text = AsyncMock()
        voice = MagicMock()
        voice.file_id = "test_voice_file_id_3"
        voice.file_unique_id = "test_voice_unique_id_3"
        update.message.voice = voice

        context = MagicMock()
        context.bot_data = {
            "db_session_factory": session_factory,
            "whisper_client": None,  # No whisper client
        }
        context.user_data = {}

        await handle_voice_message(update, context)

        with session_factory() as s:
            entries = s.query(Entry).all()
            assert len(entries) == 1
            assert entries[0].status == "pending_transcription"
            assert entries[0].audio_file_id == "test_voice_file_id_3"

    @pytest.mark.asyncio
    async def test_voice_download_failure(
        self,
        session_factory,
        session,
        mock_whisper_client,
    ):
        """Test that download failure keeps entry as pending_transcription."""
        update = MagicMock()
        update.message = MagicMock()
        update.message.text = None
        update.message.message_id = 203
        update.message.reply_text = AsyncMock()
        voice = MagicMock()
        voice.file_id = "test_voice_file_id_4"
        voice.file_unique_id = "test_voice_unique_id_4"
        update.message.voice = voice

        context = MagicMock()
        context.bot_data = {
            "db_session_factory": session_factory,
            "whisper_client": mock_whisper_client,
        }
        context.user_data = {}
        context.bot = MagicMock()
        context.bot.get_file = AsyncMock(side_effect=Exception("Download failed"))

        await handle_voice_message(update, context)

        # Entry should exist
        with session_factory() as s:
            entries = s.query(Entry).all()
            assert len(entries) == 1
            assert entries[0].status == "pending_transcription"

        # User should be notified
        update.message.reply_text.assert_called()
        reply_text = update.message.reply_text.call_args[0][0]
        assert "WARNING" in reply_text

    @pytest.mark.asyncio
    async def test_voice_transcription_failure(
        self,
        session_factory,
        session,
        mock_telegram_file,
    ):
        """Test that transcription failure keeps entry as pending_transcription."""
        failing_whisper = MagicMock()
        failing_whisper.transcribe = MagicMock(side_effect=Exception("Whisper API error"))

        update = MagicMock()
        update.message = MagicMock()
        update.message.text = None
        update.message.message_id = 204
        update.message.reply_text = AsyncMock()
        voice = MagicMock()
        voice.file_id = "test_voice_file_id_5"
        voice.file_unique_id = "test_voice_unique_id_5"
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

        # Entry should exist
        with session_factory() as s:
            entries = s.query(Entry).all()
            assert len(entries) == 1
            assert entries[0].status == "pending_transcription"

        # User should be notified of transcription failure
        update.message.reply_text.assert_called()
        reply_text = update.message.reply_text.call_args[0][0]
        assert "WARNING" in reply_text

    @pytest.mark.asyncio
    async def test_voice_enrichment_failure_after_transcription(
        self,
        session_factory,
        session,
        mock_whisper_client,
        high_confidence_transcription,
        mock_telegram_file,
    ):
        """Test that enrichment failure after successful transcription stores transcription."""
        mock_whisper_client.transcribe = MagicMock(return_value=high_confidence_transcription)

        failing_enrichment = MagicMock()
        failing_enrichment.enrich_text = MagicMock(side_effect=Exception("Enrichment failed"))

        update = MagicMock()
        update.message = MagicMock()
        update.message.text = None
        update.message.message_id = 205
        update.message.reply_text = AsyncMock()
        voice = MagicMock()
        voice.file_id = "test_voice_file_id_6"
        voice.file_unique_id = "test_voice_unique_id_6"
        update.message.voice = voice

        context = MagicMock()
        context.bot_data = {
            "db_session_factory": session_factory,
            "whisper_client": mock_whisper_client,
            "enrichment": failing_enrichment,
        }
        context.user_data = {}
        context.bot = MagicMock()
        context.bot.get_file = AsyncMock(return_value=mock_telegram_file)

        await handle_voice_message(update, context)

        # Entry should have the transcribed text stored
        with session_factory() as s:
            entries = s.query(Entry).all()
            assert len(entries) == 1
            assert entries[0].raw_text == high_confidence_transcription.text
            # Status should be pending_enrichment after enrichment failure
            assert entries[0].status == "pending_enrichment"

    @pytest.mark.asyncio
    async def test_voice_no_voice_message(self, session_factory):
        """Test that handler returns early when message has no voice."""
        update = MagicMock()
        update.message = MagicMock()
        update.message.voice = None

        context = MagicMock()
        context.bot_data = {"db_session_factory": session_factory}

        await handle_voice_message(update, context)

        with session_factory() as s:
            entries = s.query(Entry).all()
            assert len(entries) == 0
