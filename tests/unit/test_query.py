"""Tests for QueryEngine — query classification, context assembly, and response routing."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from second_brain.models import Base
from second_brain.models.entry import Entry
from second_brain.models.entity import Entity, entry_entities
from second_brain.prompts.query_simple import SimpleQueryResponse
from second_brain.prompts.query_synthesis import SynthesisQueryResponse
from second_brain.services.query_engine import (
    QueryEngine,
    QueryResponse,
    QuerySource,
    _ClassifyResponse,
)
from second_brain.utils.time import utc_now


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    with eng.connect() as conn:
        conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5("
            "clean_text, content='entries', content_rowid='id')"
        ))
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN "
            "INSERT INTO entries_fts(rowid, clean_text) VALUES (new.id, new.clean_text); "
            "END;"
        ))
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN "
            "INSERT INTO entries_fts(entries_fts, rowid, clean_text) "
            "VALUES ('delete', old.id, old.clean_text); "
            "INSERT INTO entries_fts(rowid, clean_text) VALUES (new.id, new.clean_text); "
            "END;"
        ))
        now = utc_now().isoformat()
        conn.execute(text(
            "INSERT INTO config (key, value, updated_at) VALUES "
            "('query_max_entries', '30', :now)"
        ), {"now": now})
        conn.commit()
    return eng


@pytest.fixture
def db_session(engine):
    SessionLocal = sessionmaker(bind=engine)
    sess = SessionLocal()
    yield sess
    sess.close()


@pytest.fixture
def sf(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def mock_client():
    return MagicMock()


def _add_entry(session, clean_text, entry_type="personal"):
    entry = Entry(
        raw_text=clean_text,
        clean_text=clean_text,
        source="slack_text",
        entry_type=entry_type,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(entry)
    session.flush()
    return entry


def _add_entity(session, name, entity_type="person"):
    entity = Entity(
        name=name,
        type=entity_type,
        created_at=utc_now(),
    )
    session.add(entity)
    session.flush()
    return entity


class TestClassifyQuery:
    """Test complexity classification: simple vs synthesis routing."""

    def test_classify_simple(self, mock_client, sf):
        mock_client.call_haiku.return_value = _ClassifyResponse(complexity="simple")
        engine = QueryEngine(mock_client, sf)
        result = engine._classify_query("When is the Reynolds meeting?")
        assert result == "simple"

    def test_classify_synthesis(self, mock_client, sf):
        mock_client.call_haiku.return_value = _ClassifyResponse(complexity="synthesis")
        engine = QueryEngine(mock_client, sf)
        result = engine._classify_query("Compare all my project notes from February")
        assert result == "synthesis"

    def test_classify_unknown_falls_back_to_simple(self, mock_client, sf):
        mock_client.call_haiku.return_value = _ClassifyResponse(complexity="unknown")
        engine = QueryEngine(mock_client, sf)
        result = engine._classify_query("Something weird")
        assert result == "simple"


class TestAssembleContext:
    """Test context assembly: FTS results + entity links + one-hop expansion."""

    def test_fts_results_returned(self, db_session, sf, mock_client):
        _add_entry(db_session, "Reynolds Electric supply chain meeting")
        _add_entry(db_session, "Reynolds Electric quarterly review")
        _add_entry(db_session, "Unrelated Python deployment notes")
        db_session.commit()

        engine = QueryEngine(mock_client, sf)
        with sf() as sess:
            results = engine._assemble_context(sess, "Reynolds Electric", 30)
        assert len(results) >= 2
        texts = [e.clean_text for e in results]
        assert all("Reynolds" in t for t in texts)

    def test_empty_fts_returns_empty(self, db_session, sf, mock_client):
        _add_entry(db_session, "Reynolds Electric meeting notes")
        db_session.commit()

        engine = QueryEngine(mock_client, sf)
        with sf() as sess:
            results = engine._assemble_context(sess, "xyznonexistent", 30)
        assert results == []

    def test_one_hop_expansion_via_entities(self, db_session, sf, mock_client):
        # e1 matches FTS and links to entity
        e1 = _add_entry(db_session, "Reynolds Electric supply chain update")
        # e2 doesn't match FTS but links to same entity
        e2 = _add_entry(db_session, "Quarterly financials review notes")
        entity = _add_entity(db_session, "Reynolds Electric", "company")
        db_session.execute(
            entry_entities.insert().values(entry_id=e1.id, entity_id=entity.id)
        )
        db_session.execute(
            entry_entities.insert().values(entry_id=e2.id, entity_id=entity.id)
        )
        db_session.commit()

        engine = QueryEngine(mock_client, sf)
        with sf() as sess:
            results = engine._assemble_context(sess, "Reynolds supply", 30)
        result_ids = [e.id for e in results]
        assert e1.id in result_ids
        # e2 should be included via one-hop entity expansion
        assert e2.id in result_ids

    def test_max_entries_cap(self, db_session, sf, mock_client):
        for i in range(10):
            _add_entry(db_session, f"Reynolds Electric note {i}")
        db_session.commit()

        engine = QueryEngine(mock_client, sf)
        with sf() as sess:
            results = engine._assemble_context(sess, "Reynolds", max_entries=3)
        assert len(results) <= 3

    def test_deduplication(self, db_session, sf, mock_client):
        # Entry matches both FTS and entity one-hop — should appear once
        e1 = _add_entry(db_session, "Reynolds Electric pipeline discussion")
        entity = _add_entity(db_session, "Reynolds Electric", "company")
        db_session.execute(
            entry_entities.insert().values(entry_id=e1.id, entity_id=entity.id)
        )
        db_session.commit()

        engine = QueryEngine(mock_client, sf)
        with sf() as sess:
            results = engine._assemble_context(sess, "Reynolds pipeline", 30)
        ids = [e.id for e in results]
        assert len(ids) == len(set(ids)), "Duplicate entries found"


class TestHandleQuery:
    """Test QueryEngine.handle_query() with mocked AnthropicClient."""

    def test_simple_query_uses_haiku(self, db_session, sf, mock_client):
        e1 = _add_entry(db_session, "Reynolds Electric meeting on Tuesday")
        db_session.commit()

        # Classify as simple
        mock_client.call_haiku.side_effect = [
            _ClassifyResponse(complexity="simple"),
            SimpleQueryResponse(
                answer="The Reynolds meeting is on Tuesday.",
                source_entry_ids=[e1.id],
            ),
        ]

        engine = QueryEngine(mock_client, sf)
        response = engine.handle_query("When is the Reynolds meeting?")

        assert response.model_used == "haiku"
        assert "Tuesday" in response.answer
        assert len(response.sources) == 1
        assert response.sources[0].entry_id == e1.id
        # call_haiku called twice: classify + answer
        assert mock_client.call_haiku.call_count == 2
        mock_client.call_sonnet.assert_not_called()

    def test_synthesis_query_uses_sonnet(self, db_session, sf, mock_client):
        e1 = _add_entry(db_session, "Reynolds Electric Q1 supply chain delays")
        e2 = _add_entry(db_session, "Reynolds Electric Q2 supply improvements")
        db_session.commit()

        # Classify as synthesis
        mock_client.call_haiku.return_value = _ClassifyResponse(complexity="synthesis")
        mock_client.call_sonnet.return_value = SynthesisQueryResponse(
            answer="Comparing Q1 and Q2, Reynolds improved their supply chain.",
            source_entry_ids=[e1.id, e2.id],
        )

        engine = QueryEngine(mock_client, sf)
        response = engine.handle_query("Compare Reynolds supply chain across quarters")

        assert response.model_used == "sonnet"
        assert len(response.sources) == 2
        mock_client.call_sonnet.assert_called_once()

    def test_source_attribution(self, db_session, sf, mock_client):
        """Test source attribution: entry_id, date, and snippet are correct."""
        e1 = _add_entry(db_session, "Meeting with Reynolds about Python deployment pipeline")
        db_session.commit()

        mock_client.call_haiku.side_effect = [
            _ClassifyResponse(complexity="simple"),
            SimpleQueryResponse(
                answer="You discussed Python deployment with Reynolds.",
                source_entry_ids=[e1.id],
            ),
        ]

        engine = QueryEngine(mock_client, sf)
        response = engine.handle_query("What did I discuss with Reynolds?")

        assert len(response.sources) == 1
        src = response.sources[0]
        assert src.entry_id == e1.id
        assert src.date == e1.created_at.strftime("%Y-%m-%d")
        assert "Reynolds" in src.snippet
        assert "Python deployment" in src.snippet

    def test_unknown_source_id_skipped(self, db_session, sf, mock_client):
        """If the LLM returns an entry ID not in context, it's silently skipped."""
        e1 = _add_entry(db_session, "Reynolds Electric meeting notes")
        db_session.commit()

        mock_client.call_haiku.side_effect = [
            _ClassifyResponse(complexity="simple"),
            SimpleQueryResponse(
                answer="Some answer",
                source_entry_ids=[e1.id, 99999],
            ),
        ]

        engine = QueryEngine(mock_client, sf)
        response = engine.handle_query("Reynolds meeting")

        # Only e1 should appear in sources; 99999 is silently skipped
        assert len(response.sources) == 1
        assert response.sources[0].entry_id == e1.id

    def test_no_fts_results_still_returns_answer(self, db_session, sf, mock_client):
        """When FTS returns nothing, LLM still gets called and can say 'not found'."""
        _add_entry(db_session, "Completely unrelated topic about cats")
        db_session.commit()

        mock_client.call_haiku.side_effect = [
            _ClassifyResponse(complexity="simple"),
            SimpleQueryResponse(
                answer="No matching entries found for your question.",
                source_entry_ids=[],
            ),
        ]

        engine = QueryEngine(mock_client, sf)
        response = engine.handle_query("xyznonexistent topic")

        assert response.sources == []
        assert "No matching" in response.answer


