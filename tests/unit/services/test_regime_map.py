"""
Tests for the canonical regime taxonomy (app/services/regime_map.py) and the
cross-path guarantee that live and backtest classify the same day identically.
"""
import pandas as pd
import pytest

from app.services.regime_map import (
    classify_canonical_regime,
    direction_weights,
    tech_weights,
    rsi_thresholds,
    stoch_thresholds,
    _PROFILE_RISK_ON,
    _PROFILE_NEUTRAL,
    _PROFILE_BEAR,
)
from app.services.market_context import MarketRegime
from app.services.backtesting.regime import classify_regime, build_regime_calendar


# ---------------------------------------------------------------------------
# §2 Classification rule — priority ladder + None handling
# ---------------------------------------------------------------------------

class TestClassifyCanonicalRegime:
    @pytest.mark.parametrize(
        "vix,spx_vs_sma,expected",
        [
            (45.0, 0.10, "crisis"),    # VIX>=40 dominates regardless of trend
            (45.0, -0.10, "crisis"),
            (32.0, 0.10, "high_vol"),  # VIX>=30 dominates
            (32.0, -0.10, "high_vol"),
            (22.0, -0.05, "bear"),     # below SMA + elevated VIX
            (15.0, 0.05, "bull"),      # above SMA + calm VIX
            (15.0, -0.05, "neutral"),  # below SMA but calm VIX -> not bear
            (22.0, 0.05, "neutral"),   # above SMA but elevated VIX -> not bull
            (20.0, -0.01, "bear"),     # VIX exactly at elevated boundary, below SMA
            (19.99, 0.01, "bull"),     # just under elevated, above SMA
        ],
    )
    def test_priority_ladder(self, vix, spx_vs_sma, expected):
        assert classify_canonical_regime(vix, spx_vs_sma) == expected

    @pytest.mark.parametrize(
        "vix,spx_vs_sma",
        [
            (None, 0.05),    # no VIX -> cannot confirm bull/bear
            (15.0, None),    # no SPX history -> cannot confirm trend
            (None, None),    # nothing
        ],
    )
    def test_missing_input_collapses_to_neutral(self, vix, spx_vs_sma):
        assert classify_canonical_regime(vix, spx_vs_sma) == "neutral"

    def test_exact_sma_equality_is_neutral(self):
        # spx_vs_sma == 0 is neither <0 nor >0 -> falls through
        assert classify_canonical_regime(15.0, 0.0) == "neutral"


# ---------------------------------------------------------------------------
# §3 Profile wiring — 5 labels -> 4 buckets
# ---------------------------------------------------------------------------

class TestProfileWiring:
    def test_bull_inherits_risk_on(self):
        assert direction_weights("bull") == _PROFILE_RISK_ON["direction"]
        assert tech_weights("bull") == _PROFILE_RISK_ON["tech"]
        assert rsi_thresholds("bull") == _PROFILE_RISK_ON["rsi"]
        assert stoch_thresholds("bull") == _PROFILE_RISK_ON["stoch"]

    def test_neutral_uses_neutral_bucket(self):
        assert direction_weights("neutral") == _PROFILE_NEUTRAL["direction"]
        assert tech_weights("neutral") == _PROFILE_NEUTRAL["tech"]

    @pytest.mark.parametrize("label", ["bear", "crisis", "high_vol"])
    def test_defensive_labels_use_bear_bucket(self, label):
        assert direction_weights(label) == _PROFILE_BEAR["direction"]
        assert tech_weights(label) == _PROFILE_BEAR["tech"]
        assert rsi_thresholds(label) == _PROFILE_BEAR["rsi"]
        assert stoch_thresholds(label) == _PROFILE_BEAR["stoch"]

    def test_unknown_or_none_falls_to_neutral(self):
        # Legacy 3-label strings no longer match a bucket -> safe neutral default.
        assert direction_weights("risk_off") == _PROFILE_NEUTRAL["direction"]
        assert direction_weights(None) == _PROFILE_NEUTRAL["direction"]

    def test_bear_is_stricter_than_neutral_for_longs(self):
        # Structural contract: bear demands a higher RSI to call bullish, more
        # sentiment weight, and a stronger mean-reversion tilt than neutral.
        assert rsi_thresholds("bear")[0] > rsi_thresholds("neutral")[0]
        assert direction_weights("bear")[0] > direction_weights("neutral")[0]  # more sentiment
        assert tech_weights("bear")[1] > tech_weights("neutral")[1]            # more mean-rev


# ---------------------------------------------------------------------------
# §6 Cross-path regression — live and backtest classify the SAME day identically
# ---------------------------------------------------------------------------

def _market_regime(vix, spx_above_sma200):
    """Build a live MarketRegime with only the fields extended_regime reads."""
    return MarketRegime(
        regime="cautious",
        spx_trend="neutral",
        vix_level=vix,
        spx_above_sma200=spx_above_sma200,
    )


def _backtest_ohlcv(vix, spx_above_sma200):
    """
    Build minimal ^VIX / ^GSPC frames so classify_regime yields a known sign.
    SPX is a flat series ending either above (last bar +10%) or below (-10%) its
    200-SMA, controlled by spx_above_sma200.
    """
    idx = pd.date_range("2022-01-01", periods=250, freq="D")
    vix_df = pd.DataFrame({"Close": [vix] * 250}, index=idx)
    base = [100.0] * 249
    last = 110.0 if spx_above_sma200 else 90.0
    spx_df = pd.DataFrame({"Close": base + [last]}, index=idx)
    return {"^VIX": vix_df, "^GSPC": spx_df}


# Cutoff AFTER the fixture series ends (series ends 2022-09-07) so classify_regime
# includes the final, discriminating SPX bar in its 200-SMA window.
_CUTOFF = pd.Timestamp("2023-01-01").date()


class TestCrossPathParity:
    @pytest.mark.parametrize(
        "vix,spx_above_sma200",
        [
            (45.0, True),    # crisis (VIX>=40 regardless of trend)
            (32.0, False),   # high_vol (VIX>=30)
            (27.0, False),   # bear — VIX 27 is BELOW unified high_vol (30); pre-unify live called this high_vol
            (22.0, False),   # bear
            (15.0, True),    # bull
            (15.0, False),   # neutral (calm VIX, below SMA)
            (22.0, True),    # neutral (elevated VIX, above SMA)
            (20.0, False),   # bear edge — VIX exactly at elevated boundary
        ],
    )
    def test_live_matches_backtest(self, vix, spx_above_sma200):
        live_label = _market_regime(vix, spx_above_sma200).extended_regime
        bt_label = classify_regime(_CUTOFF, _backtest_ohlcv(vix, spx_above_sma200))
        assert live_label == bt_label, (
            f"live={live_label} != backtest={bt_label} for vix={vix}, "
            f"above_sma200={spx_above_sma200}"
        )

    def test_bear_high_vol_boundary(self):
        # VIX in [20,30): SPX falling. The classic pre-unification divergence point —
        # live's old high_vol threshold was >=25, so it called VIX 27 "high_vol" while
        # backtest called it "bear". Unified: high_vol is >=30, so both now agree on "bear".
        live = _market_regime(27.0, False).extended_regime
        bt = classify_regime(_CUTOFF, _backtest_ohlcv(27.0, False))
        assert live == bt == "bear"

    def test_calendar_matches_per_signal(self):
        ohlcv = _backtest_ohlcv(22.0, False)
        cal = build_regime_calendar([_CUTOFF], ohlcv)
        assert cal[_CUTOFF] == classify_regime(_CUTOFF, ohlcv) == "bear"
