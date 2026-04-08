"""
Tests for regime transition monitoring.
"""
import pytest
from app.services import regime_monitor
from app.services.regime_monitor import (
    detect_regime_transition,
    should_skip_recommendations_on_transition,
    get_trades_needing_review,
    reset_regime_tracking,
    RegimeTransition,
)
from datetime import datetime, timezone


@pytest.fixture(autouse=True)
def reset_state():
    """Reset module-level state before each test."""
    reset_regime_tracking()
    yield
    reset_regime_tracking()


class TestDetectRegimeTransition:
    def test_first_call_initializes_returns_none(self):
        result = detect_regime_transition("risk_on")
        assert result is None

    def test_same_regime_returns_none(self):
        detect_regime_transition("risk_on")
        result = detect_regime_transition("risk_on")
        assert result is None

    def test_regime_change_returns_transition(self):
        detect_regime_transition("risk_on")
        transition = detect_regime_transition("cautious")
        assert transition is not None
        assert transition.from_regime == "risk_on"
        assert transition.to_regime == "cautious"

    def test_minor_severity_risk_on_to_cautious(self):
        detect_regime_transition("risk_on")
        transition = detect_regime_transition("cautious")
        assert transition.severity == "minor"

    def test_major_severity_any_to_risk_off(self):
        detect_regime_transition("risk_on")
        transition = detect_regime_transition("risk_off")
        assert transition.severity == "major"

    def test_major_severity_risk_off_to_risk_on(self):
        detect_regime_transition("risk_off")
        # reset because first call is init
        reset_regime_tracking()
        detect_regime_transition("risk_off")
        transition = detect_regime_transition("risk_on")
        assert transition.severity == "major"

    def test_transition_description_contains_from_and_to(self):
        detect_regime_transition("cautious")
        transition = detect_regime_transition("risk_off")
        desc = transition.description()
        assert "cautious" in desc
        assert "risk_off" in desc


class TestSkipRecommendations:
    def test_none_transition_returns_false(self):
        assert should_skip_recommendations_on_transition(None) is False

    def test_major_transition_returns_true(self):
        transition = RegimeTransition(
            from_regime="risk_on",
            to_regime="risk_off",
            detected_at=datetime.now(timezone.utc),
            severity="major",
        )
        assert should_skip_recommendations_on_transition(transition) is True

    def test_minor_transition_returns_false(self):
        transition = RegimeTransition(
            from_regime="risk_on",
            to_regime="cautious",
            detected_at=datetime.now(timezone.utc),
            severity="minor",
        )
        assert should_skip_recommendations_on_transition(transition) is False


class TestGetTradesNeedingReview:
    def _make_trade(self, trade_id, asset_id, phase="open", signal_flip_count=0):
        return {
            "id": trade_id,
            "asset_id": asset_id,
            "trade_phase": phase,
            "signal_flip_count": signal_flip_count,
        }

    def test_no_transition_returns_empty(self):
        trades = [self._make_trade(1, "AAPL")]
        result = get_trades_needing_review(trades, None)
        assert result == []

    def test_major_transition_flags_all_open_trades(self):
        trades = [
            self._make_trade(1, "AAPL"),
            self._make_trade(2, "TSLA"),
        ]
        transition = RegimeTransition(
            from_regime="cautious",
            to_regime="risk_off",
            detected_at=datetime.now(timezone.utc),
            severity="major",
        )
        result = get_trades_needing_review(trades, transition)
        assert len(result) == 2
        assert any(r["trade_id"] == 1 for r in result)
        assert any(r["trade_id"] == 2 for r in result)

    def test_major_transition_skips_closed_trades(self):
        trades = [
            self._make_trade(1, "AAPL", phase="closed_tp"),
            self._make_trade(2, "TSLA", phase="open"),
        ]
        transition = RegimeTransition(
            from_regime="cautious",
            to_regime="risk_off",
            detected_at=datetime.now(timezone.utc),
            severity="major",
        )
        result = get_trades_needing_review(trades, transition)
        assert len(result) == 1
        assert result[0]["trade_id"] == 2

    def test_minor_transition_only_flags_signal_flips(self):
        trades = [
            self._make_trade(1, "AAPL", signal_flip_count=0),
            self._make_trade(2, "TSLA", signal_flip_count=2),
        ]
        transition = RegimeTransition(
            from_regime="risk_on",
            to_regime="cautious",
            detected_at=datetime.now(timezone.utc),
            severity="minor",
        )
        result = get_trades_needing_review(trades, transition)
        assert len(result) == 1
        assert result[0]["trade_id"] == 2

    def test_empty_trades_returns_empty(self):
        transition = RegimeTransition(
            from_regime="risk_on",
            to_regime="risk_off",
            detected_at=datetime.now(timezone.utc),
            severity="major",
        )
        assert get_trades_needing_review([], transition) == []
