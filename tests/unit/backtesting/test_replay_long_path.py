"""Phase 6 (task 3): replay LONG-signal path, end-to-end.

The short path has gate tests (test_replay_short_filters.py); this pins the long path:
`_build_signal` produces a well-formed long signal, which then flows through `simulate_outcome`
and `compute_metrics` to a resolved win. Network/DB/indicator deps are patched so the assertions
characterize the replay wiring, not yfinance.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

SIM_DATE = date(2024, 6, 1)


def _make_snap(price: float = 100.0, ema200: float | None = 90.0) -> MagicMock:
    snap = MagicMock()
    snap.price = price
    snap.ema200 = ema200
    snap.rsi = 55.0
    snap.rsi_slope = 0.0
    snap.macd = [0.1, 0.05, 0.05]
    snap.macd_hist_slope = None
    snap.macd_bearish = False
    snap.macd_bullish = False
    snap.volume_ratio = 1.2
    snap.adx = [25.0]
    snap.atr_percentile = None
    snap.obv_confirming_down = False
    snap.volatility_regime = "stable"
    snap.vpoc = None
    snap.vp_overextension = None
    snap.vp_bias = None
    snap.ofi_ratio = None
    snap.pct_from_52w_high = None
    snap.in_value_area = MagicMock(return_value=False)
    snap.trend_bias = "bullish"
    snap.atr_pct = 0.02
    snap.vah = None
    snap.val = None
    snap.hvn_levels = None
    return snap


def _make_df(rows: int = 210, price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(end="2024-06-01", periods=rows, freq="B")
    return pd.DataFrame(
        {"Open": price, "High": price * 1.01, "Low": price * 0.99,
         "Close": price, "Volume": 1_000_000},
        index=idx,
    )


def _build_long(snap: MagicMock, confidence: int = 65):
    from app.services.backtesting import replay as replay_mod

    policy = MagicMock()
    policy.min_confidence_offset = 0
    policy.mtf_aligned_confidence_bonus = 8
    policy.direction_style = "any"

    mock_tech = MagicMock()
    mock_tech._ohlcv_cache = {"AAPL": _make_df()}

    with (
        patch.object(replay_mod, "build_snapshot", return_value=snap),
        patch.object(replay_mod, "compute_confidence", return_value=confidence),
        patch.object(replay_mod, "compute_relative_strength", return_value=None),
        patch.object(replay_mod, "compute_atr_levels", return_value=(90.0, 120.0)),
        patch.object(replay_mod, "calc_risk_reward", return_value=2.0),
        patch.object(replay_mod, "_min_rr_floor_for_build", return_value=1.5),
        patch.object(replay_mod, "_guess_category", return_value="stocks_large"),
        patch.object(replay_mod, "get_asset_class_policy", return_value=policy),
        patch.object(replay_mod, "tech", mock_tech),
        patch("app.services.technicals.determine_direction", return_value="long"),
    ):
        return replay_mod._build_signal(
            symbol="AAPL", sim_date=SIM_DATE, risk_tier="moderate",
            min_confidence=40, day_regime="neutral",
        )


class TestReplayLongPathEndToEnd:
    def test_build_produces_wellformed_long_signal(self):
        sig = _build_long(_make_snap())
        assert sig is not None
        assert sig["direction"] == "long"
        assert sig["symbol"] == "AAPL"
        assert sig["entry_price"] == 100.0
        # long geometry: stop below entry, target above
        assert sig["stop_loss"] < sig["entry_price"] < sig["take_profit"]
        assert sig["risk_reward"] == 2.0
        assert sig["entry_date"] == SIM_DATE.isoformat()
        # outcome fields present but unresolved at build time
        assert sig["outcome"] is None and sig["pnl_r"] is None

    def test_build_below_min_confidence_is_dropped(self):
        # min_confidence=40; a confidence that lands under it after adjustments returns None.
        assert _build_long(_make_snap(), confidence=5) is None

    def test_end_to_end_tp_flows_to_metrics(self):
        from app.services.backtesting.metrics import compute_metrics
        from app.services.backtesting.simulator import simulate_outcome

        sig = _build_long(_make_snap())
        # A future bar that spikes through the take-profit (120) on day 1 → hit_tp for a long.
        future = pd.DataFrame(
            {"Open": [101.0], "High": [125.0], "Low": [100.5], "Close": [122.0]},
            index=pd.date_range(start="2024-06-03", periods=1, freq="B"),
        )
        res = simulate_outcome(
            entry_price=sig["entry_price"], stop_loss=sig["stop_loss"],
            take_profit=sig["take_profit"], direction="long",
            future_bars=future, max_bars=14,
        )
        assert res.outcome == "hit_tp"
        assert res.pnl_r > 0

        sig["outcome"] = res.outcome
        sig["pnl_r"] = res.pnl_r
        metrics = compute_metrics([sig])
        assert metrics["total_trades"] == 1
        assert metrics["wins"] == 1
        assert metrics["win_rate"] == pytest.approx(1.0)
