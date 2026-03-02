"""Integration tests for error handling and retry flows.

Tests: API failure -> entry stored as pending -> retry -> success notification.
Covers enrichment failure paths (voice/transcription has been removed).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from second_brain.bot.handlers.message import handle_text_message
from second_brain.models.entry import Entry
from second_brain.prompts.enrichment import EnrichmentResult, ExtractedEntity
from second_brain.services.retry_manager import RetryManager
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

        event = {
            "type": "message",
            "text": "Test message for retry",
            "ts": "1234567890.300",
            "channel": "C123",
            "user": "U123",
        }
        say = AsyncMock()
        context = {
            "services": {
                "db_session_factory": session_factory,
                "enrichment": failing_enrichment,
            },
        }

        await handle_text_message(event, say, context)

        with session_factory() as s:
            entries = s.query(Entry).all()
            assert len(entries) == 1
            assert entries[0].status == "pending_enrichment"
            assert entries[0].raw_text == "Test message for retry"

        # User gets error notification
        say.assert_called()
        reply_text = say.call_args.kwargs["text"]
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
                source="slack_text",
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

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()

        retry_manager = RetryManager(
            session_factory=session_factory,
            enrichment_service=mock_enrichment,
            client=mock_client,
            channel_id="C123",
        )

        await retry_manager.retry_pending_enrichments()

        # Entry should now be enriched and open
        with session_factory() as s:
            entry = s.get(Entry, entry_id)
            assert entry.status == "open"
            assert entry.clean_text == enrichment_result.clean_text
            assert entry.entry_type == "meeting_note"

        # Recovery notification should be sent
        mock_client.chat_postMessage.assert_called()
        call_kwargs = mock_client.chat_postMessage.call_args.kwargs
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
                source="slack_text",
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
                source="slack_text",
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

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()

        retry_manager = RetryManager(
            session_factory=session_factory,
            enrichment_service=failing_enrichment,
            client=mock_client,
            channel_id="C123",
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
        mock_client.chat_postMessage.assert_called()
        last_call = mock_client.chat_postMessage.call_args.kwargs
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
        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()

        retry_manager = RetryManager(
            session_factory=session_factory,
            enrichment_service=mock_enrichment,
            client=mock_client,
            channel_id="C123",
        )

        await retry_manager.retry_pending_enrichments()

        # Enrichment should not have been called
        mock_enrichment.enrich_text.assert_not_called()
        mock_client.chat_postMessage.assert_not_called()


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

        event = {
            "type": "message",
            "text": "Important note about Reynolds Electric",
            "ts": "1234567890.400",
            "channel": "C123",
            "user": "U123",
        }
        say = AsyncMock()
        context = {
            "services": {
                "db_session_factory": session_factory,
                "enrichment": failing_enrichment,
            },
        }

        await handle_text_message(event, say, context)

        # Entry should be stored as pending_enrichment
        with session_factory() as s:
            entries = s.query(Entry).all()
            assert len(entries) == 1
            assert entries[0].status == "pending_enrichment"
            entry_id = entries[0].id

        # Step 2: Create retry manager with working enrichment
        success_enrichment = MagicMock()
        success_enrichment.enrich_text = MagicMock(return_value=enrichment_result)

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()

        retry_manager = RetryManager(
            session_factory=session_factory,
            enrichment_service=success_enrichment,
            client=mock_client,
            channel_id="C123",
        )

        await retry_manager.retry_pending_enrichments()

        # Step 3: Verify entry is now enriched
        with session_factory() as s:
            entry = s.get(Entry, entry_id)
            assert entry.status == "open"
            assert entry.clean_text == enrichment_result.clean_text
            assert entry.entry_type == "meeting_note"

        # Step 4: Verify recovery notification was sent
        mock_client.chat_postMessage.assert_called()
        call_kwargs = mock_client.chat_postMessage.call_args.kwargs
        assert "RECOVERED" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_retry_pending_calls_enrichment(
        self,
        session_factory,
        session,
    ):
        """Test that retry_pending() processes pending enrichments."""
        with session_factory() as s:
            e1 = Entry(
                raw_text="Pending enrichment note",
                source="slack_text",
                status="pending_enrichment",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            s.add(e1)
            s.commit()

        enrichment_result_local = EnrichmentResult(
            intent="capture",
            clean_text="Pending enrichment note cleaned.",
            entry_type="personal",
            entities=[],
            is_open_loop=False,
            tags=[],
        )

        mock_enrichment = MagicMock()
        mock_enrichment.enrich_text = MagicMock(return_value=enrichment_result_local)

        mock_client = AsyncMock()
        mock_client.chat_postMessage = AsyncMock()

        retry_manager = RetryManager(
            session_factory=session_factory,
            enrichment_service=mock_enrichment,
            client=mock_client,
            channel_id="C123",
        )

        await retry_manager.retry_pending()

        # Enrichment should have been called
        mock_enrichment.enrich_text.assert_called()

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
            client=None,
            channel_id=None,
        )

        # Should not raise
        await retry_manager.retry_pending()
