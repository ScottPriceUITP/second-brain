"""Tests for connection scoring service."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from second_brain.models import Base
from second_brain.models.entry import Entry
from second_brain.models.entity import Entity, entry_entities
from second_brain.models.relation import EntryRelation
from second_brain.services.connection_scoring import (
    ConnectionScore,
    ConnectionScoringResponse,
    ConnectionScoringService,
    ScoredConnection,
)
from second_brain.utils.time import utc_now


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    with eng.connect() as conn:
        # Create FTS5 virtual table and triggers
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
        # Seed config
        now = utc_now().isoformat()
        conn.execute(text(
            "INSERT INTO config (key, value, updated_at) VALUES "
            "('connection_score_threshold', '4', :now)"
        ), {"now": now})
        conn.execute(text(
            "INSERT INTO config (key, value, updated_at) VALUES "
            "('connection_min_count', '2', :now)"
        ), {"now": now})
        conn.commit()
    return eng


@pytest.fixture
def session(engine):
    SessionLocal = sessionmaker(bind=engine)
    sess = SessionLocal()
    yield sess
    sess.close()


@pytest.fixture
def mock_client():
    return MagicMock()


def _add_entry(session, clean_text: str) -> Entry:
    entry = Entry(
        raw_text=clean_text,
        clean_text=clean_text,
        source="slack_text",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(entry)
    session.flush()
    return entry


def _add_entity(session, name: str, entity_type: str) -> Entity:
    entity = Entity(
        name=name,
        type=entity_type,
        created_at=utc_now(),
    )
    session.add(entity)
    session.flush()
    return entity


class TestConnectionScoringService:
    def test_no_candidates_returns_empty(self, session, mock_client):
        service = ConnectionScoringService(mock_client, session)
        entry = _add_entry(session, "Unique entry about nothing related")
        session.commit()

        result = service.score_connections(entry)
        assert result == []
        mock_client.call_haiku.assert_not_called()

    def test_scores_connections_and_stores_relations(self, session, mock_client):
        # Create existing entries that FTS will find (all must match FTS query)
        c1 = _add_entry(session, "Reynolds Electric supply chain delay report")
        c2 = _add_entry(session, "Reynolds Electric quarterly financials review")
        c3 = _add_entry(session, "Reynolds meeting notes from last chain update")
        session.commit()

        # New entry to score
        new_entry = _add_entry(session, "Reynolds Electric supply chain update meeting")
        session.commit()

        # Mock Haiku response
        mock_client.call_haiku.return_value = ConnectionScoringResponse(
            connections=[
                ConnectionScore(candidate_id=c1.id, score=5, relation_type="follow_up_of"),
                ConnectionScore(candidate_id=c2.id, score=4, relation_type="related"),
                ConnectionScore(candidate_id=c3.id, score=1, relation_type="related"),
            ]
        )

        service = ConnectionScoringService(mock_client, session)
        result = service.score_connections(new_entry)

        # Should return strong connections (score >= 4) since we have 2+ of them
        assert len(result) == 2
        assert result[0].score == 5
        assert result[1].score == 4

        # Check that relations were stored in DB
        relations = session.query(EntryRelation).filter(
            EntryRelation.from_entry_id == new_entry.id
        ).all()
        assert len(relations) == 3  # All scores stored

    def test_below_min_count_returns_empty(self, session, mock_client):
        c1 = _add_entry(session, "Reynolds Electric supply chain delay")
        c2 = _add_entry(session, "Reynolds Electric quarterly pipeline update")
        session.commit()

        new_entry = _add_entry(session, "Reynolds Electric discussion notes")
        session.commit()

        # Only 1 strong connection (below min_count of 2)
        mock_client.call_haiku.return_value = ConnectionScoringResponse(
            connections=[
                ConnectionScore(candidate_id=c1.id, score=5, relation_type="related"),
                ConnectionScore(candidate_id=c2.id, score=2, relation_type="related"),
            ]
        )

        service = ConnectionScoringService(mock_client, session)
        result = service.score_connections(new_entry)

        # Below min_count so returns empty
        assert result == []

        # But relations are still stored
        relations = session.query(EntryRelation).filter(
            EntryRelation.from_entry_id == new_entry.id
        ).all()
        assert len(relations) == 2

    def test_uses_entity_names_in_fts_query(self, session, mock_client):
        c1 = _add_entry(session, "Reynolds Electric supply chain meeting")
        session.commit()

        new_entry = _add_entry(session, "Short note")
        entity = _add_entity(session, "Reynolds Electric", "company")
        session.execute(
            entry_entities.insert().values(entry_id=new_entry.id, entity_id=entity.id)
        )
        session.commit()

        mock_client.call_haiku.return_value = ConnectionScoringResponse(
            connections=[
                ConnectionScore(candidate_id=c1.id, score=4, relation_type="related"),
            ]
        )

        service = ConnectionScoringService(mock_client, session)
        # Ensure entities are loaded
        session.refresh(new_entry)
        service.score_connections(new_entry)

        # Haiku should have been called because entity name provided FTS terms
        mock_client.call_haiku.assert_called_once()

    def test_unknown_candidate_id_ignored(self, session, mock_client):
        c1 = _add_entry(session, "Reynolds Electric supply chain")
        session.commit()

        new_entry = _add_entry(session, "Reynolds Electric update")
        session.commit()

        mock_client.call_haiku.return_value = ConnectionScoringResponse(
            connections=[
                ConnectionScore(candidate_id=c1.id, score=4, relation_type="related"),
                ConnectionScore(candidate_id=9999, score=5, relation_type="related"),
            ]
        )

        service = ConnectionScoringService(mock_client, session)
        service.score_connections(new_entry)

        # Only 1 relation stored (unknown ID ignored)
        relations = session.query(EntryRelation).filter(
            EntryRelation.from_entry_id == new_entry.id
        ).all()
        assert len(relations) == 1

    def test_empty_clean_text_no_entities(self, session, mock_client):
        service = ConnectionScoringService(mock_client, session)
        entry = Entry(
            raw_text="hi",
            clean_text="",
            source="slack_text",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(entry)
        session.flush()
        session.commit()

        result = service.score_connections(entry)
        assert result == []


class TestConnectionScoringPrompt:
    def test_build_scoring_user_prompt(self):
        from second_brain.prompts.connection_scoring import build_scoring_user_prompt

        result = build_scoring_user_prompt(
            "New entry about Reynolds",
            [
                {"id": 1, "clean_text": "Reynolds Electric meeting"},
                {"id": 2, "clean_text": "Python deployment"},
            ],
        )
        assert "NEW ENTRY:" in result
        assert "Reynolds Electric meeting" in result
        assert "[ID: 1]" in result
        assert "[ID: 2]" in result
