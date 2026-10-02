"""
Earnings calendar: per-ticker upcoming earnings date lookup via yfinance.
Results are cached in memory (24h TTL) to avoid hammering Yahoo Finance.
"""
import logging
import time
from datetime import date
from app.core.market_time import market_date
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[Optional[date], float]] = {}
_TTL_SECONDS = 86_400  # 24 hours


def _fetch_next_earnings(symbol: str) -> Optional[date]:
    """Fetch the next upcoming earnings date via market_data routing layer."""
    from app.services.market_data import fetch_next_earnings
    return fetch_next_earnings(symbol)


def get_next_earnings(symbol: str) -> Optional[date]:
    """Return the next upcoming earnings date for the given yfinance symbol. Cached 24h."""
    now = time.monotonic()
    cached = _CACHE.get(symbol)
    if cached is not None:
        earnings_date, fetched_at = cached
        if now - fetched_at < _TTL_SECONDS:
            return earnings_date

    earnings_date = _fetch_next_earnings(symbol)
    _CACHE[symbol] = (earnings_date, now)
    logger.debug("[EARNINGS] %s: next_earnings=%s", symbol, earnings_date)
    return earnings_date


def is_near_earnings(symbol: str, window_days: int = 2) -> tuple[bool, Optional[int]]:
    """
    Check if earnings falls within ±window_days of today.

    Returns:
        (is_near, days_to_earnings)
        days_to_earnings is positive = earnings is upcoming,
        0 = earnings today,
        negative = earnings was recent (past N days).
        None if no earnings date found.
    """
    next_date = get_next_earnings(symbol)
    if next_date is None:
        return False, None

    delta = (next_date - market_date()).days
    if abs(delta) <= window_days:
        return True, delta
    return False, delta if delta >= 0 else None
