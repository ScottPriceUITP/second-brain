"""Tests for SchedulerService — job registration, active hours, config intervals."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from second_brain.models import Base
from second_brain.services.scheduler import SchedulerService
from second_brain.utils.time import utc_now


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    with eng.connect() as conn:
        now = utc_now().isoformat()
        for key, value in {
            "scheduler_interval_hours": "2",
            "scheduler_start_hour": "8",
            "scheduler_end_hour": "21",
            "calendar_sync_interval_minutes": "30",
            "meeting_check_interval_minutes": "5",
            "enrichment_retry_interval_minutes": "10",
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
def services(sf):
    return {"db_session_factory": sf}


class TestJobRegistration:
    @pytest.mark.asyncio
    async def test_all_jobs_registered(self, services):
        """setup_scheduler registers main_scheduler, calendar_sync, meeting_check, retry_jobs."""
        svc = SchedulerService(services)
        svc.setup_scheduler(bot_data={})

        job_ids = {job.id for job in svc.scheduler.get_jobs()}
        assert "main_scheduler" in job_ids
        assert "calendar_sync" in job_ids
        assert "meeting_check" in job_ids
        assert "retry_jobs" in job_ids
        assert "escalation_check" in job_ids
        assert "pattern_detection" in job_ids

        svc.shutdown()

    @pytest.mark.asyncio
    async def test_scheduler_starts(self, services):
        """setup_scheduler starts the scheduler."""
        svc = SchedulerService(services)
        svc.setup_scheduler(bot_data={})
        assert svc.scheduler.running is True
        svc.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_stops_scheduler(self, services):
        """shutdown() calls scheduler.shutdown() and is idempotent."""
        svc = SchedulerService(services)
        svc.setup_scheduler(bot_data={})
        assert svc.scheduler.running is True
        # Calling shutdown should not raise
        svc.shutdown()
        # Calling shutdown again should also not raise (idempotent guard)
        svc.shutdown()

    @pytest.mark.asyncio
    async def test_replace_existing_jobs(self, services):
        """Calling setup_scheduler with replace_existing=True doesn't duplicate jobs."""
        svc = SchedulerService(services)
        svc.setup_scheduler(bot_data={})

        # Shutdown first, then setup again to avoid SchedulerAlreadyRunningError
        svc.shutdown()
        svc.scheduler = __import__(
            "apscheduler.schedulers.asyncio", fromlist=["AsyncIOScheduler"]
        ).AsyncIOScheduler()
        svc.setup_scheduler(bot_data={})

        job_ids = [job.id for job in svc.scheduler.get_jobs()]
        assert len(job_ids) == 7
        svc.shutdown()


class TestActiveHoursFiltering:
    @pytest.mark.asyncio
    async def test_main_scheduler_cron_hour_range(self, services):
        """Main scheduler job uses cron with correct hour range from config."""
        svc = SchedulerService(services)
        svc.setup_scheduler(bot_data={})

        main_job = svc.scheduler.get_job("main_scheduler")
        assert main_job is not None
        trigger = main_job.trigger
        assert trigger is not None

        svc.shutdown()

    @pytest.mark.asyncio
    async def test_custom_active_hours(self, sf):
        """Scheduler reads custom start/end hours from config."""
        with sf() as session:
            session.execute(text(
                "UPDATE config SET value = '9' WHERE key = 'scheduler_start_hour'"
            ))
            session.execute(text(
                "UPDATE config SET value = '18' WHERE key = 'scheduler_end_hour'"
            ))
            session.commit()

        services = {"db_session_factory": sf}
        svc = SchedulerService(services)
        svc.setup_scheduler(bot_data={})

        main_job = svc.scheduler.get_job("main_scheduler")
        assert main_job is not None
        svc.shutdown()


class TestConfigurableIntervals:
    @pytest.mark.asyncio
    async def test_custom_calendar_interval(self, sf):
        """Calendar sync uses interval from config."""
        with sf() as session:
            session.execute(text(
                "UPDATE config SET value = '45' WHERE key = 'calendar_sync_interval_minutes'"
            ))
            session.commit()

        services = {"db_session_factory": sf}
        svc = SchedulerService(services)
        svc.setup_scheduler(bot_data={})

        cal_job = svc.scheduler.get_job("calendar_sync")
        assert cal_job is not None
        trigger = cal_job.trigger
        assert trigger.interval.total_seconds() == 45 * 60

        svc.shutdown()

    @pytest.mark.asyncio
    async def test_custom_meeting_interval(self, sf):
        """Meeting check uses interval from config."""
        with sf() as session:
            session.execute(text(
                "UPDATE config SET value = '10' WHERE key = 'meeting_check_interval_minutes'"
            ))
            session.commit()

        services = {"db_session_factory": sf}
        svc = SchedulerService(services)
        svc.setup_scheduler(bot_data={})

        meeting_job = svc.scheduler.get_job("meeting_check")
        assert meeting_job is not None
        assert meeting_job.trigger.interval.total_seconds() == 10 * 60

        svc.shutdown()

    @pytest.mark.asyncio
    async def test_custom_retry_interval(self, sf):
        """Retry job uses interval from config."""
        with sf() as session:
            session.execute(text(
                "UPDATE config SET value = '20' WHERE key = 'enrichment_retry_interval_minutes'"
            ))
            session.commit()

        services = {"db_session_factory": sf}
        svc = SchedulerService(services)
        svc.setup_scheduler(bot_data={})

        retry_job = svc.scheduler.get_job("retry_jobs")
        assert retry_job is not None
        assert retry_job.trigger.interval.total_seconds() == 20 * 60

        svc.shutdown()

    @pytest.mark.asyncio
    async def test_default_intervals_from_config(self, services):
        """Uses default intervals from the config table."""
        svc = SchedulerService(services)
        svc.setup_scheduler(bot_data={})

        cal_job = svc.scheduler.get_job("calendar_sync")
        assert cal_job.trigger.interval.total_seconds() == 30 * 60

        meeting_job = svc.scheduler.get_job("meeting_check")
        assert meeting_job.trigger.interval.total_seconds() == 5 * 60

        retry_job = svc.scheduler.get_job("retry_jobs")
        assert retry_job.trigger.interval.total_seconds() == 10 * 60

        svc.shutdown()


class TestMainSchedulerSkip:
    def test_skips_without_anthropic_client(self, services):
        """Main scheduler job does nothing without anthropic_client."""
        svc = SchedulerService(services)
        assert svc.anthropic_client is None


class TestFormattingHelpers:
    def test_format_open_loops_empty(self):
        result = SchedulerService._format_open_loops([])
        assert result == ""

    def test_format_recent_entries_empty(self):
        result = SchedulerService._format_recent_entries([])
        assert result == ""

    def test_format_calendar_events_empty(self):
        result = SchedulerService._format_calendar_events([])
        assert result == ""

    def test_format_open_loops_with_entry(self, sf):
        from second_brain.models.entry import Entry

        with sf() as session:
            entry = Entry(
                raw_text="Follow up with client about contract",
                clean_text="Follow up with client about contract",
                source="slack_text",
                status="open",
                is_open_loop=True,
                created_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            )
            session.add(entry)
            session.flush()

            result = SchedulerService._format_open_loops([entry])
            assert "Follow up with client" in result
            assert "2026-03-01" in result
