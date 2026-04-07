"""
Tests for macro calendar high-impact day detection.
"""
import pytest
from datetime import date
from unittest.mock import patch
import app.services.macro_calendar as macro_cal
from app.services.macro_calendar import (
    is_high_impact_day,
    get_upcoming_events,
    get_macro_overlay_info,
    MACRO_DAY_CONFIDENCE_PENALTY,
    _FALLBACK_DATES,
)


@pytest.fixture(autouse=True)
def reset_loaded_dates():
    """Reset module-level date cache before each test."""
    macro_cal._loaded_dates = None
    yield
    macro_cal._loaded_dates = None


@pytest.fixture
def use_fallback_dates():
    """Patch fetcher to fail so tests use fallback static dates."""
    with patch("app.services.macro_calendar_fetcher.get_high_impact_dates", side_effect=Exception("no network")):
        yield


class TestIsHighImpactDay:
    def test_known_fomc_date_returns_true(self, use_fallback_dates):
        # 2026-01-28 is a known FOMC date in fallback set
        d = date(2026, 1, 28)
        assert is_high_impact_day(d) is True

    def test_non_impact_date_returns_false(self, use_fallback_dates):
        # A random Saturday unlikely to be a macro event
        d = date(2026, 5, 16)
        assert is_high_impact_day(d) is False

    def test_window_one_day_before_event(self, use_fallback_dates):
        # 2026-01-28 is FOMC; day before (01-27) should match with window_days=1
        d = date(2026, 1, 27)
        assert is_high_impact_day(d, window_days=1) is True

    def test_window_one_day_after_event(self, use_fallback_dates):
        d = date(2026, 1, 29)
        assert is_high_impact_day(d, window_days=1) is True

    def test_outside_window_returns_false(self, use_fallback_dates):
        # 3 days away from nearest event; window=1 → should not match
        d = date(2026, 1, 25)  # 3 days before Jan 28
        assert is_high_impact_day(d, window_days=1) is False

    def test_known_nfp_date_returns_true(self, use_fallback_dates):
        # 2026-01-02 is an NFP date in fallback set
        d = date(2026, 1, 2)
        assert is_high_impact_day(d) is True


class TestGetUpcomingEvents:
    def test_returns_list(self, use_fallback_dates):
        events = get_upcoming_events(date(2026, 1, 1), days_ahead=30)
        assert isinstance(events, list)

    def test_events_within_window(self, use_fallback_dates):
        events = get_upcoming_events(date(2026, 1, 1), days_ahead=5)
        for ev in events:
            assert ev["days_away"] <= 5

    def test_event_has_date_and_days_away(self, use_fallback_dates):
        events = get_upcoming_events(date(2026, 1, 1), days_ahead=30)
        if events:
            assert "date" in events[0]
            assert "days_away" in events[0]

    def test_days_away_zero_for_today_event(self, use_fallback_dates):
        # 2026-01-28 is a known FOMC date
        events = get_upcoming_events(date(2026, 1, 28), days_ahead=0)
        assert any(ev["days_away"] == 0 for ev in events)


class TestGetMacroOverlayInfo:
    def test_active_true_on_fomc_date(self, use_fallback_dates):
        info = get_macro_overlay_info(date(2026, 1, 28))
        assert info["active"] is True

    def test_active_false_on_normal_date(self, use_fallback_dates):
        info = get_macro_overlay_info(date(2026, 5, 16))
        assert info["active"] is False

    def test_confidence_penalty_value(self):
        assert MACRO_DAY_CONFIDENCE_PENALTY == 10

    def test_overlay_info_contains_expected_keys(self, use_fallback_dates):
        info = get_macro_overlay_info(date(2026, 1, 28))
        assert "active" in info
        assert "confidence_penalty_points" in info
        assert "upcoming_events" in info
        assert "total_dates_loaded" in info

    def test_message_set_when_active(self, use_fallback_dates):
        info = get_macro_overlay_info(date(2026, 1, 28))
        assert info["message"] != ""

    def test_message_empty_when_not_active(self, use_fallback_dates):
        info = get_macro_overlay_info(date(2026, 5, 16))
        assert info["message"] == ""
