"""Shared test fixtures for the Second Brain test suite."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from second_brain.models import Base
from second_brain.utils.time import utc_now


@pytest.fixture
def engine():
    """Create an in-memory SQLite database with all tables and FTS."""
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
        # Seed config defaults
        now = utc_now().isoformat()
        for key, value in {
            "connection_score_threshold": "4",
            "connection_min_count": "2",
            "entity_match_confidence_threshold": "0.8",
            "scheduler_interval_hours": "2",
            "scheduler_start_hour": "8",
            "scheduler_end_hour": "21",
            "query_session_timeout_minutes": "10",
            "query_max_entries": "30",
            "nudge_escalation_days": "3",
            "enrichment_retry_count": "3",
            "enrichment_retry_interval_minutes": "10",
        }.items():
            conn.execute(text(
                "INSERT INTO config (key, value, updated_at) "
                "VALUES (:key, :value, :now)"
            ), {"key": key, "value": value, "now": now})
        conn.commit()
    return eng


@pytest.fixture
def session(engine):
    """Create a database session."""
    SessionLocal = sessionmaker(bind=engine)
    sess = SessionLocal()
    yield sess
    sess.close()


@pytest.fixture
def session_factory(engine):
    """Create a sessionmaker for services that require session_factory."""
    return sessionmaker(bind=engine)


@pytest.fixture
def mock_anthropic_client():
    """Mock AnthropicClient that returns configurable responses."""
    client = MagicMock()
    client.call_haiku = MagicMock()
    client.call_sonnet = MagicMock()
    return client


@pytest.fixture
def slack_event():
    """Mock Slack message event dict."""
    return {
        "type": "message",
        "text": "Test message",
        "channel": "C123456",
        "ts": "1234567890.123456",
        "user": "U123456",
    }


@pytest.fixture
def mock_say():
    """Mock Slack say() function."""
    return AsyncMock()


@pytest.fixture
def mock_ack():
    """Mock Slack ack() function."""
    return AsyncMock()


@pytest.fixture
def mock_client():
    """Mock Slack WebClient."""
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "1234567890.789012"}
    client.chat_update.return_value = {"ok": True}
    client.views_open.return_value = {"ok": True}
    return client


@pytest.fixture
def slack_context(session_factory):
    """Slack Bolt context dict with services injected."""
    return {
        "services": {
            "db_session_factory": session_factory,
        },
    }


def make_entry(session, **kwargs):
    """Factory function to create entries with defaults."""
    from second_brain.models.entry import Entry

    defaults = {
        "raw_text": "Test entry",
        "source": "slack_text",
        "status": "open",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    defaults.update(kwargs)
    entry = Entry(**defaults)
    session.add(entry)
    session.flush()
    return entry


def make_entity(session, name="Test Entity", entity_type="person"):
    """Factory function to create entities."""
    from second_brain.models.entity import Entity

    entity = Entity(
        name=name,
        type=entity_type,
        created_at=utc_now(),
    )
    session.add(entity)
    session.flush()
    return entity


def make_tag(session, name="test-tag"):
    """Factory function to create tags."""
    from second_brain.models.tag import Tag

    tag = Tag(name=name)
    session.add(tag)
    session.flush()
    return tag
