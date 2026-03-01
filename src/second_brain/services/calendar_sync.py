"""Google Calendar sync service — OAuth2, event sync, attendee matching."""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker

from second_brain.config import (
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_OAUTH_REFRESH_TOKEN,
    get_config,
    get_config_bool,
)
from second_brain.models.calendar_event import CalendarEvent
from second_brain.utils.time import utc_now

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


class CalendarSyncService:
    """Syncs Google Calendar events to the local database.

    Handles OAuth2 token management, event fetching, upserting into
    the calendar_events table, and attendee-to-entity matching.
    """

    def __init__(self, session_factory: sessionmaker) -> None:
        self.session_factory = session_factory
        self._credentials = None
        self._service = None
        self._token_refreshed = False

    def setup_oauth(self):
        """Create OAuth2 credentials from environment variables.

        Uses the refresh token from GOOGLE_OAUTH_REFRESH_TOKEN along with
        GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.

        Returns:
            google.oauth2.credentials.Credentials instance, or None if
            required env vars are missing.
        """
        if not all([GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_OAUTH_REFRESH_TOKEN]):
            logger.warning("Google Calendar credentials not configured")
            return None

        from google.oauth2.credentials import Credentials

        credentials = Credentials(
            token=None,
            refresh_token=GOOGLE_OAUTH_REFRESH_TOKEN,
            token_uri=GOOGLE_TOKEN_URI,
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=SCOPES,
        )
        return credentials

    def _get_credentials(self):
        """Get valid credentials, refreshing the token if necessary.

        Returns:
            google.oauth2.credentials.Credentials with a valid access token,
            or None if credentials cannot be established.
        """
        if self._credentials and self._credentials.valid:
            self._token_refreshed = False
            return self._credentials

        if self._credentials is None:
            self._credentials = self.setup_oauth()
            if self._credentials is None:
                return None

        if not self._credentials.valid:
            from google.auth.transport.requests import Request

            self._credentials.refresh(Request())
            self._token_refreshed = True
            logger.info("Google OAuth token refreshed")
        else:
            self._token_refreshed = False

        return self._credentials

    def _get_service(self):
        """Get or create the Google Calendar API service client."""
        credentials = self._get_credentials()
        if not credentials:
            return None

        # Rebuild service if credentials were refreshed
        if self._service and not self._token_refreshed:
            return self._service

        from googleapiclient.discovery import build

        self._service = build("calendar", "v3", credentials=credentials)
        return self._service

    def sync_calendars(self) -> int:
        """Pull next 24 hours of events from configured calendars and upsert locally.

        Returns:
            Number of events synced.
        """
        service = self._get_service()
        if not service:
            logger.info("Calendar sync skipped: no Google Calendar service")
            return 0

        # Get configured calendar IDs from config
        with self.session_factory() as session:
            calendar_ids_json = get_config(session, "google_calendar_ids")

        calendar_ids = ["primary"]  # Default to primary calendar
        if calendar_ids_json:
            try:
                calendar_ids = json.loads(calendar_ids_json)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Invalid google_calendar_ids config, using primary")

        now = utc_now()
        time_min = now.isoformat()
        time_max = (now + timedelta(hours=24)).isoformat()

        total_synced = 0

        for cal_id in calendar_ids:
            try:
                events_result = (
                    service.events()
                    .list(
                        calendarId=cal_id,
                        timeMin=time_min,
                        timeMax=time_max,
                        singleEvents=True,
                        orderBy="startTime",
                    )
                    .execute()
                )
                events = events_result.get("items", [])

                with self.session_factory() as session:
                    for event in events:
                        self._upsert_event(session, event, cal_id)
                    session.commit()

                total_synced += len(events)
                logger.info(
                    "Synced %d events from calendar %s", len(events), cal_id
                )

            except Exception:
                logger.exception("Failed to sync calendar %s", cal_id)

        return total_synced

    async def sync(self, notify_callback=None) -> int:
        """Full sync cycle: sync calendars, resolve attendees, notify if needed.

        This is the main entry point called by the scheduler.

        Args:
            notify_callback: Optional async callable(message: str) for sending
                Telegram notifications (e.g., token refresh notice).

        Returns:
            Number of events synced.
        """
        total_synced = self.sync_calendars()

        # Match attendees to entities
        self._match_attendees_to_entities()

        # Notify on token refresh if configured
        if self._token_refreshed and notify_callback:
            with self.session_factory() as session:
                should_notify = get_config_bool(session, "notify_on_token_refresh")
            if should_notify:
                try:
                    await notify_callback(
                        "Google Calendar OAuth token was automatically refreshed."
                    )
                except Exception:
                    logger.exception("Failed to send token refresh notification")

        logger.info("Calendar sync complete: %d events total", total_synced)
        return total_synced

    def _upsert_event(self, session, event: dict, calendar_id: str) -> None:
        """Insert or update a single calendar event in the database."""
        event_id = event.get("id", "")
        if not event_id:
            return

        # Parse start/end times (can be date or dateTime)
        start = event.get("start", {})
        end = event.get("end", {})
        start_time = self._parse_event_time(start.get("dateTime") or start.get("date"))
        end_time = self._parse_event_time(end.get("dateTime") or end.get("date"))

        if not start_time or not end_time:
            return

        # Extract attendees
        attendees_raw = event.get("attendees", [])
        attendees = [
            {"name": a.get("displayName", ""), "email": a.get("email", "")}
            for a in attendees_raw
        ]

        # Extract video link
        video_link = self._extract_video_link(event)

        # Check if event already exists
        existing = session.get(CalendarEvent, event_id)
        now = utc_now()

        if existing:
            existing.calendar_id = calendar_id
            existing.title = event.get("summary", "(no title)")
            existing.description = event.get("description")
            existing.start_time = start_time
            existing.end_time = end_time
            existing.location = event.get("location")
            existing.video_link = video_link
            existing.attendees = json.dumps(attendees) if attendees else None
            existing.synced_at = now
        else:
            cal_event = CalendarEvent(
                id=event_id,
                calendar_id=calendar_id,
                title=event.get("summary", "(no title)"),
                description=event.get("description"),
                start_time=start_time,
                end_time=end_time,
                location=event.get("location"),
                video_link=video_link,
                attendees=json.dumps(attendees) if attendees else None,
                synced_at=now,
            )
            session.add(cal_event)

    @staticmethod
    def _parse_event_time(time_str: str | None) -> datetime | None:
        """Parse a Google Calendar time string to a datetime."""
        if not time_str:
            return None

        # Full datetime with timezone (e.g., 2026-03-01T10:00:00-05:00)
        try:
            return datetime.fromisoformat(time_str)
        except ValueError:
            pass

        # Date only (all-day events)
        try:
            return datetime.strptime(time_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning("Could not parse event time: %s", time_str)
            return None

    @staticmethod
    def _extract_video_link(event_data: dict) -> str | None:
        """Extract a video conference link from event data.

        Checks conferenceData first (Google Meet), then falls back to
        scanning the description for Zoom/Teams/Meet URLs.
        """
        conference_data = event_data.get("conferenceData", {})
        for entry_point in conference_data.get("entryPoints", []):
            if entry_point.get("entryPointType") == "video":
                return entry_point.get("uri")

        # Fallback: scan description for video URLs
        description = event_data.get("description", "") or ""
        for prefix in [
            "https://meet.google.com/",
            "https://zoom.us/",
            "https://teams.microsoft.com/",
        ]:
            idx = description.find(prefix)
            if idx != -1:
                end = len(description)
                for ch in (" ", "\n", "\r", '"', "'", "<"):
                    pos = description.find(ch, idx)
                    if pos != -1 and pos < end:
                        end = pos
                return description[idx:end]

        return None

    def _match_attendees_to_entities(self) -> None:
        """Match calendar attendees to existing person entities.

        For each attendee in recently synced events:
        - Fuzzy match against existing person entities
        - Auto-create new person entities for unrecognized attendees
        - Infer company entities from email domains
        """
        try:
            from second_brain.services.entity_resolution import EntityResolutionService
        except ImportError:
            logger.debug("Entity resolution not available, skipping attendee matching")
            return

        with self.session_factory() as session:
            # Get events synced in the last hour
            cutoff = utc_now() - timedelta(hours=1)
            recent_events = (
                session.query(CalendarEvent)
                .filter(CalendarEvent.synced_at >= cutoff)
                .all()
            )

            entities_to_resolve = []
            company_entities = []

            for event in recent_events:
                if not event.attendees:
                    continue
                try:
                    attendees = json.loads(event.attendees)
                except (json.JSONDecodeError, TypeError):
                    continue

                for attendee in attendees:
                    name = attendee.get("name", "").strip()
                    email = attendee.get("email", "").strip()

                    if not name and email:
                        # Use email local part as name
                        name = email.split("@")[0].replace(".", " ").title()

                    if name:
                        entities_to_resolve.append({"name": name, "type": "person"})

                    # Infer company from email domain
                    if email and "@" in email:
                        domain = email.split("@")[1].lower()
                        # Skip common email providers
                        if domain not in _COMMON_EMAIL_DOMAINS:
                            company_name = _domain_to_company(domain)
                            if company_name:
                                company_entities.append(
                                    {"name": company_name, "type": "company"}
                                )

            if not entities_to_resolve and not company_entities:
                return

            all_entities = entities_to_resolve + company_entities

            # Deduplicate by (name, type)
            seen = set()
            unique_entities = []
            for e in all_entities:
                key = (e["name"].lower(), e["type"])
                if key not in seen:
                    seen.add(key)
                    unique_entities.append(e)

            if unique_entities:
                resolver = EntityResolutionService(session=session)
                result = resolver.resolve_entities(unique_entities)
                session.commit()
                logger.info(
                    "Attendee matching: %d auto-linked, %d ambiguous, %d new",
                    len(result.auto_linked),
                    len(result.ambiguous),
                    len(result.new_created),
                )

    def get_upcoming_events(self, minutes_ahead: int = 15) -> list[CalendarEvent]:
        """Get events starting within the next N minutes.

        Args:
            minutes_ahead: Look-ahead window in minutes.

        Returns:
            List of CalendarEvent records (detached from session).
        """
        now = utc_now()
        cutoff = now + timedelta(minutes=minutes_ahead)

        with self.session_factory() as session:
            events = (
                session.query(CalendarEvent)
                .filter(
                    CalendarEvent.start_time >= now,
                    CalendarEvent.start_time <= cutoff,
                )
                .order_by(CalendarEvent.start_time)
                .all()
            )
            session.expunge_all()
            return events

    def get_recent_events(self, hours_back: int = 4) -> list[CalendarEvent]:
        """Get events from the recent past for enrichment context.

        Args:
            hours_back: How many hours back to look.

        Returns:
            List of CalendarEvent records (detached from session).
        """
        now = utc_now()
        cutoff = now - timedelta(hours=hours_back)

        with self.session_factory() as session:
            events = (
                session.query(CalendarEvent)
                .filter(
                    CalendarEvent.start_time >= cutoff,
                    CalendarEvent.start_time <= now,
                )
                .order_by(CalendarEvent.start_time.desc())
                .all()
            )
            session.expunge_all()
            return events


# Common email providers that should not be treated as companies
_COMMON_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "protonmail.com",
        "proton.me",
        "fastmail.com",
        "zoho.com",
        "mail.com",
        "yandex.com",
    }
)


def _domain_to_company(domain: str) -> str | None:
    """Convert an email domain to a company name.

    e.g., 'acme.com' -> 'Acme', 'smith-jones.co.uk' -> 'Smith Jones'
    """
    # Remove common TLDs
    parts = domain.split(".")
    if len(parts) < 2:
        return None

    # Take the main domain part (before TLD)
    name = parts[0]
    if not name:
        return None

    # Clean up: replace hyphens/underscores with spaces, title-case
    name = re.sub(r"[-_]", " ", name)
    return name.title()
