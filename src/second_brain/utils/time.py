"""Timezone utilities for consistent naive-UTC datetime handling.

SQLite does not store timezone info — all datetimes round-trip as naive.
Using timezone-aware datetimes causes TypeError when subtracting a naive
datetime read from the DB from an aware datetime created in Python.

All code should use utc_now() instead of datetime.now(timezone.utc).
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/New_York")


def utc_now() -> datetime:
    """Return the current UTC time as a timezone-naive datetime.

    This is safe for SQLite storage and arithmetic with datetimes
    read back from the database.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(dt: datetime) -> datetime:
    """Convert a naive-UTC datetime to local time (America/New_York)."""
    return dt.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
