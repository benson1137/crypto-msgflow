"""Time utilities. The whole system stores UTC-naive timestamps (§2.2)."""
from datetime import UTC, datetime


def utcnow() -> datetime:
    """Current time as UTC-naive datetime (no tzinfo).

    Server local time is UTC+8; storing local time would make staleness
    checks off by 8 hours. Everything stores UTC.
    """
    return datetime.now(UTC).replace(tzinfo=None)
