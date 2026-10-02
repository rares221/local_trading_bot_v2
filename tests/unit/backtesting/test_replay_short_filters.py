"""
Unit tests for short-trade filtering in replay._build_signal().

Tests cover the four gate layers that must reject or pass short signals:
  1. EMA200 hard gate        — do not short above EMA200
  2. Regime hard gate        — crisis/high_vol block all trades
  3. SHORTS_ENABLED flag     — global short disable
  4. Bear-regime confidence floor — BEAR_REGIME_SHORT_MIN_CONFIDENCE
"""
from __future__ import annotations

from datetime import date
from typing import Optional
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

_MODULE = "app.services.backtesting.replay"
SIM_DATE = date(2024, 6, 1)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_snap(
    price: float = 100.0,
    ema200: Optional[float] = None,
    rsi: float = 50.0,
    rsi_slope: float = 0.0,
    macd=None,
    macd_hist_slope: Optional[float] = None,
    macd_bearish: bool = False,
    macd_bullish: bool = False,
    volume_ratio: float = 1.0,
    adx=None,
    atr_percentile: Optional[float] = None,
    obv_confirming_down: bool = False,
    volatility_regime: str = "stable",
    vpoc: Optional[float] = None,
    pct_from_52w_high: Optional[float] = None,
) -> MagicMock:
    snap = MagicMock()
    snap.price = price
    snap.ema200 = ema200
    snap.rsi = rsi
    snap.rsi_slope = rsi_slope
    snap.macd = macd if macd is not None else [0.1, 0.05, 0.05]
    snap.macd_hist_slope = macd_hist_slope
    snap.macd_bearish = macd_bearish
    snap.macd_bullish = macd_bullish
    snap.volume_ratio = volume_ratio
    snap.adx = adx if adx is not None else [25.0]
    snap.atr_percentile = atr_percentile
    snap.obv_confirming_down = obv_confirming_down
    snap.volatility_regime = volatility_regime
    snap.vpoc = vpoc
    snap.vp_overextension = None
    snap.vp_bias = None
    snap.ofi_ratio = None
    snap.pct_from_52w_high = pct_from_52w_high
    snap.in_value_area = MagicMock(return_value=False)
    snap.trend_bias = None
    snap.atr_pct = None
    snap.vah = None
    snap.val = None
    snap.hvn_levels = None
    return snap


def _make_df(rows: int = 210, price: float = 90.0) -> pd.DataFrame:
    """Return a minimal OHLCV DataFrame with enough bars for all indicators."""
    idx = pd.date_range(end="2024-06-01", periods=rows, freq="B")
    return pd.DataFrame(
        {
            "Open": price, "High": price * 1.01,
            "Low": price * 0.99, "Close": price, "Volume": 1_000_000,
        },
        index=idx,
    )


def _build_signal_patched(
    snap: MagicMock,
    direction: str = "short",
    day_regime: str = "bear",
    confidence: int = 65,
    symbol: str = "AAPL",
    df: Optional[pd.DataFrame] = None,
    shorts_enabled: bool = False,
    shorts_bear_enabled: bool = True,
    bear_min_confidence: int = 50,
):
    """
    Call _build_signal with all network/db dependencies patched.
    `direction` controls what determine_direction returns.
    """
    from app.services.backtesting import replay as replay_mod

    df = df or _make_df()

    policy = MagicMock()
    policy.min_confidence_offset = 0
    policy.mtf_aligned_confidence_bonus = 8
    policy.direction_style = "any"

    mock_tech = MagicMock()
    mock_tech._ohlcv_cache = {symbol: df}

    with (
        patch.object(replay_mod, "build_snapshot", return_value=snap),
        patch.object(replay_mod, "compute_confidence", return_value=confidence),
        patch.object(replay_mod, "compute_relative_strength", return_value=None),
        patch.object(replay_mod, "compute_atr_levels", return_value=(85.0, 115.0)),
        patch.object(replay_mod, "calc_risk_reward", return_value=2.0),
        patch.object(replay_mod, "_min_rr_floor_for_build", return_value=1.5),
        patch.object(replay_mod, "_guess_category", return_value="stocks_large"),
        patch.object(replay_mod, "get_asset_class_policy", return_value=policy),
        patch.object(replay_mod, "tech", mock_tech),
        patch("app.services.technicals.determine_direction", return_value=direction),
        patch.object(replay_mod, "SHORTS_ENABLED", shorts_enabled),
        patch.object(replay_mod, "SHORTS_BEAR_REGIME_ENABLED", shorts_bear_enabled),
        patch.object(replay_mod, "BEAR_REGIME_SHORT_MIN_CONFIDENCE", bear_min_confidence),
    ):
        return replay_mod._build_signal(
            symbol=symbol,
            sim_date=SIM_DATE,
            risk_tier="moderate",
            min_confidence=40,
            day_regime=day_regime,
        )


# ---------------------------------------------------------------------------
# Gate 1: EMA200 hard gate
# ---------------------------------------------------------------------------

