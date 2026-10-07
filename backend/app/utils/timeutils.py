"""UTC time helpers. All persisted timestamps in this project are UTC."""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    """Normalize a datetime to UTC. Naive datetimes are assumed UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_datetime(s: str) -> datetime:
    """Parse a datetime string to timezone-aware UTC datetime.

    Supports ISO 8601 formats (with or without timezone).
    Naive datetimes are assumed UTC.
    """
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return to_utc(dt)


def parse_datetime_with_timezone(s: str, timezone_str: str) -> datetime:
    """Parse a datetime string with a specific timezone to timezone-aware UTC datetime.

    Args:
        s: The datetime string to parse.
        timezone_str: The timezone string (e.g., 'America/New_York').

    Returns:
        datetime: The parsed datetime in UTC.
    """
    from dateutil import parser
    from pytz import timezone

    dt = parser.parse(s)
    tz = timezone(timezone_str)
    dt = dt.astimezone(tz)
    return dt.astimezone(timezone.utc)
