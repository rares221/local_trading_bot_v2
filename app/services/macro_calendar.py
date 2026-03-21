"""
Macro calendar overlay: high-impact event dates (FOMC, CPI, NFP) to reduce
recommendation confidence on and around those days. Static list; extend as needed.
"""
from datetime import date, timedelta

# High-impact US macro dates (FOMC decision, CPI release, NFP). Format: (year, month, day).
# FOMC 2024–2025 (meeting days); CPI ~mid-month; NFP first Friday.
HIGH_IMPACT_DATES: set[tuple[int, int, int]] = {
    # 2024 FOMC
    (2024, 1, 31), (2024, 3, 20), (2024, 5, 1), (2024, 6, 12),
    (2024, 7, 31), (2024, 9, 18), (2024, 11, 7), (2024, 12, 18),
    # 2025 FOMC
    (2025, 1, 29), (2025, 3, 19), (2025, 5, 7), (2025, 6, 18),
    (2025, 7, 30), (2025, 9, 17), (2025, 11, 5), (2025, 12, 17),
    # CPI release days (approx mid-month)
    (2024, 1, 11), (2024, 2, 13), (2024, 3, 12), (2024, 4, 10), (2024, 5, 15),
    (2024, 6, 12), (2024, 7, 11), (2024, 8, 14), (2024, 9, 11), (2024, 10, 10),
    (2024, 11, 13), (2024, 12, 11),
    (2025, 1, 15), (2025, 2, 12), (2025, 3, 12), (2025, 4, 10), (2025, 5, 14),
    (2025, 6, 11), (2025, 7, 10), (2025, 8, 13), (2025, 9, 11), (2025, 10, 10),
    (2025, 11, 12), (2025, 12, 10),
    # NFP first Friday of month (sample)
    (2024, 1, 5), (2024, 2, 2), (2024, 3, 8), (2024, 4, 5), (2024, 5, 3),
    (2024, 6, 7), (2024, 7, 5), (2024, 8, 2), (2024, 9, 6), (2024, 10, 4),
    (2024, 11, 1), (2024, 12, 6),
    (2025, 1, 10), (2025, 2, 7), (2025, 3, 7), (2025, 4, 4), (2025, 5, 2),
    (2025, 6, 6), (2025, 7, 4), (2025, 8, 1), (2025, 9, 5), (2025, 10, 3),
    (2025, 11, 7), (2025, 12, 5),
}


def is_high_impact_day(d: date | None = None, window_days: int = 1) -> bool:
    """
    True if d (default today) is within ±window_days of a known high-impact macro date.
    Used to reduce recommendation confidence on FOMC/CPI/NFP days.
    """
    if d is None:
        d = date.today()
    for offset in range(-window_days, window_days + 1):
        t = d + timedelta(days=offset)
        if (t.year, t.month, t.day) in HIGH_IMPACT_DATES:
            return True
    return False