class TestEma200Gate:
    def test_short_rejected_when_price_above_ema200(self):
        """Price > EMA200 must block shorts — structural upward bias makes these losers."""
        snap = _make_snap(price=150.0, ema200=120.0)
        result = _build_signal_patched(snap, direction="short", shorts_bear_enabled=True)
        assert result is None

    def test_short_allowed_when_price_below_ema200(self):
        """Price < EMA200 means downtrend confirmed — short is eligible."""
        snap = _make_snap(price=80.0, ema200=120.0)
        result = _build_signal_patched(snap, direction="short", shorts_bear_enabled=True)
        assert result is not None

    def test_short_allowed_when_ema200_unavailable(self):
        """If EMA200 data is missing, gate must not fire — missing data ≠ above EMA200."""
        snap = _make_snap(price=150.0, ema200=None)
        result = _build_signal_patched(snap, direction="short", shorts_bear_enabled=True)
        assert result is not None

    def test_long_not_affected_by_ema200_gate(self):
        """EMA200 gate is short-only — longs above EMA200 must pass."""
        snap = _make_snap(price=150.0, ema200=120.0)
        result = _build_signal_patched(snap, direction="long")
        assert result is not None


# ---------------------------------------------------------------------------
# Gate 2: Regime hard gate (crisis / high_vol)
# ---------------------------------------------------------------------------

class TestRegimeGate:
    def test_crisis_regime_blocks_short(self):
        snap = _make_snap(price=80.0, ema200=120.0)
        result = _build_signal_patched(snap, direction="short", day_regime="crisis")
        assert result is None

    def test_high_vol_regime_blocks_short(self):
        snap = _make_snap(price=80.0, ema200=120.0)
        result = _build_signal_patched(snap, direction="short", day_regime="high_vol")
        assert result is None

    def test_crisis_regime_blocks_long_too(self):
        snap = _make_snap(price=150.0, ema200=120.0)
        result = _build_signal_patched(snap, direction="long", day_regime="crisis")
        assert result is None

    def test_bear_regime_allows_short(self):
        snap = _make_snap(price=80.0, ema200=120.0)
        result = _build_signal_patched(snap, direction="short", day_regime="bear", shorts_bear_enabled=True)
        assert result is not None

    def test_neutral_regime_blocks_short_when_only_bear_enabled(self):
        """In neutral regime with only SHORTS_BEAR_REGIME_ENABLED=True, shorts must be blocked."""
        snap = _make_snap(price=80.0, ema200=120.0)
        result = _build_signal_patched(
            snap, direction="short", day_regime="neutral",
            shorts_enabled=False, shorts_bear_enabled=True,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Gate 3: SHORTS_ENABLED flag
# ---------------------------------------------------------------------------

class TestShortsEnabledFlag:
    def test_shorts_globally_disabled_blocks_short_in_bear(self):
        """SHORTS_ENABLED=False + SHORTS_BEAR_REGIME_ENABLED=False must block shorts everywhere."""
        snap = _make_snap(price=80.0, ema200=120.0)
        result = _build_signal_patched(
            snap, direction="short", day_regime="bear",
            shorts_enabled=False, shorts_bear_enabled=False,
        )
        assert result is None

    def test_shorts_globally_enabled_overrides_all(self):
        """SHORTS_ENABLED=True must let shorts through in any non-blocked regime."""
        snap = _make_snap(price=80.0, ema200=120.0)
        result = _build_signal_patched(
            snap, direction="short", day_regime="neutral",
            shorts_enabled=True, shorts_bear_enabled=False,
        )
        assert result is not None

    def test_bear_exception_enables_short_when_flag_set(self):
        """SHORTS_BEAR_REGIME_ENABLED=True must allow shorts in bear regime."""
        snap = _make_snap(price=80.0, ema200=120.0)
        result = _build_signal_patched(
            snap, direction="short", day_regime="bear",
            shorts_enabled=False, shorts_bear_enabled=True,
        )
        assert result is not None

    def test_bear_exception_does_not_fire_outside_bear(self):
        """SHORTS_BEAR_REGIME_ENABLED=True in risk_off regime must not unlock shorts."""
        snap = _make_snap(price=80.0, ema200=120.0)
        result = _build_signal_patched(
            snap, direction="short", day_regime="risk_off",
            shorts_enabled=False, shorts_bear_enabled=True,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Gate 4: Bear-regime confidence floor
# ---------------------------------------------------------------------------

class TestBearConfidenceFloor:
    def test_short_rejected_below_confidence_floor(self):
        """Confidence below BEAR_REGIME_SHORT_MIN_CONFIDENCE must reject the short."""
        snap = _make_snap(price=80.0, ema200=120.0)
        result = _build_signal_patched(
            snap, direction="short", day_regime="bear",
            confidence=45, shorts_bear_enabled=True, bear_min_confidence=50,
        )
        assert result is None

    def test_short_passes_at_confidence_floor(self):
        """Confidence exactly at the floor must pass."""
        snap = _make_snap(price=80.0, ema200=120.0)
        result = _build_signal_patched(
            snap, direction="short", day_regime="bear",
            confidence=50, shorts_bear_enabled=True, bear_min_confidence=50,
        )
        assert result is not None

    def test_short_passes_above_confidence_floor(self):
        snap = _make_snap(price=80.0, ema200=120.0)
        result = _build_signal_patched(
            snap, direction="short", day_regime="bear",
            confidence=70, shorts_bear_enabled=True, bear_min_confidence=50,
        )
        assert result is not None

    def test_confidence_floor_does_not_apply_to_longs(self):
        """Confidence floor is short-specific — a long at confidence=45 must pass."""
        snap = _make_snap(price=100.0, ema200=None)
        result = _build_signal_patched(
            snap, direction="long", day_regime="bear",
            confidence=45, bear_min_confidence=50,
        )
        assert result is not None
