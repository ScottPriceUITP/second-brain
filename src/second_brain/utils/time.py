"""Timezone utilities for consistent naive-UTC datetime handling.

SQLite does not store timezone info — all datetimes round-trip as naive.
Using timezone-aware datetimes causes TypeError when subtracting a naive
datetime read from the DB from an aware datetime created in Python.

All code should use utc_now() instead of datetime.now(timezone.utc).
"""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return the current UTC time as a timezone-naive datetime.

    This is safe for SQLite storage and arithmetic with datetimes
    read back from the database.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
