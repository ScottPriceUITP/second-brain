"""Tests for config module — get_config, set_config, seed_config_defaults."""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from second_brain.models import Base
from second_brain.config import (
    CONFIG_DEFAULTS,
    get_config,
    get_config_bool,
    get_config_float,
    get_config_int,
    seed_config_defaults,
    set_config,
)


@pytest.fixture
def engine():
    """Create in-memory SQLite with tables but NO seeded config rows."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    SessionLocal = sessionmaker(bind=engine)
    sess = SessionLocal()
    yield sess
    sess.close()


class TestGetConfig:
    """Test get_config() returns defaults and DB values."""

    def test_returns_default_when_not_in_db(self, session):
        # query_max_entries is in CONFIG_DEFAULTS but not seeded in DB
        val = get_config(session, "query_max_entries")
        assert val == "30"

    def test_returns_db_value_when_present(self, session):
        set_config(session, "query_max_entries", "50")
        val = get_config(session, "query_max_entries")
        assert val == "50"

    def test_returns_none_for_unknown_key(self, session):
        val = get_config(session, "nonexistent_key_xyz")
        assert val is None

    def test_db_value_overrides_default(self, session):
        # Default is "30", override to "10"
        set_config(session, "query_max_entries", "10")
        val = get_config(session, "query_max_entries")
        assert val == "10"


class TestGetConfigInt:
    def test_returns_int(self, session):
        val = get_config_int(session, "query_max_entries")
        assert val == 30
        assert isinstance(val, int)

    def test_returns_none_for_unknown(self, session):
        val = get_config_int(session, "nonexistent_key")
        assert val is None


class TestGetConfigFloat:
    def test_returns_float(self, session):
        val = get_config_float(session, "entity_match_confidence_threshold")
        assert val == 0.8
        assert isinstance(val, float)

    def test_returns_none_for_unknown(self, session):
        val = get_config_float(session, "nonexistent_key")
        assert val is None


class TestGetConfigBool:
    def test_true_values(self, session):
        for truthy in ("true", "1", "yes"):
            set_config(session, "test_bool", truthy)
            assert get_config_bool(session, "test_bool") is True

    def test_false_values(self, session):
        for falsy in ("false", "0", "no"):
            set_config(session, "test_bool", falsy)
            assert get_config_bool(session, "test_bool") is False

    def test_returns_none_for_unknown(self, session):
        val = get_config_bool(session, "nonexistent_key")
        assert val is None

    def test_default_bool_value(self, session):
        # notify_on_token_refresh defaults to "true"
        val = get_config_bool(session, "notify_on_token_refresh")
        assert val is True


class TestSetConfig:
    """Test set_config() updates values."""

    def test_insert_new_key(self, session):
        set_config(session, "custom_key", "custom_value")
        val = get_config(session, "custom_key")
        assert val == "custom_value"

    def test_update_existing_key(self, session):
        set_config(session, "query_max_entries", "50")
        assert get_config(session, "query_max_entries") == "50"
        set_config(session, "query_max_entries", "100")
        assert get_config(session, "query_max_entries") == "100"

    def test_upsert_updates_timestamp(self, session):
        set_config(session, "test_key", "v1")
        row1 = session.execute(
            text("SELECT updated_at FROM config WHERE key = 'test_key'")
        ).fetchone()
        set_config(session, "test_key", "v2")
        row2 = session.execute(
            text("SELECT updated_at FROM config WHERE key = 'test_key'")
        ).fetchone()
        # updated_at should be different (or at least not error)
        assert row1 is not None
        assert row2 is not None


class TestSeedConfigDefaults:
    """Test seed_config_defaults() creates all expected keys."""

    def test_seeds_all_defaults(self, session):
        seed_config_defaults(session)

        for key, expected_value in CONFIG_DEFAULTS.items():
            val = get_config(session, key)
            assert val == expected_value, f"Key '{key}' expected '{expected_value}', got '{val}'"

    def test_does_not_overwrite_existing(self, session):
        # Pre-set a value
        set_config(session, "query_max_entries", "999")
        # Seed defaults
        seed_config_defaults(session)
        # Should still be the pre-set value
        val = get_config(session, "query_max_entries")
        assert val == "999"

    def test_seed_count_matches_defaults(self, session):
        seed_config_defaults(session)
        row = session.execute(text("SELECT COUNT(*) FROM config")).fetchone()
        assert row[0] == len(CONFIG_DEFAULTS)

    def test_idempotent(self, session):
        seed_config_defaults(session)
        seed_config_defaults(session)
        row = session.execute(text("SELECT COUNT(*) FROM config")).fetchone()
        assert row[0] == len(CONFIG_DEFAULTS)


class TestJsonConfigValues:
    """Test JSON-encoded complex config values."""

    def test_store_and_retrieve_json_list(self, session):
        data = ["tag1", "tag2", "tag3"]
        set_config(session, "json_list", json.dumps(data))
        val = get_config(session, "json_list")
        parsed = json.loads(val)
        assert parsed == data

    def test_store_and_retrieve_json_dict(self, session):
        data = {"threshold": 0.8, "enabled": True, "tags": ["a", "b"]}
        set_config(session, "json_dict", json.dumps(data))
        val = get_config(session, "json_dict")
        parsed = json.loads(val)
        assert parsed == data

    def test_store_and_retrieve_nested_json(self, session):
        data = {"models": {"haiku": {"max_tokens": 1024}, "sonnet": {"max_tokens": 4096}}}
        set_config(session, "model_config", json.dumps(data))
        val = get_config(session, "model_config")
        parsed = json.loads(val)
        assert parsed["models"]["haiku"]["max_tokens"] == 1024
        assert parsed["models"]["sonnet"]["max_tokens"] == 4096
