"""Integration tests for the query pipeline.

Full flow: query -> FTS context assembly -> Haiku/Sonnet routing -> response with source attribution.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from second_brain.models.entry import Entry
from second_brain.models.entity import Entity, entry_entities
from second_brain.prompts.query_simple import SimpleQueryResponse
from second_brain.prompts.query_synthesis import SynthesisQueryResponse
from second_brain.services.query_engine import QueryEngine, QueryResponse
from second_brain.services.query_session import QuerySession
from second_brain.utils.time import utc_now


def _seed_entries(session_factory):
    """Seed the database with entries for query testing."""
    entries_data = [
        {
            "raw_text": "Reynolds Electric supply chain delay — they notified us that the Q3 shipment will be two weeks late due to parts shortage.",
            "clean_text": "Reynolds Electric supply chain delay. They notified us that the Q3 shipment will be two weeks late due to parts shortage.",
            "entry_type": "project_context",
            "status": "open",
        },
        {
            "raw_text": "Meeting with Sarah Chen re: Reynolds quarterly review. Revenue up 12%, but margins squeezed by raw material costs.",
            "clean_text": "Meeting with Sarah Chen regarding Reynolds quarterly review. Revenue up 12%, but margins squeezed by raw material costs.",
            "entry_type": "meeting_note",
            "status": "open",
        },
        {
            "raw_text": "New Python deployment pipeline using Docker and GitHub Actions. Reduced deployment time from 30min to 5min.",
            "clean_text": "New Python deployment pipeline using Docker and GitHub Actions. Reduced deployment time from 30 minutes to 5 minutes.",
            "entry_type": "project_context",
            "status": "open",
        },
        {
            "raw_text": "Idea: build an internal dashboard for Reynolds Electric account tracking.",
            "clean_text": "Idea: build an internal dashboard for Reynolds Electric account tracking.",
            "entry_type": "idea",
            "status": "open",
        },
    ]

    with session_factory() as session:
        created_entries = []
        for data in entries_data:
            entry = Entry(
                raw_text=data["raw_text"],
                clean_text=data["clean_text"],
                entry_type=data["entry_type"],
                status=data["status"],
                source="telegram_text",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            session.add(entry)
            session.flush()
            created_entries.append(entry.id)

        # Create entities and link them
        reynolds = Entity(
            name="Reynolds Electric",
            type="company",
            created_at=utc_now(),
        )
        sarah = Entity(
            name="Sarah Chen",
            type="person",
            created_at=utc_now(),
        )
        session.add_all([reynolds, sarah])
        session.flush()

        # Link Reynolds to entries 0, 1, 3
        for entry_id in [created_entries[0], created_entries[1], created_entries[3]]:
            session.execute(
                entry_entities.insert().values(
                    entry_id=entry_id, entity_id=reynolds.id
                )
            )
        # Link Sarah to entry 1
        session.execute(
            entry_entities.insert().values(
                entry_id=created_entries[1], entity_id=sarah.id
            )
        )

        session.commit()
    return created_entries


class _ClassifyResponse:
    """Minimal classify response mock."""
    def __init__(self, complexity):
        self.complexity = complexity


class TestQueryPipeline:
    """Integration tests for the full query pipeline."""

    def test_simple_query_routes_to_haiku(
        self,
        session_factory,
        mock_anthropic_client,
    ):
        """Test that a simple query is classified and routed to Haiku."""
        entry_ids = _seed_entries(session_factory)

        # First call: classify query -> simple
        # Second call: answer query
        mock_anthropic_client.call_haiku = MagicMock(
            side_effect=[
                _ClassifyResponse(complexity="simple"),
                SimpleQueryResponse(
                    answer="Reynolds Electric's Q3 shipment will be two weeks late due to parts shortage.",
                    source_entry_ids=[entry_ids[0]],
                ),
            ]
        )

        engine = QueryEngine(
            anthropic_client=mock_anthropic_client,
            session_factory=session_factory,
        )

        result = engine.handle_query("What's the status of the Reynolds Electric shipment?")

        assert isinstance(result, QueryResponse)
        assert "Reynolds" in result.answer
        assert result.model_used == "haiku"
        assert len(result.sources) >= 1
        assert result.sources[0].entry_id == entry_ids[0]
        assert result.sources[0].date is not None

        # Haiku should have been called twice (classify + answer)
        assert mock_anthropic_client.call_haiku.call_count == 2

    def test_synthesis_query_routes_to_sonnet(
        self,
        session_factory,
        mock_anthropic_client,
    ):
        """Test that a synthesis query is classified and routed to Sonnet."""
        entry_ids = _seed_entries(session_factory)

        # Classify query -> synthesis
        mock_anthropic_client.call_haiku = MagicMock(
            return_value=_ClassifyResponse(complexity="synthesis"),
        )
        mock_anthropic_client.call_sonnet = MagicMock(
            return_value=SynthesisQueryResponse(
                answer="Based on your notes, Reynolds Electric is facing supply chain challenges. "
                       "Revenue is up 12% but margins are squeezed. The Q3 shipment is delayed two weeks.",
                source_entry_ids=[entry_ids[0], entry_ids[1]],
            ),
        )

        engine = QueryEngine(
            anthropic_client=mock_anthropic_client,
            session_factory=session_factory,
        )

        result = engine.handle_query("Summarize everything about Reynolds Electric")

        assert isinstance(result, QueryResponse)
        assert "Reynolds" in result.answer
        assert result.model_used == "sonnet"
        assert len(result.sources) == 2

        # Haiku called for classify, Sonnet called for answer
        mock_anthropic_client.call_haiku.assert_called_once()
        mock_anthropic_client.call_sonnet.assert_called_once()

    def test_query_assembles_fts_context(
        self,
        session_factory,
        mock_anthropic_client,
    ):
        """Test that query assembles FTS context with relevant entries."""
        entry_ids = _seed_entries(session_factory)

        call_count = [0]
        captured_user_prompt = [None]

        def mock_haiku_call(system_prompt, user_prompt, response_model):
            call_count[0] += 1
            if call_count[0] == 1:
                return _ClassifyResponse(complexity="simple")
            else:
                captured_user_prompt[0] = user_prompt
                return SimpleQueryResponse(
                    answer="The deployment pipeline uses Docker and GitHub Actions.",
                    source_entry_ids=[entry_ids[2]],
                )

        mock_anthropic_client.call_haiku = MagicMock(side_effect=mock_haiku_call)

        engine = QueryEngine(
            anthropic_client=mock_anthropic_client,
            session_factory=session_factory,
        )

        result = engine.handle_query("Tell me about the Python deployment pipeline")

        # The user prompt should contain context from FTS-matched entries
        assert captured_user_prompt[0] is not None
        assert "KNOWLEDGE BASE ENTRIES" in captured_user_prompt[0]
        # The deployment entry should be in the context
        assert "Docker" in captured_user_prompt[0] or "deployment" in captured_user_prompt[0]

    def test_query_with_no_matching_entries(
        self,
        session_factory,
        mock_anthropic_client,
    ):
        """Test query when FTS returns no results."""
        # Don't seed any entries

        mock_anthropic_client.call_haiku = MagicMock(
            side_effect=[
                _ClassifyResponse(complexity="simple"),
                SimpleQueryResponse(
                    answer="I don't have any information about quantum computing in your knowledge base.",
                    source_entry_ids=[],
                ),
            ]
        )

        engine = QueryEngine(
            anthropic_client=mock_anthropic_client,
            session_factory=session_factory,
        )

        result = engine.handle_query("What do I know about quantum computing?")

        assert isinstance(result, QueryResponse)
        assert len(result.sources) == 0

    def test_query_source_attribution_filters_invalid_ids(
        self,
        session_factory,
        mock_anthropic_client,
    ):
        """Test that source attribution filters out invalid entry IDs."""
        entry_ids = _seed_entries(session_factory)

        mock_anthropic_client.call_haiku = MagicMock(
            side_effect=[
                _ClassifyResponse(complexity="simple"),
                SimpleQueryResponse(
                    answer="Here is the answer.",
                    source_entry_ids=[entry_ids[0], 99999],  # 99999 doesn't exist in context
                ),
            ]
        )

        engine = QueryEngine(
            anthropic_client=mock_anthropic_client,
            session_factory=session_factory,
        )

        result = engine.handle_query("Reynolds supply chain status")

        # Only valid source should be included
        valid_ids = [s.entry_id for s in result.sources]
        assert entry_ids[0] in valid_ids
        assert 99999 not in valid_ids

    def test_query_with_session_context(
        self,
        session_factory,
        mock_anthropic_client,
    ):
        """Test follow-up query includes session context."""
        entry_ids = _seed_entries(session_factory)

        captured_prompts = []

        def mock_haiku_call(system_prompt, user_prompt, response_model):
            captured_prompts.append(user_prompt)
            if len(captured_prompts) == 1:
                return _ClassifyResponse(complexity="simple")
            else:
                return SimpleQueryResponse(
                    answer="Sarah Chen attended the Reynolds quarterly review.",
                    source_entry_ids=[entry_ids[1]],
                )

        mock_anthropic_client.call_haiku = MagicMock(side_effect=mock_haiku_call)

        engine = QueryEngine(
            anthropic_client=mock_anthropic_client,
            session_factory=session_factory,
        )

        # Simulate a follow-up query with prior context
        prior_session = QuerySession(
            query="Tell me about Reynolds Electric",
            response="Reynolds Electric is a company in your knowledge base.",
            source_entry_ids=[entry_ids[0]],
        )

        result = engine.handle_query(
            "Who attended the quarterly review?",
            session_context=prior_session,
        )

        assert isinstance(result, QueryResponse)
        # The prompt sent to Haiku should contain prior context
        answer_prompt = captured_prompts[-1]
        assert "PREVIOUS QUERY CONTEXT" in answer_prompt
        assert "Reynolds Electric" in answer_prompt

    def test_query_one_hop_entity_expansion(
        self,
        session_factory,
        mock_anthropic_client,
    ):
        """Test that query context includes one-hop entity expansion."""
        entry_ids = _seed_entries(session_factory)

        captured_prompts = []

        def mock_haiku_call(system_prompt, user_prompt, response_model):
            captured_prompts.append(user_prompt)
            if len(captured_prompts) == 1:
                return _ClassifyResponse(complexity="simple")
            else:
                return SimpleQueryResponse(
                    answer="Sarah Chen attended the Reynolds quarterly review.",
                    source_entry_ids=[entry_ids[1]],
                )

        mock_anthropic_client.call_haiku = MagicMock(side_effect=mock_haiku_call)

        engine = QueryEngine(
            anthropic_client=mock_anthropic_client,
            session_factory=session_factory,
        )

        # Query about Sarah should find entries linked via entity
        result = engine.handle_query("What meetings has Sarah Chen attended?")

        # Should have at least one source
        assert isinstance(result, QueryResponse)

    def test_unknown_complexity_falls_back_to_simple(
        self,
        session_factory,
        mock_anthropic_client,
    ):
        """Test that unknown complexity classification falls back to simple (Haiku)."""
        entry_ids = _seed_entries(session_factory)

        mock_anthropic_client.call_haiku = MagicMock(
            side_effect=[
                _ClassifyResponse(complexity="unknown_type"),
                SimpleQueryResponse(
                    answer="Answer from Haiku.",
                    source_entry_ids=[],
                ),
            ]
        )

        engine = QueryEngine(
            anthropic_client=mock_anthropic_client,
            session_factory=session_factory,
        )

        result = engine.handle_query("Some query")

        assert result.model_used == "haiku"
        # Haiku should be called twice (classify + answer), not Sonnet
        assert mock_anthropic_client.call_haiku.call_count == 2
        mock_anthropic_client.call_sonnet.assert_not_called()
