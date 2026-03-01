"""Tests for CalendarSyncService — event parsing, upsert, queries, attendee matching."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from second_brain.models import Base
from second_brain.models.calendar_event import CalendarEvent
from second_brain.services.calendar_sync import (
    CalendarSyncService,
    _COMMON_EMAIL_DOMAINS,
    _domain_to_company,
)
from second_brain.utils.time import utc_now


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    with eng.connect() as conn:
        now = utc_now().isoformat()
        for key, value in {
            "entity_match_confidence_threshold": "0.8",
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
def service(sf):
    return CalendarSyncService(session_factory=sf)


def _make_google_event(
    event_id="evt_1",
    summary="Team Standup",
    start_dt=None,
    end_dt=None,
    attendees=None,
    description=None,
    location=None,
    conference_data=None,
):
    """Build a dict resembling a Google Calendar API event."""
    if start_dt is None:
        start_dt = utc_now() + timedelta(hours=1)
    if end_dt is None:
        end_dt = start_dt + timedelta(hours=1)

    event = {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start_dt.isoformat()},
        "end": {"dateTime": end_dt.isoformat()},
    }
    if attendees is not None:
        event["attendees"] = attendees
    if description is not None:
        event["description"] = description
    if location is not None:
        event["location"] = location
    if conference_data is not None:
        event["conferenceData"] = conference_data
    return event


class TestParseEventTime:
    def test_parses_iso_datetime(self, service):
        result = CalendarSyncService._parse_event_time("2026-03-01T10:00:00-05:00")
        assert result is not None
        assert result.year == 2026
        assert result.month == 3
        assert result.day == 1

    def test_parses_date_only(self, service):
        result = CalendarSyncService._parse_event_time("2026-03-01")
        assert result is not None
        assert result.year == 2026
        assert result.month == 3
        assert result.day == 1

    def test_returns_none_for_empty(self, service):
        assert CalendarSyncService._parse_event_time(None) is None
        assert CalendarSyncService._parse_event_time("") is None

    def test_returns_none_for_invalid(self, service):
        result = CalendarSyncService._parse_event_time("not-a-date")
        assert result is None


class TestExtractVideoLink:
    def test_extracts_from_conference_data(self):
        event = {
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/abc-def-ghi"}
                ]
            }
        }
        result = CalendarSyncService._extract_video_link(event)
        assert result == "https://meet.google.com/abc-def-ghi"

    def test_extracts_zoom_from_description(self):
        event = {
            "description": "Join Zoom: https://zoom.us/j/12345678 for the call"
        }
        result = CalendarSyncService._extract_video_link(event)
        assert result == "https://zoom.us/j/12345678"

    def test_extracts_teams_from_description(self):
        event = {
            "description": "Teams link: https://teams.microsoft.com/l/meetup-join/abc\nSee you"
        }
        result = CalendarSyncService._extract_video_link(event)
        assert result == "https://teams.microsoft.com/l/meetup-join/abc"

    def test_returns_none_when_no_video(self):
        event = {"description": "Just a regular meeting"}
        assert CalendarSyncService._extract_video_link(event) is None

    def test_returns_none_for_empty_event(self):
        assert CalendarSyncService._extract_video_link({}) is None

    def test_conference_data_takes_priority(self):
        event = {
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.google.com/xyz"}
                ]
            },
            "description": "Join at https://zoom.us/j/999"
        }
        result = CalendarSyncService._extract_video_link(event)
        assert "meet.google.com" in result


class TestUpsertEvent:
    def test_inserts_new_event(self, service, sf):
        event = _make_google_event(
            event_id="new_1",
            summary="Sprint Planning",
            attendees=[
                {"displayName": "Alice", "email": "alice@example.com"},
                {"displayName": "Bob", "email": "bob@example.com"},
            ],
        )

        with sf() as session:
            service._upsert_event(session, event, "primary")
            session.commit()

        with sf() as session:
            db_event = session.get(CalendarEvent, "new_1")
            assert db_event is not None
            assert db_event.title == "Sprint Planning"
            assert db_event.calendar_id == "primary"
            attendees = json.loads(db_event.attendees)
            assert len(attendees) == 2
            assert attendees[0]["name"] == "Alice"

    def test_updates_existing_event(self, service, sf):
        event_v1 = _make_google_event(event_id="upd_1", summary="Old Title")
        event_v2 = _make_google_event(event_id="upd_1", summary="New Title")

        with sf() as session:
            service._upsert_event(session, event_v1, "primary")
            session.commit()

        with sf() as session:
            service._upsert_event(session, event_v2, "primary")
            session.commit()

        with sf() as session:
            db_event = session.get(CalendarEvent, "upd_1")
            assert db_event.title == "New Title"

    def test_skips_event_without_id(self, service, sf):
        event = _make_google_event()
        event["id"] = ""

        with sf() as session:
            service._upsert_event(session, event, "primary")
            session.commit()

        with sf() as session:
            count = session.query(CalendarEvent).count()
            assert count == 0

    def test_skips_event_without_times(self, service, sf):
        event = {"id": "no_time", "summary": "No Time Event", "start": {}, "end": {}}

        with sf() as session:
            service._upsert_event(session, event, "primary")
            session.commit()

        with sf() as session:
            db_event = session.get(CalendarEvent, "no_time")
            assert db_event is None

    def test_handles_all_day_event(self, service, sf):
        event = {
            "id": "allday_1",
            "summary": "Holiday",
            "start": {"date": "2026-03-15"},
            "end": {"date": "2026-03-16"},
        }

        with sf() as session:
            service._upsert_event(session, event, "primary")
            session.commit()

        with sf() as session:
            db_event = session.get(CalendarEvent, "allday_1")
            assert db_event is not None
            assert db_event.title == "Holiday"

    def test_stores_location_and_video_link(self, service, sf):
        event = _make_google_event(
            event_id="loc_1",
            summary="Office Meeting",
            location="Room 42",
        )
        event["conferenceData"] = {
            "entryPoints": [
                {"entryPointType": "video", "uri": "https://meet.google.com/test"}
            ]
        }

        with sf() as session:
            service._upsert_event(session, event, "primary")
            session.commit()

        with sf() as session:
            db_event = session.get(CalendarEvent, "loc_1")
            assert db_event.location == "Room 42"
            assert db_event.video_link == "https://meet.google.com/test"

    def test_no_title_defaults(self, service, sf):
        event = _make_google_event(event_id="notitle_1")
        del event["summary"]

        with sf() as session:
            service._upsert_event(session, event, "primary")
            session.commit()

        with sf() as session:
            db_event = session.get(CalendarEvent, "notitle_1")
            assert db_event.title == "(no title)"


class TestGetUpcomingEvents:
    def test_returns_events_within_window(self, service, sf):
        now = utc_now()
        with sf() as session:
            soon = CalendarEvent(
                id="soon_1",
                calendar_id="primary",
                title="Upcoming",
                start_time=now + timedelta(minutes=5),
                end_time=now + timedelta(minutes=35),
                synced_at=now,
            )
            far = CalendarEvent(
                id="far_1",
                calendar_id="primary",
                title="Far Away",
                start_time=now + timedelta(hours=2),
                end_time=now + timedelta(hours=3),
                synced_at=now,
            )
            past = CalendarEvent(
                id="past_1",
                calendar_id="primary",
                title="Already Passed",
                start_time=now - timedelta(hours=1),
                end_time=now - timedelta(minutes=30),
                synced_at=now,
            )
            session.add_all([soon, far, past])
            session.commit()

        events = service.get_upcoming_events(minutes_ahead=15)
        assert len(events) == 1
        assert events[0].title == "Upcoming"

    def test_returns_empty_when_no_upcoming(self, service, sf):
        events = service.get_upcoming_events(minutes_ahead=15)
        assert events == []

    def test_custom_minutes_ahead(self, service, sf):
        now = utc_now()
        with sf() as session:
            event = CalendarEvent(
                id="custom_1",
                calendar_id="primary",
                title="Custom Window",
                start_time=now + timedelta(minutes=25),
                end_time=now + timedelta(minutes=55),
                synced_at=now,
            )
            session.add(event)
            session.commit()

        # Not in 15-minute window
        events = service.get_upcoming_events(minutes_ahead=15)
        assert len(events) == 0

        # In 30-minute window
        events = service.get_upcoming_events(minutes_ahead=30)
        assert len(events) == 1


class TestGetRecentEvents:
    def test_returns_recent_events(self, service, sf):
        now = utc_now()
        with sf() as session:
            recent = CalendarEvent(
                id="recent_1",
                calendar_id="primary",
                title="Just Ended",
                start_time=now - timedelta(hours=2),
                end_time=now - timedelta(hours=1),
                synced_at=now,
            )
            old = CalendarEvent(
                id="old_1",
                calendar_id="primary",
                title="Long Ago",
                start_time=now - timedelta(hours=10),
                end_time=now - timedelta(hours=9),
                synced_at=now,
            )
            session.add_all([recent, old])
            session.commit()

        events = service.get_recent_events(hours_back=4)
        assert len(events) == 1
        assert events[0].title == "Just Ended"

    def test_returns_empty_when_no_recent(self, service, sf):
        events = service.get_recent_events(hours_back=4)
        assert events == []

    def test_custom_hours_back(self, service, sf):
        now = utc_now()
        with sf() as session:
            event = CalendarEvent(
                id="hours_1",
                calendar_id="primary",
                title="Semi-Recent",
                start_time=now - timedelta(hours=6),
                end_time=now - timedelta(hours=5),
                synced_at=now,
            )
            session.add(event)
            session.commit()

        events = service.get_recent_events(hours_back=4)
        assert len(events) == 0

        events = service.get_recent_events(hours_back=8)
        assert len(events) == 1


class TestAttendeeEntityMatching:
    @patch("second_brain.services.entity_resolution.EntityResolutionService")
    def test_matches_attendees_to_entities(self, MockResolver, service, sf):
        """Attendees from recently synced events are resolved as entities."""
        now = utc_now()
        with sf() as session:
            event = CalendarEvent(
                id="match_1",
                calendar_id="primary",
                title="Meeting",
                start_time=now + timedelta(hours=1),
                end_time=now + timedelta(hours=2),
                attendees=json.dumps([
                    {"name": "Alice Smith", "email": "alice@acme.com"},
                    {"name": "Bob Jones", "email": "bob@gmail.com"},
                ]),
                synced_at=now,
            )
            session.add(event)
            session.commit()

        mock_resolver_instance = MagicMock()
        mock_resolver_instance.resolve_entities.return_value = MagicMock(
            auto_linked=[], ambiguous=[], new_created=[]
        )
        MockResolver.return_value = mock_resolver_instance

        service._match_attendees_to_entities()

        mock_resolver_instance.resolve_entities.assert_called_once()
        call_args = mock_resolver_instance.resolve_entities.call_args[0][0]

        names = [e["name"] for e in call_args]
        types = [e["type"] for e in call_args]

        # Should have person entities for both attendees
        assert "Alice Smith" in names
        assert "Bob Jones" in names
        assert "person" in types

        # Should have company entity for acme.com but NOT gmail.com
        assert "Acme" in names
        assert "company" in [e["type"] for e in call_args if e["name"] == "Acme"]

    @patch("second_brain.services.entity_resolution.EntityResolutionService")
    def test_skips_common_email_domains(self, MockResolver, service, sf):
        """Gmail, Yahoo etc. should not create company entities."""
        now = utc_now()
        with sf() as session:
            event = CalendarEvent(
                id="common_1",
                calendar_id="primary",
                title="Chat",
                start_time=now + timedelta(hours=1),
                end_time=now + timedelta(hours=2),
                attendees=json.dumps([
                    {"name": "User One", "email": "user1@gmail.com"},
                    {"name": "User Two", "email": "user2@yahoo.com"},
                ]),
                synced_at=now,
            )
            session.add(event)
            session.commit()

        mock_resolver_instance = MagicMock()
        mock_resolver_instance.resolve_entities.return_value = MagicMock(
            auto_linked=[], ambiguous=[], new_created=[]
        )
        MockResolver.return_value = mock_resolver_instance

        service._match_attendees_to_entities()

        call_args = mock_resolver_instance.resolve_entities.call_args[0][0]
        company_entities = [e for e in call_args if e["type"] == "company"]
        assert len(company_entities) == 0

    @patch("second_brain.services.entity_resolution.EntityResolutionService")
    def test_derives_name_from_email_when_missing(self, MockResolver, service, sf):
        """When attendee has no name, derive from email local part."""
        now = utc_now()
        with sf() as session:
            event = CalendarEvent(
                id="noname_1",
                calendar_id="primary",
                title="Meeting",
                start_time=now + timedelta(hours=1),
                end_time=now + timedelta(hours=2),
                attendees=json.dumps([
                    {"name": "", "email": "john.doe@example.com"},
                ]),
                synced_at=now,
            )
            session.add(event)
            session.commit()

        mock_resolver_instance = MagicMock()
        mock_resolver_instance.resolve_entities.return_value = MagicMock(
            auto_linked=[], ambiguous=[], new_created=[]
        )
        MockResolver.return_value = mock_resolver_instance

        service._match_attendees_to_entities()

        call_args = mock_resolver_instance.resolve_entities.call_args[0][0]
        person_entities = [e for e in call_args if e["type"] == "person"]
        assert any("John Doe" in e["name"] for e in person_entities)

    def test_skips_when_no_recent_events(self, service, sf):
        """No error when there are no recently synced events."""
        # Should not raise
        service._match_attendees_to_entities()

    @patch("second_brain.services.entity_resolution.EntityResolutionService")
    def test_deduplicates_entities(self, MockResolver, service, sf):
        """Duplicate attendees across events are deduplicated."""
        now = utc_now()
        with sf() as session:
            event1 = CalendarEvent(
                id="dedup_1",
                calendar_id="primary",
                title="Meeting 1",
                start_time=now + timedelta(hours=1),
                end_time=now + timedelta(hours=2),
                attendees=json.dumps([
                    {"name": "Alice", "email": "alice@acme.com"},
                ]),
                synced_at=now,
            )
            event2 = CalendarEvent(
                id="dedup_2",
                calendar_id="primary",
                title="Meeting 2",
                start_time=now + timedelta(hours=3),
                end_time=now + timedelta(hours=4),
                attendees=json.dumps([
                    {"name": "Alice", "email": "alice@acme.com"},
                ]),
                synced_at=now,
            )
            session.add_all([event1, event2])
            session.commit()

        mock_resolver_instance = MagicMock()
        mock_resolver_instance.resolve_entities.return_value = MagicMock(
            auto_linked=[], ambiguous=[], new_created=[]
        )
        MockResolver.return_value = mock_resolver_instance

        service._match_attendees_to_entities()

        call_args = mock_resolver_instance.resolve_entities.call_args[0][0]
        # Should deduplicate Alice person + Acme company
        alice_entries = [e for e in call_args if e["name"].lower() == "alice"]
        assert len(alice_entries) == 1
        acme_entries = [e for e in call_args if e["name"].lower() == "acme"]
        assert len(acme_entries) == 1


class TestSyncCalendars:
    @patch.object(CalendarSyncService, "_get_service")
    def test_sync_returns_zero_without_service(self, mock_get_svc, service):
        """sync_calendars returns 0 when no Google service is available."""
        mock_get_svc.return_value = None
        result = service.sync_calendars()
        assert result == 0

    @patch.object(CalendarSyncService, "_get_service")
    def test_sync_fetches_and_upserts_events(self, mock_get_svc, service, sf):
        """sync_calendars fetches events from Google API and upserts them."""
        now = utc_now()
        mock_events = {
            "items": [
                _make_google_event(
                    event_id="sync_1",
                    summary="Synced Event",
                    start_dt=now + timedelta(hours=2),
                ),
            ]
        }

        mock_service = MagicMock()
        mock_service.events.return_value.list.return_value.execute.return_value = mock_events
        mock_get_svc.return_value = mock_service

        # Need config for calendar IDs
        with sf() as session:
            session.execute(text(
                "INSERT INTO config (key, value, updated_at) VALUES "
                "('google_calendar_ids', :val, :now)"
            ), {"val": '["primary"]', "now": now.isoformat()})
            session.commit()

        result = service.sync_calendars()
        assert result == 1

        with sf() as session:
            db_event = session.get(CalendarEvent, "sync_1")
            assert db_event is not None
            assert db_event.title == "Synced Event"


class TestDomainToCompany:
    def test_simple_domain(self):
        assert _domain_to_company("acme.com") == "Acme"

    def test_hyphenated_domain(self):
        assert _domain_to_company("smith-jones.co.uk") == "Smith Jones"

    def test_underscored_domain(self):
        assert _domain_to_company("my_company.com") == "My Company"

    def test_single_part_returns_none(self):
        assert _domain_to_company("localhost") is None

    def test_empty_name_returns_none(self):
        assert _domain_to_company(".com") is None


class TestCommonEmailDomains:
    def test_gmail_is_common(self):
        assert "gmail.com" in _COMMON_EMAIL_DOMAINS

    def test_protonmail_is_common(self):
        assert "protonmail.com" in _COMMON_EMAIL_DOMAINS

    def test_custom_domain_not_common(self):
        assert "acme.com" not in _COMMON_EMAIL_DOMAINS
