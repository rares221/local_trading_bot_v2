"""
Tests for the backtesting trade outcome simulator.
"""
import pytest
import pandas as pd
from datetime import date, datetime

from app.services.backtesting.simulator import simulate_outcome, SimulatedOutcome


def make_bars(rows: list[dict]) -> pd.DataFrame:
    """Helper: build a minimal OHLCV DataFrame with DatetimeIndex."""
    dates = pd.date_range("2024-01-01", periods=len(rows), freq="D")
    df = pd.DataFrame(rows, index=dates)
    return df


class TestSimulateOutcomeEmpty:
    def test_empty_bars_returns_expired(self):
        result = simulate_outcome(
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            direction="long",
            future_bars=pd.DataFrame(),
        )
        assert result.outcome == "expired"
        assert result.bars_held == 0

    def test_none_bars_returns_expired(self):
        result = simulate_outcome(
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            direction="long",
            future_bars=None,
        )
        assert result.outcome == "expired"


class TestSimulateOutcomeLong:
    def test_long_hits_tp(self):
        bars = make_bars([
            {"Open": 101, "High": 115, "Low": 100, "Close": 113},
        ])
        result = simulate_outcome(100.0, 95.0, 110.0, "long", bars)
        assert result.outcome == "hit_tp"
        assert result.closing_price == 110.0
        assert result.pnl_r == pytest.approx(2.0, abs=0.01)

    def test_long_hits_sl(self):
        bars = make_bars([
            {"Open": 99, "High": 101, "Low": 93, "Close": 94},
        ])
        result = simulate_outcome(100.0, 95.0, 110.0, "long", bars)
        assert result.outcome == "hit_sl"
        assert result.pnl_r < 0

    def test_long_expires_no_trigger(self):
        bars = make_bars([
            {"Open": 100, "High": 102, "Low": 98, "Close": 101},
            {"Open": 101, "High": 103, "Low": 99, "Close": 102},
        ])
        result = simulate_outcome(100.0, 95.0, 110.0, "long", bars, max_bars=2)
        assert result.outcome == "expired"
        assert result.bars_held == 2

    def test_long_sl_before_tp_same_bar_conservative(self):
        """When both SL and TP are hit on same bar and open is normal, SL wins."""
        bars = make_bars([
            {"Open": 100, "High": 115, "Low": 93, "Close": 105},
        ])
        result = simulate_outcome(100.0, 95.0, 110.0, "long", bars)
        assert result.outcome == "hit_sl"

    def test_long_gap_slippage_on_open(self):
        """If bar opens below SL (gap down), fill at open (slippage)."""
        bars = make_bars([
            {"Open": 92, "High": 98, "Low": 90, "Close": 93},
        ])
        result = simulate_outcome(100.0, 95.0, 110.0, "long", bars)
        assert result.outcome == "hit_sl"
        assert result.closing_price == 92.0

    def test_long_pnl_r_win_is_positive(self):
        bars = make_bars([
            {"Open": 101, "High": 115, "Low": 100, "Close": 113},
        ])
        result = simulate_outcome(100.0, 95.0, 110.0, "long", bars)
        assert result.pnl_r > 0

    def test_long_bars_held_counts_correctly(self):
        bars = make_bars([
            {"Open": 100, "High": 102, "Low": 98, "Close": 101},
            {"Open": 101, "High": 103, "Low": 99, "Close": 102},
            {"Open": 102, "High": 115, "Low": 101, "Close": 113},
        ])
        result = simulate_outcome(100.0, 95.0, 110.0, "long", bars)
        assert result.outcome == "hit_tp"
        assert result.bars_held == 3


class TestSimulateOutcomeShort:
    def test_short_hits_tp(self):
        bars = make_bars([
            {"Open": 99, "High": 100, "Low": 88, "Close": 90},
        ])
        result = simulate_outcome(100.0, 105.0, 90.0, "short", bars)
        assert result.outcome == "hit_tp"
        assert result.closing_price == 90.0

    def test_short_hits_sl(self):
        bars = make_bars([
            {"Open": 101, "High": 108, "Low": 100, "Close": 106},
        ])
        result = simulate_outcome(100.0, 105.0, 90.0, "short", bars)
        assert result.outcome == "hit_sl"
        # _pnl_r is direction-aware: for a short, pnl_r = (entry - close) / risk, so an SL
        # hit at 105 on a short entered at 100 (risk 5) is -1.0 R — a loss, not a gain.
        assert result.pnl_r == pytest.approx(-1.0, abs=0.01)
        assert result.closing_price == 105.0

    def test_short_expires(self):
        bars = make_bars([
            {"Open": 100, "High": 102, "Low": 97, "Close": 99},
        ])
        result = simulate_outcome(100.0, 105.0, 90.0, "short", bars, max_bars=1)
        assert result.outcome == "expired"

    def test_short_gap_slippage_on_open(self):
        """If bar opens above SL on short, fill at open."""
        bars = make_bars([
            {"Open": 108, "High": 110, "Low": 107, "Close": 109},
        ])
        result = simulate_outcome(100.0, 105.0, 90.0, "short", bars)
        assert result.outcome == "hit_sl"
        assert result.closing_price == 108.0


class TestSimulateOutcomeMaxBars:
    def test_max_bars_limits_scanning(self):
        # 5 bars that don't hit SL/TP, max_bars=3 should expire at bar 3
        bars = make_bars([
            {"Open": 100, "High": 102, "Low": 98, "Close": 101},
            {"Open": 101, "High": 103, "Low": 99, "Close": 102},
            {"Open": 102, "High": 104, "Low": 100, "Close": 103},
            {"Open": 103, "High": 115, "Low": 102, "Close": 112},  # would hit TP
            {"Open": 113, "High": 116, "Low": 112, "Close": 114},
        ])
        result = simulate_outcome(100.0, 95.0, 110.0, "long", bars, max_bars=3)
        assert result.outcome == "expired"
        assert result.bars_held == 3
