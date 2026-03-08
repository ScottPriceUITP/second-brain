"""Tests for NudgeManager — nudge creation, user actions, escalation, snooze."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from second_brain.models import Base
from second_brain.models.entry import Entry
from second_brain.models.nudge import NudgeHistory
from second_brain.services.nudge_manager import NudgeManager
from second_brain.utils.time import utc_now


# SQLite strips timezone info from datetimes, so when check_escalations() does
# utc_now() - nudge.sent_at, it fails (aware - naive).
# We use _utcnow() for test data to match what SQLite round-trips.
def _utcnow():
    return utc_now()


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    with eng.connect() as conn:
        now = utc_now().isoformat()
        for key, value in {
            "nudge_escalation_days": "3",
        }.items():
            conn.execute(text(
                "INSERT INTO config (key, value, updated_at) "
                "VALUES (:key, :value, :now)"
            ), {"key": key, "value": value, "now": now})
        conn.commit()
    return eng


@pytest.fixture
def sf(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def mock_anthropic():
    return MagicMock()


@pytest.fixture
def manager(sf, mock_anthropic):
    return NudgeManager(session_factory=sf, anthropic_client=mock_anthropic)


def _add_entry(sf, **kwargs) -> int:
    """Create an entry and return its ID (avoids detached instance issues)."""
    defaults = {
        "raw_text": "Follow up with client",
        "source": "slack_text",
        "status": "open",
        "is_open_loop": True,
        "created_at": _utcnow(),
        "updated_at": _utcnow(),
    }
    defaults.update(kwargs)
    with sf() as session:
        entry = Entry(**defaults)
        session.add(entry)
        session.commit()
        return entry.id


class TestCreateNudge:
    def test_creates_nudge_history_record(self, manager, sf):
        nudge_id, formatted, keyboard = manager.create_nudge(
            entry_id=None,
            nudge_type="open_loop",
            message="Time to follow up",
        )

        assert nudge_id is not None
        assert isinstance(nudge_id, int)

        # Verify persisted in DB
        with sf() as session:
            db_nudge = session.get(NudgeHistory, nudge_id)
            assert db_nudge is not None
            assert db_nudge.nudge_type == "open_loop"
            assert db_nudge.message_text == "Time to follow up"
            assert db_nudge.escalation_level == 1
            assert db_nudge.sent_at is not None

    def test_creates_nudge_with_entry_id(self, manager, sf):
        entry_id = _add_entry(sf)
        nudge_id, _, _ = manager.create_nudge(
            entry_id=entry_id,
            nudge_type="open_loop",
            message="Follow up needed",
        )
        with sf() as session:
            db_nudge = session.get(NudgeHistory, nudge_id)
            assert db_nudge.entry_id == entry_id

    def test_creates_nudge_with_escalation_level(self, manager, sf):
        nudge_id, _, _ = manager.create_nudge(
            entry_id=None,
            nudge_type="pattern_insight",
            message="Pattern detected",
            escalation_level=2,
        )
        with sf() as session:
            db_nudge = session.get(NudgeHistory, nudge_id)
            assert db_nudge.escalation_level == 2

    def test_returns_formatted_message(self, manager, sf):
        _, formatted, _ = manager.create_nudge(
            entry_id=None,
            nudge_type="open_loop",
            message="Hey, follow up",
            escalation_level=1,
        )
        assert "*Reminder*" in formatted
        assert "Hey, follow up" in formatted

    def test_returns_formatted_message_level_2(self, manager, sf):
        _, formatted, _ = manager.create_nudge(
            entry_id=None,
            nudge_type="open_loop",
            message="Urgent item",
            escalation_level=2,
        )
        assert "*Attention*" in formatted

    def test_returns_formatted_message_level_3(self, manager, sf):
        _, formatted, _ = manager.create_nudge(
            entry_id=None,
            nudge_type="open_loop",
            message="Action needed",
            escalation_level=3,
        )
        assert "*Action Needed*" in formatted

    def test_returns_block_kit_blocks(self, manager, sf):
        nudge_id, _, blocks = manager.create_nudge(
            entry_id=None,
            nudge_type="open_loop",
            message="Test nudge",
        )
        # blocks should be a list of Block Kit block dicts
        assert isinstance(blocks, list)
        assert len(blocks) >= 2  # at least a section + actions block

        # Find the actions block
        actions_block = [b for b in blocks if b.get("type") == "actions"]
        assert len(actions_block) == 1
        elements = actions_block[0]["elements"]
        assert len(elements) == 3

        texts = [el["text"]["text"] for el in elements]
        assert texts == ["Done", "Snooze", "Drop"]

        action_ids = [el["action_id"] for el in elements]
        assert action_ids == ["nudge_done", "nudge_snooze", "nudge_drop"]

        # All buttons should reference the nudge_id in their value
        for el in elements:
            assert el["value"] == str(nudge_id)


class TestHandleNudgeAction:
    def test_done_action(self, manager, sf):
        entry_id = _add_entry(sf)
        nudge_id, _, _ = manager.create_nudge(
            entry_id=entry_id,
            nudge_type="open_loop",
            message="Follow up",
        )
        result = manager.handle_nudge_action(nudge_id, "done")
        assert result == "Marked as done."

        with sf() as session:
            db_nudge = session.get(NudgeHistory, nudge_id)
            assert db_nudge.user_action == "done"
            assert db_nudge.user_action_at is not None
            db_entry = session.get(Entry, entry_id)
            assert db_entry.status == "resolved"
            assert db_entry.is_open_loop is False

    def test_snoozed_action_with_date(self, manager, sf):
        nudge_id, _, _ = manager.create_nudge(
            entry_id=None,
            nudge_type="open_loop",
            message="Snooze test",
        )
        snooze_date = date(2026, 4, 1)
        result = manager.handle_nudge_action(nudge_id, "snoozed", snooze_until=snooze_date)
        assert "Snoozed until 2026-04-01" in result

        with sf() as session:
            db_nudge = session.get(NudgeHistory, nudge_id)
            assert db_nudge.user_action == "snoozed"
            assert db_nudge.snooze_until == snooze_date

    def test_snoozed_action_default_date(self, manager, sf):
        nudge_id, _, _ = manager.create_nudge(
            entry_id=None,
            nudge_type="open_loop",
            message="Snooze default",
        )
        result = manager.handle_nudge_action(nudge_id, "snoozed")
        assert "Snoozed until" in result

        with sf() as session:
            db_nudge = session.get(NudgeHistory, nudge_id)
            assert db_nudge.snooze_until is not None

    def test_dropped_action(self, manager, sf):
        entry_id = _add_entry(sf)
        nudge_id, _, _ = manager.create_nudge(
            entry_id=entry_id,
            nudge_type="open_loop",
            message="Drop test",
        )
        result = manager.handle_nudge_action(nudge_id, "dropped")
        assert result == "Dropped. Won't remind you again."

        with sf() as session:
            db_nudge = session.get(NudgeHistory, nudge_id)
            assert db_nudge.user_action == "dropped"
            db_entry = session.get(Entry, entry_id)
            assert db_entry.status == "archived"
            assert db_entry.is_open_loop is False

    def test_nudge_not_found(self, manager, sf):
        result = manager.handle_nudge_action(9999, "done")
        assert result == "Nudge not found."

    def test_done_no_entry(self, manager, sf):
        nudge_id, _, _ = manager.create_nudge(
            entry_id=None,
            nudge_type="pattern_insight",
            message="Pattern nudge",
        )
        result = manager.handle_nudge_action(nudge_id, "done")
        assert result == "Marked as done."


class TestEscalationLifecycle:
    """Test escalation from level 1 -> 2 -> 3 based on time thresholds.

    All escalation tests patch utc_now in nudge_manager to control time.
    """

    def test_level_1_to_2_after_threshold(self, manager, sf):
        """A level 1 nudge escalates to level 2 after nudge_escalation_days."""
        entry_id = _add_entry(sf)
        now = _utcnow()

        with sf() as session:
            old_nudge = NudgeHistory(
                entry_id=entry_id,
                nudge_type="open_loop",
                message_text="Old nudge",
                escalation_level=1,
                sent_at=now - timedelta(days=4),
            )
            session.add(old_nudge)
            session.commit()

        with patch("second_brain.services.nudge_manager.utc_now", return_value=now):
            escalated = manager.check_escalations()

        assert len(escalated) == 1
        new_nudge_id, formatted, keyboard = escalated[0]
        with sf() as session:
            new_nudge = session.get(NudgeHistory, new_nudge_id)
            assert new_nudge.escalation_level == 2

    def test_level_2_to_3_after_threshold(self, manager, sf):
        """A level 2 nudge escalates to level 3 after nudge_escalation_days."""
        entry_id = _add_entry(sf)
        now = _utcnow()

        with sf() as session:
            old_nudge = NudgeHistory(
                entry_id=entry_id,
                nudge_type="open_loop",
                message_text="Level 2 nudge",
                escalation_level=2,
                sent_at=now - timedelta(days=4),
            )
            session.add(old_nudge)
            session.commit()

        with patch("second_brain.services.nudge_manager.utc_now", return_value=now):
            escalated = manager.check_escalations()

        assert len(escalated) == 1
        new_nudge_id, _, _ = escalated[0]
        with sf() as session:
            new_nudge = session.get(NudgeHistory, new_nudge_id)
            assert new_nudge.escalation_level == 3

    def test_level_3_does_not_escalate_further(self, manager, sf):
        """Level 3 is max; no further escalation."""
        entry_id = _add_entry(sf)

        with sf() as session:
            old_nudge = NudgeHistory(
                entry_id=entry_id,
                nudge_type="open_loop",
                message_text="Level 3 nudge",
                escalation_level=3,
                sent_at=_utcnow() - timedelta(days=10),
            )
            session.add(old_nudge)
            session.commit()

        with patch("second_brain.services.nudge_manager.utc_now", return_value=_utcnow()):
            escalated = manager.check_escalations()

        assert len(escalated) == 0

    def test_no_escalation_before_threshold(self, manager, sf):
        """No escalation if not enough time has passed."""
        entry_id = _add_entry(sf)
        now = _utcnow()

        with sf() as session:
            old_nudge = NudgeHistory(
                entry_id=entry_id,
                nudge_type="open_loop",
                message_text="Recent nudge",
                escalation_level=1,
                sent_at=now - timedelta(days=1),
            )
            session.add(old_nudge)
            session.commit()

        with patch("second_brain.services.nudge_manager.utc_now", return_value=now):
            escalated = manager.check_escalations()

        assert len(escalated) == 0

    def test_already_actioned_not_escalated(self, manager, sf):
        """Nudges with user_action set are not escalated."""
        entry_id = _add_entry(sf)

        with sf() as session:
            old_nudge = NudgeHistory(
                entry_id=entry_id,
                nudge_type="open_loop",
                message_text="Done nudge",
                escalation_level=1,
                sent_at=_utcnow() - timedelta(days=5),
                user_action="done",
                user_action_at=_utcnow(),
            )
            session.add(old_nudge)
            session.commit()

        with patch("second_brain.services.nudge_manager.utc_now", return_value=_utcnow()):
            escalated = manager.check_escalations()

        assert len(escalated) == 0


class TestCheckEscalations:
    def test_marks_old_nudge_as_no_action(self, manager, sf):
        """check_escalations marks old nudge as no_action when escalating."""
        entry_id = _add_entry(sf)
        now = _utcnow()

        with sf() as session:
            old_nudge = NudgeHistory(
                entry_id=entry_id,
                nudge_type="open_loop",
                message_text="Old nudge",
                escalation_level=1,
                sent_at=now - timedelta(days=4),
            )
            session.add(old_nudge)
            session.commit()
            old_id = old_nudge.id

        with patch("second_brain.services.nudge_manager.utc_now", return_value=now):
            manager.check_escalations()

        with sf() as session:
            old = session.get(NudgeHistory, old_id)
            assert old.user_action == "no_action"

    def test_skips_snoozed_not_yet_expired(self, manager, sf):
        """Snoozed nudges that haven't expired are skipped."""
        entry_id = _add_entry(sf)
        now = _utcnow()

        with sf() as session:
            snoozed = NudgeHistory(
                entry_id=entry_id,
                nudge_type="open_loop",
                message_text="Snoozed nudge",
                escalation_level=1,
                sent_at=now - timedelta(days=5),
                snooze_until=date.today() + timedelta(days=2),
            )
            session.add(snoozed)
            session.commit()

        with patch("second_brain.services.nudge_manager.utc_now", return_value=now):
            escalated = manager.check_escalations()

        assert len(escalated) == 0

    def test_no_duplicate_escalation(self, manager, sf):
        """If an escalation already exists at next level, don't create another."""
        entry_id = _add_entry(sf)
        now = _utcnow()

        with sf() as session:
            old_nudge = NudgeHistory(
                entry_id=entry_id,
                nudge_type="open_loop",
                message_text="Old nudge",
                escalation_level=1,
                sent_at=now - timedelta(days=5),
            )
            existing_escalation = NudgeHistory(
                entry_id=entry_id,
                nudge_type="open_loop",
                message_text="Existing level 2",
                escalation_level=2,
                sent_at=now - timedelta(days=1),
            )
            session.add_all([old_nudge, existing_escalation])
            session.commit()

        with patch("second_brain.services.nudge_manager.utc_now", return_value=now):
            escalated = manager.check_escalations()

        assert len(escalated) == 0

    def test_builds_escalation_message_with_entry(self, manager, sf):
        """Escalation message includes entry text snippet."""
        entry_id = _add_entry(sf, clean_text="Call the plumber about the leak")
        now = _utcnow()

        with sf() as session:
            old_nudge = NudgeHistory(
                entry_id=entry_id,
                nudge_type="open_loop",
                message_text="Old nudge",
                escalation_level=1,
                sent_at=now - timedelta(days=4),
            )
            session.add(old_nudge)
            session.commit()

        with patch("second_brain.services.nudge_manager.utc_now", return_value=now):
            escalated = manager.check_escalations()

        assert len(escalated) == 1
        new_nudge_id, _, _ = escalated[0]
        with sf() as session:
            new_nudge = session.get(NudgeHistory, new_nudge_id)
            assert "Call the plumber" in new_nudge.message_text

    def test_builds_escalation_message_level3(self, manager, sf):
        """Level 3 escalation message asks about resolution."""
        entry_id = _add_entry(sf, clean_text="Send invoice to client")
        now = _utcnow()

        with sf() as session:
            old_nudge = NudgeHistory(
                entry_id=entry_id,
                nudge_type="open_loop",
                message_text="Urgent nudge",
                escalation_level=2,
                sent_at=now - timedelta(days=4),
            )
            session.add(old_nudge)
            session.commit()

        with patch("second_brain.services.nudge_manager.utc_now", return_value=now):
            escalated = manager.check_escalations()

        assert len(escalated) == 1
        new_nudge_id, _, _ = escalated[0]
        with sf() as session:
            new_nudge = session.get(NudgeHistory, new_nudge_id)
            assert "resolved or dropped" in new_nudge.message_text


