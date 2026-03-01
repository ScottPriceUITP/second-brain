"""Shared test fixtures for the Second Brain test suite."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from second_brain.models import Base


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
        now = datetime.now(timezone.utc).isoformat()
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
            "transcription_retry_count": "3",
            "enrichment_retry_interval_minutes": "10",
            "transcription_retry_interval_minutes": "10",
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
def mock_whisper_client():
    """Mock WhisperClient that returns configurable transcriptions."""
    from second_brain.services.whisper_client import TranscriptionResult

    client = MagicMock()
    client.transcribe = MagicMock(return_value=TranscriptionResult(
        text="Test transcription",
        confidence=0.95,
        language="en",
    ))
    return client


@pytest.fixture
def mock_update():
    """Mock Telegram Update object."""
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = "Test message"
    update.message.message_id = 12345
    update.message.reply_text = AsyncMock()
    update.message.voice = None
    return update


@pytest.fixture
def mock_voice_update():
    """Mock Telegram Update with voice message."""
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = None
    update.message.message_id = 12346
    update.message.reply_text = AsyncMock()
    voice = MagicMock()
    voice.file_id = "test_file_id"
    voice.file_unique_id = "test_unique_id"
    update.message.voice = voice
    return update


@pytest.fixture
def mock_context(session_factory):
    """Mock Telegram context with bot_data containing services."""
    context = MagicMock()
    context.bot_data = {
        "db_session_factory": session_factory,
    }
    context.user_data = {}
    context.bot = MagicMock()
    context.bot.get_file = AsyncMock()
    return context


def make_entry(session, **kwargs):
    """Factory function to create entries with defaults."""
    from second_brain.models.entry import Entry

    defaults = {
        "raw_text": "Test entry",
        "source": "telegram_text",
        "status": "open",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
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
        created_at=datetime.now(timezone.utc),
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
