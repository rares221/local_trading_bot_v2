"""Tests for the US/Eastern market-date helper (Phase 5: timezone correctness)."""
from datetime import date, datetime, timezone

from app.core.market_time import market_date, market_now, to_market_date, MARKET_TZ


def test_to_market_date_evening_et_is_previous_utc_rollover_day():
    # 01:00 UTC == 21:00 previous-day ET → the market date is the previous day, even though
    # a UTC date.today() would already read the next day.
    dt = datetime(2026, 6, 2, 1, 0, tzinfo=timezone.utc)
    assert dt.date() == date(2026, 6, 2)          # what UTC date.today() would give
    assert to_market_date(dt) == date(2026, 6, 1)  # the correct market date


def test_to_market_date_midday_utc_same_day():
    dt = datetime(2026, 6, 2, 17, 0, tzinfo=timezone.utc)  # 13:00 ET
    assert to_market_date(dt) == date(2026, 6, 2)


def test_to_market_date_treats_naive_as_utc():
    naive = datetime(2026, 6, 2, 1, 0)  # no tzinfo → treated as UTC
    assert to_market_date(naive) == date(2026, 6, 1)


def test_market_now_is_eastern_aware():
    now = market_now()
    assert now.tzinfo is not None
    assert now.tzinfo.key == MARKET_TZ.key


def test_market_date_matches_now_date():
    assert market_date() == market_now().date()


def test_market_date_flips_at_et_evening_boundary(monkeypatch):
    # Prove the full market_date() chain (not just to_market_date) uses ET: at 00:30 UTC the
    # server clock already reads the next day, but the market date must still be the prior ET day.
    import app.core.market_time as mt

    boundary_et = datetime(2026, 3, 11, 0, 30, tzinfo=timezone.utc).astimezone(mt.MARKET_TZ)
    assert datetime(2026, 3, 11, 0, 30, tzinfo=timezone.utc).date() == date(2026, 3, 11)
    monkeypatch.setattr(mt, "market_now", lambda: boundary_et)
    assert mt.market_date() == date(2026, 3, 10)