class TestSnooze:
    def test_snooze_sets_snooze_until(self, manager, sf):
        nudge_id, _, _ = manager.create_nudge(
            entry_id=None,
            nudge_type="open_loop",
            message="Test",
        )
        snooze_date = date(2026, 5, 1)
        manager.handle_nudge_action(nudge_id, "snoozed", snooze_until=snooze_date)

        with sf() as session:
            db_nudge = session.get(NudgeHistory, nudge_id)
            assert db_nudge.snooze_until == snooze_date
            assert db_nudge.user_action == "snoozed"

    def test_snoozed_nudge_not_escalated_before_expiry(self, manager, sf):
        """Snoozed nudge shouldn't be picked up by check_escalations before expiry."""
        entry_id = _add_entry(sf)
        now = _utcnow()

        with sf() as session:
            nudge = NudgeHistory(
                entry_id=entry_id,
                nudge_type="open_loop",
                message_text="Snoozed",
                escalation_level=1,
                sent_at=now - timedelta(days=5),
                snooze_until=date.today() + timedelta(days=3),
            )
            session.add(nudge)
            session.commit()

        with patch("second_brain.services.nudge_manager.utc_now", return_value=now):
            escalated = manager.check_escalations()

        assert len(escalated) == 0


class TestGetSnoozedDue:
    def test_finds_expired_snoozes(self, manager, sf):
        with sf() as session:
            expired = NudgeHistory(
                nudge_type="open_loop",
                message_text="Expired snooze",
                escalation_level=1,
                sent_at=_utcnow() - timedelta(days=5),
                user_action="snoozed",
                user_action_at=_utcnow() - timedelta(days=3),
                snooze_until=date.today() - timedelta(days=1),
            )
            session.add(expired)
            session.commit()

        result = manager.get_snoozed_due()
        assert len(result) == 1
        assert result[0]["message_text"] == "Expired snooze"

    def test_ignores_future_snoozes(self, manager, sf):
        with sf() as session:
            future = NudgeHistory(
                nudge_type="open_loop",
                message_text="Future snooze",
                escalation_level=1,
                sent_at=_utcnow(),
                user_action="snoozed",
                user_action_at=_utcnow(),
                snooze_until=date.today() + timedelta(days=5),
            )
            session.add(future)
            session.commit()

        result = manager.get_snoozed_due()
        assert len(result) == 0

    def test_ignores_non_snoozed_nudges(self, manager, sf):
        with sf() as session:
            done = NudgeHistory(
                nudge_type="open_loop",
                message_text="Done nudge",
                escalation_level=1,
                sent_at=_utcnow(),
                user_action="done",
                user_action_at=_utcnow(),
            )
            session.add(done)
            session.commit()

        result = manager.get_snoozed_due()
        assert len(result) == 0

    def test_finds_snooze_due_today(self, manager, sf):
        with sf() as session:
            today_snooze = NudgeHistory(
                nudge_type="open_loop",
                message_text="Due today",
                escalation_level=1,
                sent_at=_utcnow() - timedelta(days=2),
                user_action="snoozed",
                user_action_at=_utcnow() - timedelta(days=2),
                snooze_until=date.today(),
            )
            session.add(today_snooze)
            session.commit()

        result = manager.get_snoozed_due()
        assert len(result) == 1


class TestSetPlatformMessageId:
    def test_sets_platform_message_id(self, manager, sf):
        nudge_id, _, _ = manager.create_nudge(
            entry_id=None,
            nudge_type="open_loop",
            message="Test",
        )
        manager.set_platform_message_id(nudge_id, "1234567890.99999")

        with sf() as session:
            db_nudge = session.get(NudgeHistory, nudge_id)
            assert db_nudge.platform_message_id == "1234567890.99999"

    def test_nonexistent_nudge_no_error(self, manager, sf):
        manager.set_platform_message_id(9999, "1234567890.12345")