class TestHandleQueryWithConversationHistory:
    """Test queries with conversation history context."""

    def test_conversation_history_included_in_prompt(self, db_session, sf, mock_client):
        e1 = _add_entry(db_session, "Reynolds Electric supply chain details")
        db_session.commit()

        history = [
            {"role": "user", "text": "What about Reynolds Electric?"},
            {"role": "assistant", "text": "Reynolds Electric is a supply chain company."},
        ]

        mock_client.call_haiku.side_effect = [
            _ClassifyResponse(complexity="simple"),
            SimpleQueryResponse(
                answer="Their supply chain had delays in Q1.",
                source_entry_ids=[e1.id],
            ),
        ]

        engine = QueryEngine(mock_client, sf)
        response = engine.handle_query(
            "Tell me more about their supply chain",
            conversation_history=history,
        )

        assert response.answer
        # Verify the user prompt sent to LLM includes conversation history
        haiku_calls = mock_client.call_haiku.call_args_list
        answer_call = haiku_calls[1]
        user_prompt = answer_call.kwargs.get("user_prompt") or answer_call[1].get("user_prompt") or answer_call[0][1]
        assert "CONVERSATION HISTORY:" in user_prompt
        assert "Reynolds Electric" in user_prompt

    def test_no_history_omits_section(self, db_session, sf, mock_client):
        _add_entry(db_session, "Reynolds Electric meeting notes")
        db_session.commit()

        mock_client.call_haiku.side_effect = [
            _ClassifyResponse(complexity="simple"),
            SimpleQueryResponse(
                answer="Answer.",
                source_entry_ids=[],
            ),
        ]

        engine = QueryEngine(mock_client, sf)
        engine.handle_query("Reynolds meeting")

        haiku_calls = mock_client.call_haiku.call_args_list
        answer_call = haiku_calls[1]
        user_prompt = answer_call.kwargs.get("user_prompt") or answer_call[1].get("user_prompt") or answer_call[0][1]
        assert "CONVERSATION HISTORY:" not in user_prompt


