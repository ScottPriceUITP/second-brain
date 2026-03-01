"""Tests for QuerySessionManager — active sessions, updates, resets, and timeouts."""

from datetime import datetime, timedelta, timezone

import pytest

from second_brain.services.query_session import QuerySession, QuerySessionManager


class TestQuerySession:
    """Test the QuerySession dataclass."""

    def test_defaults(self):
        qs = QuerySession()
        assert qs.query == ""
        assert qs.response == ""
        assert qs.source_entry_ids == []
        assert qs.last_activity is not None

    def test_custom_values(self):
        qs = QuerySession(
            query="test query",
            response="test response",
            source_entry_ids=[1, 2, 3],
        )
        assert qs.query == "test query"
        assert qs.response == "test response"
        assert qs.source_entry_ids == [1, 2, 3]


class TestQuerySessionManagerIsActive:
    """Test QuerySessionManager.is_active() with various timeouts."""

    def test_no_session_is_not_active(self):
        mgr = QuerySessionManager()
        assert mgr.is_active() is False

    def test_fresh_session_is_active(self):
        mgr = QuerySessionManager()
        mgr.update("q", "r", [1])
        assert mgr.is_active() is True

    def test_default_timeout_10_minutes(self):
        mgr = QuerySessionManager()
        mgr.update("q", "r", [1])
        # Manually backdate the session
        mgr._session.last_activity = datetime.now(timezone.utc) - timedelta(minutes=11)
        assert mgr.is_active() is False

    def test_within_default_timeout(self):
        mgr = QuerySessionManager()
        mgr.update("q", "r", [1])
        mgr._session.last_activity = datetime.now(timezone.utc) - timedelta(minutes=9)
        assert mgr.is_active() is True

    def test_custom_timeout(self):
        mgr = QuerySessionManager()
        mgr.update("q", "r", [1])
        mgr._session.last_activity = datetime.now(timezone.utc) - timedelta(minutes=25)
        # Still active with a 30-minute timeout
        assert mgr.is_active(timeout_minutes=30) is True
        # Not active with a 20-minute timeout
        assert mgr.is_active(timeout_minutes=20) is False

    def test_exactly_at_timeout_boundary(self):
        mgr = QuerySessionManager()
        mgr.update("q", "r", [1])
        # Set to exactly 10 minutes ago — should be expired (< not <=)
        mgr._session.last_activity = datetime.now(timezone.utc) - timedelta(minutes=10)
        assert mgr.is_active() is False


class TestQuerySessionManagerUpdate:
    """Test session update."""

    def test_update_creates_session(self):
        mgr = QuerySessionManager()
        assert mgr.session is None
        mgr.update("What about X?", "X is a topic.", [1, 2])
        assert mgr.session is not None
        assert mgr.session.query == "What about X?"
        assert mgr.session.response == "X is a topic."
        assert mgr.session.source_entry_ids == [1, 2]

    def test_update_replaces_previous_session(self):
        mgr = QuerySessionManager()
        mgr.update("first query", "first response", [1])
        mgr.update("second query", "second response", [2, 3])
        assert mgr.session.query == "second query"
        assert mgr.session.response == "second response"
        assert mgr.session.source_entry_ids == [2, 3]

    def test_update_refreshes_last_activity(self):
        mgr = QuerySessionManager()
        mgr.update("q1", "r1", [1])
        # Backdate the session
        mgr._session.last_activity = datetime.now(timezone.utc) - timedelta(minutes=15)
        assert mgr.is_active() is False

        # Update should refresh the timestamp
        mgr.update("q2", "r2", [2])
        assert mgr.is_active() is True


class TestQuerySessionManagerReset:
    """Test session reset."""

    def test_reset_clears_session(self):
        mgr = QuerySessionManager()
        mgr.update("q", "r", [1])
        assert mgr.session is not None
        mgr.reset()
        assert mgr.session is None

    def test_reset_when_already_empty(self):
        mgr = QuerySessionManager()
        mgr.reset()  # Should not raise
        assert mgr.session is None

    def test_is_active_false_after_reset(self):
        mgr = QuerySessionManager()
        mgr.update("q", "r", [1])
        assert mgr.is_active() is True
        mgr.reset()
        assert mgr.is_active() is False


class TestQuerySessionManagerSessionProperty:
    """Test the session property (returns None when expired)."""

    def test_session_returns_none_when_expired(self):
        mgr = QuerySessionManager()
        mgr.update("q", "r", [1])
        mgr._session.last_activity = datetime.now(timezone.utc) - timedelta(minutes=15)
        # .session property should return None for expired sessions
        assert mgr.session is None

    def test_session_returns_session_when_active(self):
        mgr = QuerySessionManager()
        mgr.update("q", "r", [1])
        assert mgr.session is not None
        assert mgr.session.query == "q"

    def test_session_returns_none_when_no_session(self):
        mgr = QuerySessionManager()
        assert mgr.session is None
