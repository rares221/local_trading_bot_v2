"""
Macro calendar overlay: high-impact event dates (FOMC, CPI, NFP) to reduce
recommendation confidence on and around those days.

Dates are fetched from public sources (Fed calendar, press releases) and cached.
Falls back to static dates if fetching fails.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Points subtracted from each candidate confidence when `is_high_impact_day`
MACRO_DAY_CONFIDENCE_PENALTY = 10

# Static fallback dates (used when fetcher fails or on first run before cache exists)
# FOMC: statement release day (second day of scheduled two-day meetings)
# CPI: ~2nd week of month (approximate)
# NFP: first Friday of month
_FALLBACK_DATES: set[tuple[int, int, int]] = {
    # 2024 FOMC
    (2024, 1, 31), (2024, 3, 20), (2024, 5, 1), (2024, 6, 12),
    (2024, 7, 31), (2024, 9, 18), (2024, 11, 7), (2024, 12, 18),
    # 2025 FOMC
    (2025, 1, 29), (2025, 3, 19), (2025, 5, 7), (2025, 6, 18),
    (2025, 7, 30), (2025, 9, 17), (2025, 11, 5), (2025, 12, 17),
    # 2026 FOMC
    (2026, 1, 28), (2026, 3, 18), (2026, 4, 29), (2026, 6, 17),
    (2026, 7, 29), (2026, 9, 16), (2026, 10, 28), (2026, 12, 9),
    # 2027 FOMC
    (2027, 1, 27), (2027, 3, 17), (2027, 4, 28), (2027, 6, 9),
    (2027, 7, 28), (2027, 9, 15), (2027, 10, 27), (2027, 12, 8),
    # CPI 2024
    (2024, 1, 11), (2024, 2, 13), (2024, 3, 12), (2024, 4, 10), (2024, 5, 15),
    (2024, 6, 12), (2024, 7, 11), (2024, 8, 14), (2024, 9, 11), (2024, 10, 10),
    (2024, 11, 13), (2024, 12, 11),
    # CPI 2025
    (2025, 1, 15), (2025, 2, 12), (2025, 3, 12), (2025, 4, 10), (2025, 5, 14),
    (2025, 6, 11), (2025, 7, 10), (2025, 8, 13), (2025, 9, 11), (2025, 10, 10),
    (2025, 11, 12), (2025, 12, 10),
    # CPI 2026
    (2026, 1, 14), (2026, 2, 13), (2026, 3, 11), (2026, 4, 10), (2026, 5, 12),
    (2026, 6, 10), (2026, 7, 14), (2026, 8, 12), (2026, 9, 11), (2026, 10, 14),
    (2026, 11, 10), (2026, 12, 10),
    # CPI 2027
    (2027, 1, 13), (2027, 2, 11), (2027, 3, 10), (2027, 4, 14), (2027, 5, 12),
    (2027, 6, 10), (2027, 7, 13), (2027, 8, 11), (2027, 9, 10), (2027, 10, 13),
    (2027, 11, 10), (2027, 12, 10),
    # NFP 2024
    (2024, 1, 5), (2024, 2, 2), (2024, 3, 8), (2024, 4, 5), (2024, 5, 3),
    (2024, 6, 7), (2024, 7, 5), (2024, 8, 2), (2024, 9, 6), (2024, 10, 4),
    (2024, 11, 1), (2024, 12, 6),
    # NFP 2025
    (2025, 1, 10), (2025, 2, 7), (2025, 3, 7), (2025, 4, 4), (2025, 5, 2),
    (2025, 6, 6), (2025, 7, 4), (2025, 8, 1), (2025, 9, 5), (2025, 10, 3),
    (2025, 11, 7), (2025, 12, 5),
    # NFP 2026
    (2026, 1, 2), (2026, 2, 6), (2026, 3, 6), (2026, 4, 3), (2026, 5, 1),
    (2026, 6, 5), (2026, 7, 3), (2026, 8, 7), (2026, 9, 4), (2026, 10, 2),
    (2026, 11, 6), (2026, 12, 4),
    # NFP 2027
    (2027, 1, 1), (2027, 2, 5), (2027, 3, 5), (2027, 4, 2), (2027, 5, 7),
    (2027, 6, 4), (2027, 7, 2), (2027, 8, 6), (2027, 9, 3), (2027, 10, 1),
    (2027, 11, 5), (2027, 12, 3),
}

# Module-level cache for loaded dates (populated on first access)
_loaded_dates: set[tuple[int, int, int]] | None = None


def _get_dates() -> set[tuple[int, int, int]]:
    """
    Get high-impact dates, trying the fetcher first, falling back to static.
    Caches result in module-level variable for performance.
    """
    global _loaded_dates
    if _loaded_dates is not None:
        return _loaded_dates

    try:
        from app.services.macro_calendar_fetcher import get_high_impact_dates
        _loaded_dates = get_high_impact_dates()
        if _loaded_dates:
            logger.info(f"Using {len(_loaded_dates)} fetched macro dates")
            return _loaded_dates
    except Exception as e:
        logger.warning(f"Macro calendar fetcher failed, using fallback: {e}")

    _loaded_dates = _FALLBACK_DATES.copy()
    logger.info(f"Using {len(_loaded_dates)} fallback macro dates")
    return _loaded_dates


def refresh_dates(force: bool = False) -> int:
    """
    Refresh macro dates from public sources.
    Returns the count of dates loaded.
    """
    global _loaded_dates
    try:
        from app.services.macro_calendar_fetcher import get_high_impact_dates
        _loaded_dates = get_high_impact_dates(force_refresh=force)
        logger.info(f"Refreshed macro calendar: {len(_loaded_dates)} dates")
        return len(_loaded_dates)
    except Exception as e:
        logger.warning(f"Failed to refresh macro calendar: {e}")
        if _loaded_dates is None:
            _loaded_dates = _FALLBACK_DATES.copy()
        return len(_loaded_dates)


def is_high_impact_day(d: date | None = None, window_days: int = 1) -> bool:
    """
    True if d (default today) is within ±window_days of a known high-impact macro date.
    Used to reduce recommendation confidence on FOMC/CPI/NFP days.
    """
    if d is None:
        d = date.today()
    dates = _get_dates()
    for offset in range(-window_days, window_days + 1):
        t = d + timedelta(days=offset)
        if (t.year, t.month, t.day) in dates:
            return True
    return False


def get_upcoming_events(d: date | None = None, days_ahead: int = 14) -> list[dict[str, Any]]:
    """
    Get upcoming high-impact events within the next N days.
    Returns list of dicts with 'date' and 'days_away' keys.
    """
    if d is None:
        d = date.today()
    dates = _get_dates()
    upcoming: list[dict[str, Any]] = []

    for offset in range(0, days_ahead + 1):
        check_date = d + timedelta(days=offset)
        if (check_date.year, check_date.month, check_date.day) in dates:
            upcoming.append({
                "date": check_date.isoformat(),
                "days_away": offset,
            })

    return upcoming


def get_macro_overlay_info(d: date | None = None, window_days: int = 1) -> dict[str, Any]:
    """
    For API/UI: whether the macro calendar overlay applies today and how it affects confidence.
    """
    active = is_high_impact_day(d, window_days)
    upcoming = get_upcoming_events(d, days_ahead=30)  # 30 days for UI calendar page
    dates = _get_dates()

    return {
        "active": active,
        "as_of": (d or date.today()).isoformat(),
        "window_days": window_days,
        "confidence_penalty_points": MACRO_DAY_CONFIDENCE_PENALTY,
        "total_dates_loaded": len(dates),
        "upcoming_events": upcoming,
        "message": (
            f"Today is within ±{window_days} day(s) of a listed FOMC, CPI, or NFP date. "
            f"The engine applies −{MACRO_DAY_CONFIDENCE_PENALTY} confidence points to each candidate in the batch."
            if active
            else ""
        ),
    }


# Legacy alias for backward compatibility
HIGH_IMPACT_DATES = _FALLBACK_DATES