class TestBuildUserPrompt:
    """Test the static _build_user_prompt method."""

    def test_basic_prompt_structure(self):
        entry = Entry(
            id=1,
            raw_text="Test text",
            clean_text="Test text",
            source="slack_text",
            entry_type="personal",
            created_at=datetime(2026, 2, 15, tzinfo=timezone.utc),
            updated_at=datetime(2026, 2, 15, tzinfo=timezone.utc),
        )
        prompt = QueryEngine._build_user_prompt("What happened?", [entry], None)
        assert "KNOWLEDGE BASE ENTRIES:" in prompt
        assert "QUESTION:" in prompt
        assert "What happened?" in prompt
        assert "[Entry 1, 2026-02-15, personal]" in prompt
        assert "Test text" in prompt

    def test_no_entries(self):
        prompt = QueryEngine._build_user_prompt("What happened?", [], None)
        assert "(No matching entries found)" in prompt

    def test_with_conversation_history(self):
        history = [
            {"role": "user", "text": "Previous question"},
            {"role": "assistant", "text": "Previous answer"},
        ]
        prompt = QueryEngine._build_user_prompt("Follow up?", [], history)
        assert "CONVERSATION HISTORY:" in prompt
        assert "You: Previous question" in prompt
        assert "Assistant: Previous answer" in prompt

    def test_uses_clean_text_over_raw(self):
        entry = Entry(
            id=1,
            raw_text="raw version",
            clean_text="clean version",
            source="slack_text",
            entry_type="personal",
            created_at=datetime(2026, 2, 15, tzinfo=timezone.utc),
            updated_at=datetime(2026, 2, 15, tzinfo=timezone.utc),
        )
        prompt = QueryEngine._build_user_prompt("Query", [entry], None)
        assert "clean version" in prompt

    def test_falls_back_to_raw_text(self):
        entry = Entry(
            id=2,
            raw_text="raw version",
            clean_text=None,
            source="slack_text",
            entry_type="personal",
            created_at=datetime(2026, 2, 15, tzinfo=timezone.utc),
            updated_at=datetime(2026, 2, 15, tzinfo=timezone.utc),
        )
        prompt = QueryEngine._build_user_prompt("Query", [entry], None)
        assert "raw version" in prompt
