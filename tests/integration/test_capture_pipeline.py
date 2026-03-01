"""Integration tests for the text capture pipeline.

Full flow: text message -> enrichment -> entity resolution -> connection scoring -> store -> confirm.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text

from second_brain.bot.handlers.message import handle_text_message
from second_brain.models.entry import Entry
from second_brain.models.entity import Entity, entry_entities
from second_brain.models.relation import EntryRelation
from second_brain.models.tag import Tag, entry_tags
from second_brain.prompts.enrichment import EnrichmentResult, ExtractedEntity
from second_brain.services.connection_scoring import (
    ConnectionScore,
    ConnectionScoringResponse,
)


@pytest.fixture
def enrichment_result_capture():
    """A realistic EnrichmentResult for a capture intent."""
    return EnrichmentResult(
        intent="capture",
        clean_text="Had a great meeting with Reynolds Electric about the supply chain improvements. They want to switch to a new vendor by Q3.",
        entry_type="meeting_note",
        entities=[
            ExtractedEntity(name="Reynolds Electric", type="company"),
            ExtractedEntity(name="Sarah Chen", type="person"),
        ],
        is_open_loop=True,
        follow_up_date="2026-03-15",
        tags=["supply-chain", "reynolds", "vendor"],
        calendar_event_id=None,
    )


@pytest.fixture
def enrichment_result_query():
    """A realistic EnrichmentResult for a query intent."""
    return EnrichmentResult(
        intent="query",
        clean_text="What did we discuss about Reynolds Electric?",
        entry_type="personal",
        entities=[],
        is_open_loop=False,
        follow_up_date=None,
        tags=[],
        calendar_event_id=None,
    )


@pytest.fixture
def mock_enrichment_service(enrichment_result_capture):
    """Mock EnrichmentService that returns configurable results."""
    service = MagicMock()
    service.enrich_text = MagicMock(return_value=enrichment_result_capture)
    return service


@pytest.fixture
def mock_connection_scoring_response():
    """Mock connection scoring response with realistic data."""
    return ConnectionScoringResponse(
        connections=[
            ConnectionScore(candidate_id=1, score=5, relation_type="follow_up_of"),
            ConnectionScore(candidate_id=2, score=4, relation_type="related"),
        ]
    )


class TestCapturePipeline:
    """Full text capture pipeline integration tests."""

    @pytest.mark.asyncio
    async def test_full_capture_pipeline(
        self,
        session_factory,
        session,
        mock_enrichment_service,
        enrichment_result_capture,
    ):
        """Test complete capture flow: text -> enrich -> entity resolution -> store."""
        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "Had a great meeting with Reynolds Electric about supply chain improvements"
        update.message.message_id = 100
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot_data = {
            "db_session_factory": session_factory,
            "enrichment": mock_enrichment_service,
            "anthropic_client": None,  # No connection scoring
        }
        context.user_data = {}

        await handle_text_message(update, context)

        # Verify enrichment service was called
        mock_enrichment_service.enrich_text.assert_called_once()

        # Verify entry was stored and enriched
        with session_factory() as s:
            entries = s.query(Entry).all()
            assert len(entries) == 1

            entry = entries[0]
            assert entry.raw_text == "Had a great meeting with Reynolds Electric about supply chain improvements"
            assert entry.clean_text == enrichment_result_capture.clean_text
            assert entry.entry_type == "meeting_note"
            assert entry.is_open_loop is True
            assert entry.status == "open"
            assert entry.source == "telegram_text"
            assert entry.follow_up_date is not None
            assert str(entry.follow_up_date) == "2026-03-15"

            # Verify entities were created and linked
            entities = s.query(Entity).all()
            assert len(entities) == 2
            entity_names = {e.name for e in entities}
            assert "Reynolds Electric" in entity_names
            assert "Sarah Chen" in entity_names

            # Verify entity-entry junction
            junctions = s.execute(
                entry_entities.select().where(entry_entities.c.entry_id == entry.id)
            ).fetchall()
            assert len(junctions) == 2

            # Verify tags were created and linked
            tags = s.query(Tag).all()
            tag_names = {t.name for t in tags}
            assert "supply-chain" in tag_names
            assert "reynolds" in tag_names
            assert "vendor" in tag_names

            # Verify tag-entry junction
            tag_junctions = s.execute(
                entry_tags.select().where(entry_tags.c.entry_id == entry.id)
            ).fetchall()
            assert len(tag_junctions) == 3

        # Verify confirmation was sent
        update.message.reply_text.assert_called()
        reply_text = update.message.reply_text.call_args[0][0]
        assert "Captured" in reply_text

    @pytest.mark.asyncio
    async def test_capture_with_connection_scoring(
        self,
        session_factory,
        session,
        mock_enrichment_service,
        mock_anthropic_client,
        mock_connection_scoring_response,
    ):
        """Test capture with connection scoring finding related entries."""
        # Seed existing entries that FTS can find
        with session_factory() as s:
            for text_content in [
                "Reynolds Electric supply chain delay report from last month",
                "Reynolds Electric quarterly financials review meeting notes",
            ]:
                existing = Entry(
                    raw_text=text_content,
                    clean_text=text_content,
                    source="telegram_text",
                    status="open",
                    entry_type="meeting_note",
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                s.add(existing)
            s.commit()

        mock_anthropic_client.call_haiku.return_value = mock_connection_scoring_response

        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "Reynolds Electric supply chain update"
        update.message.message_id = 101
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot_data = {
            "db_session_factory": session_factory,
            "enrichment": mock_enrichment_service,
            "anthropic_client": mock_anthropic_client,
        }
        context.user_data = {}

        await handle_text_message(update, context)

        # Verify confirmation was sent
        update.message.reply_text.assert_called()

    @pytest.mark.asyncio
    async def test_capture_stores_entry_before_enrichment(
        self,
        session_factory,
        session,
    ):
        """Test that entry is stored with pending_enrichment status before enrichment runs."""
        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "A test message"
        update.message.message_id = 102
        update.message.reply_text = AsyncMock()

        # No enrichment service means entry stays as pending_enrichment
        context = MagicMock()
        context.bot_data = {
            "db_session_factory": session_factory,
            "enrichment": None,
        }
        context.user_data = {}

        await handle_text_message(update, context)

        with session_factory() as s:
            entries = s.query(Entry).all()
            assert len(entries) == 1
            assert entries[0].status == "pending_enrichment"
            assert entries[0].raw_text == "A test message"

    @pytest.mark.asyncio
    async def test_enrichment_failure_keeps_pending_status(
        self,
        session_factory,
        session,
    ):
        """Test that enrichment failure keeps entry as pending_enrichment."""
        failing_enrichment = MagicMock()
        failing_enrichment.enrich_text = MagicMock(side_effect=Exception("API error"))

        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "Test message that will fail enrichment"
        update.message.message_id = 103
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
            # After enrichment failure, status remains pending_enrichment
            assert entries[0].status == "pending_enrichment"

        # User should be notified of the error
        update.message.reply_text.assert_called()
        reply_text = update.message.reply_text.call_args[0][0]
        assert "WARNING" in reply_text

    @pytest.mark.asyncio
    async def test_query_intent_routes_to_query_engine(
        self,
        session_factory,
        session,
        enrichment_result_query,
    ):
        """Test that query intent routes to query engine instead of capture."""
        query_enrichment = MagicMock()
        query_enrichment.enrich_text = MagicMock(return_value=enrichment_result_query)

        mock_query_result = MagicMock()
        mock_query_result.response = "Reynolds Electric was discussed in your meeting on Feb 15."
        mock_query_result.sources = []

        mock_query_engine = MagicMock()
        mock_query_engine.query = MagicMock(return_value=mock_query_result)

        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "What did we discuss about Reynolds Electric?"
        update.message.message_id = 104
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot_data = {
            "db_session_factory": session_factory,
            "enrichment": query_enrichment,
            "query_engine": mock_query_engine,
        }
        context.user_data = {}

        await handle_text_message(update, context)

        # Query engine should have been called
        mock_query_engine.query.assert_called_once()

    @pytest.mark.asyncio
    async def test_capture_with_entity_reuse(
        self,
        session_factory,
        session,
        mock_enrichment_service,
    ):
        """Test that entity resolution reuses existing entities on exact match."""
        # Pre-create an entity
        with session_factory() as s:
            existing_entity = Entity(
                name="Reynolds Electric",
                type="company",
                created_at=datetime.now(timezone.utc),
            )
            s.add(existing_entity)
            s.commit()
            existing_entity_id = existing_entity.id

        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "Meeting with Reynolds Electric about supply chain"
        update.message.message_id = 105
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot_data = {
            "db_session_factory": session_factory,
            "enrichment": mock_enrichment_service,
            "anthropic_client": None,
        }
        context.user_data = {}

        await handle_text_message(update, context)

        with session_factory() as s:
            # Should have 2 entities total: existing "Reynolds Electric" + new "Sarah Chen"
            companies = s.query(Entity).filter(Entity.type == "company").all()
            active_companies = [c for c in companies if c.merged_into_id is None]
            assert len(active_companies) == 1
            assert active_companies[0].id == existing_entity_id

    @pytest.mark.asyncio
    async def test_capture_with_no_message_text(self, session_factory):
        """Test that handler returns early when message has no text."""
        update = MagicMock()
        update.message = MagicMock()
        update.message.text = None

        context = MagicMock()
        context.bot_data = {"db_session_factory": session_factory}

        await handle_text_message(update, context)

        # No entries should be created
        with session_factory() as s:
            entries = s.query(Entry).all()
            assert len(entries) == 0

    @pytest.mark.asyncio
    async def test_capture_with_no_database(self):
        """Test that handler reports error when database is unavailable."""
        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "Test message"
        update.message.message_id = 106
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot_data = {}

        await handle_text_message(update, context)

        update.message.reply_text.assert_called()
        reply_text = update.message.reply_text.call_args[0][0]
        assert "WARNING" in reply_text
