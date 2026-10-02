"""
Current market date/time anchored to US/Eastern (the market calendar), independent of the
server timezone. The container runs UTC, which rolls to "tomorrow" at ~19:00-20:00 ET — so
`date.today()` there can be a day ahead of the market date, shifting expiry, turn-of-month,
earnings-proximity, macro-day and batch-date logic. Use `market_date()` for anything the
market calendar governs; leave pure logging on the server clock.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")


def market_now() -> datetime:
    """Timezone-aware 'now' in US/Eastern."""
    return datetime.now(MARKET_TZ)


def market_date() -> date:
    """Current calendar date in US/Eastern (the market date)."""
    return market_now().date()


def to_market_date(dt: datetime) -> date:
    """
    The US/Eastern calendar date for a datetime. Naive datetimes are treated as UTC (the
    container clock). e.g. 2026-06-02 01:00 UTC is 2026-06-01 21:00 ET -> date(2026, 6, 1).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MARKET_TZ).date()
