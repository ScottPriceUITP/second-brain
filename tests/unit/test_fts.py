"""Tests for FTS search utility."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from second_brain.models import Base
from second_brain.models.entry import Entry
from second_brain.utils.fts import _sanitize_fts_query, fts_search
from second_brain.utils.time import utc_now


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    # Create FTS5 virtual table and triggers
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
        conn.commit()
    return eng


@pytest.fixture
def session(engine):
    SessionLocal = sessionmaker(bind=engine)
    sess = SessionLocal()
    yield sess
    sess.close()


def _add_entry(session, clean_text: str) -> Entry:
    entry = Entry(
        raw_text=clean_text,
        clean_text=clean_text,
        source="telegram_text",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(entry)
    session.flush()
    return entry


class TestSanitizeFtsQuery:
    def test_basic_terms(self):
        result = _sanitize_fts_query("hello world")
        assert '"hello"' in result
        assert '"world"' in result

    def test_special_chars_removed(self):
        result = _sanitize_fts_query('hello: "world" (test)')
        assert ":" not in result.replace('"hello"', "").replace('"world"', "").replace('"test"', "")

    def test_empty_query(self):
        assert _sanitize_fts_query("") == ""

    def test_only_special_chars(self):
        assert _sanitize_fts_query("!@#$%") == ""


class TestFtsSearch:
    def test_basic_search(self, session):
        _add_entry(session, "Meeting with Reynolds about the supply chain")
        _add_entry(session, "Python deployment pipeline improvements")
        _add_entry(session, "Reynolds Electric quarterly review")
        session.commit()

        results = fts_search(session, "Reynolds")
        assert len(results) >= 1
        texts = [e.clean_text for e in results]
        assert any("Reynolds" in t for t in texts)

    def test_no_results(self, session):
        _add_entry(session, "Meeting with Reynolds about the supply chain")
        session.commit()

        results = fts_search(session, "nonexistent term xyzzy")
        assert len(results) == 0

    def test_empty_query(self, session):
        _add_entry(session, "Some entry text")
        session.commit()

        results = fts_search(session, "")
        assert len(results) == 0

    def test_limit(self, session):
        for i in range(5):
            _add_entry(session, f"Entry about Python topic {i}")
        session.commit()

        results = fts_search(session, "Python", limit=3)
        assert len(results) <= 3

    def test_exclude_entry_id(self, session):
        e1 = _add_entry(session, "Reynolds Electric supply chain issues")
        e2 = _add_entry(session, "Reynolds Electric quarterly review")
        session.commit()

        results = fts_search(session, "Reynolds", exclude_entry_id=e1.id)
        result_ids = [e.id for e in results]
        assert e1.id not in result_ids
        assert e2.id in result_ids

    def test_multiple_terms_or_logic(self, session):
        _add_entry(session, "Meeting about Python deployment")
        _add_entry(session, "Reynolds Electric supply chain")
        session.commit()

        results = fts_search(session, "Python Reynolds")
        assert len(results) >= 1
