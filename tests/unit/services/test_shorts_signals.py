"""
Unit tests for the breakdown-family short signal generator (Phase 5a).
See docs/superpowers/plans/2026-09-17-shorts-engine-breakdown-family.md.
"""
from datetime import date

import pandas as pd
import pytest

from app.services.technicals import TechnicalSnapshot
from app.services.recommendations import shorts_signals as ss


def _bars(closes, highs=None, lows=None, vols=None):
    """Build an OHLCV frame from a close list; highs/lows/vols default around close."""
    n = len(closes)
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    vols = vols or [1_000_000] * n
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"Open": closes, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=idx,
    )


def _snap(**kw):
    base = dict(symbol="TESTZ", price=100.0)
    base.update(kw)
    return TechnicalSnapshot(**base)


# ── Task 1: level helpers ────────────────────────────────────────────────────

def test_resolve_support_prefers_nearest_swing_below_prev_close():
    snap = _snap(supports=[95.0, 90.0, 80.0])
    bars = _bars([100.0] * 25)
    assert ss._resolve_support(snap, bars, prev_close=100.0) == 95.0


def test_resolve_support_falls_back_to_20bar_low():
    snap = _snap(supports=[])
    lows = [90.0] * 24 + [100.0]
    bars = _bars([100.0] * 25, lows=lows)
    assert ss._resolve_support(snap, bars, prev_close=100.0) == 90.0


# ── Task 2: support_breakdown ────────────────────────────────────────────────

def test_support_breakdown_fires_on_close_below_support_with_volume():
    snap = _snap(supports=[95.0], volume_ratio=1.5)
    bars = _bars([100.0] * 24 + [94.0])
    assert ss.support_breakdown(snap, bars) is True


def test_support_breakdown_no_fire_without_volume():
    snap = _snap(supports=[95.0], volume_ratio=0.8)
    bars = _bars([100.0] * 24 + [94.0])
    assert ss.support_breakdown(snap, bars) is False


def test_support_breakdown_no_fire_when_close_above_support():
    snap = _snap(supports=[95.0], volume_ratio=1.5)
    bars = _bars([100.0] * 25)
    assert ss.support_breakdown(snap, bars) is False


# ── Task 3: failed_breakout ──────────────────────────────────────────────────

def test_failed_breakout_fires_when_high_pierced_resistance_but_closed_back_below():
    snap = _snap(resistances=[105.0])
    closes = [100.0] * 20 + [101, 106, 104, 103, 102]
    highs = [c * 1.005 for c in closes[:-4]] + [107.0, 106.5, 105.5, 103.0]
    bars = _bars(closes, highs=highs)
    assert ss.failed_breakout(snap, bars) is True


def test_failed_breakout_no_fire_when_still_above_resistance():
    snap = _snap(resistances=[105.0])
    closes = [100.0] * 20 + [106, 107, 108, 108, 107]
    highs = [c * 1.01 for c in closes]
    bars = _bars(closes, highs=highs)
    assert ss.failed_breakout(snap, bars) is False


# ── Task 4: failed_retest ────────────────────────────────────────────────────

def test_failed_retest_fires_break_then_rally_then_reject():
    snap = _snap(supports=[95.0])
    closes = [100.0] * 12 + [93, 92, 96, 95, 94]
    highs = [c * 1.01 for c in closes[:-3]] + [96.0, 95.2, 94.5]
    bars = _bars(closes, highs=highs)
    assert ss.failed_retest(snap, bars) is True


def test_failed_retest_no_fire_when_reclaimed_support():
    snap = _snap(supports=[95.0])
    closes = [100.0] * 12 + [93, 92, 96, 97, 98]
    bars = _bars(closes)
    assert ss.failed_retest(snap, bars) is False


# ── Task 5: detect_short_triggers + pre-gates ────────────────────────────────

def test_detect_returns_matched_triggers_when_gates_pass():
    snap = _snap(supports=[95.0], volume_ratio=1.5, ema200=110.0)
    bars = _bars([100.0] * 24 + [94.0])
    assert "support_breakdown" in ss.detect_short_triggers(snap, bars, regime="bear")


def test_detect_empty_when_above_ema200():
    snap = _snap(supports=[95.0], volume_ratio=1.5, ema200=90.0)
    bars = _bars([100.0] * 24 + [94.0])
    assert ss.detect_short_triggers(snap, bars, regime="bear") == []


def test_detect_empty_in_blocked_regime():
    snap = _snap(supports=[95.0], volume_ratio=1.5, ema200=110.0)
    bars = _bars([100.0] * 24 + [94.0])
    assert ss.detect_short_triggers(snap, bars, regime="high_vol") == []


# ── Task 6: generate_short_signal ────────────────────────────────────────────

def test_generate_short_signal_emits_for_strong_breakdown():
    snap = _snap(
        price=100.0, atr=2.0, rsi=38.0, rsi_slope=-1.0, macd=(-1.0, -0.5, -0.5),
        macd_hist_slope=-0.5, adx=(30.0, 12.0, 28.0), stochastic=(40.0, 45.0),
        bollinger=(105.0, 100.0, 95.0), obv=(1000.0, -5.0), ema200=110.0,
        volume_ratio=1.6, ofi_ratio=0.45, atr_percentile=0.5, supports=[95.0],
    )
    bars = _bars([100.0] * 24 + [94.0])
    sig = ss.generate_short_signal(
        "TESTZ", snap, bars, regime_id="bear", category="stocks_large",
        risk_tier="moderate", preferred_risk="moderate",
        sentiment_avg=None, sentiment_count=0, today=date(2026, 9, 15),
    )
    assert sig is not None
    assert sig.direction == "short"
    assert sig.trigger == "support_breakdown"
    assert sig.confidence >= ss.SHORT_CONFIDENCE_FLOOR
    assert sig.stop_loss > sig.entry > sig.take_profit  # short: SL above, TP below


def test_generate_short_signal_none_when_no_trigger():
    snap = _snap(price=100.0, atr=2.0, ema200=110.0, supports=[95.0], volume_ratio=0.8)
    bars = _bars([100.0] * 25)
    sig = ss.generate_short_signal(
        "TESTZ", snap, bars, regime_id="bear", category="stocks_large",
        risk_tier="moderate", preferred_risk="moderate",
        sentiment_avg=None, sentiment_count=0, today=date(2026, 9, 15),
    )
    assert sig is None


def test_generate_short_signal_none_below_floor():
    snap = _snap(
        price=100.0, atr=2.0, rsi=52.0, ema200=110.0, supports=[95.0], volume_ratio=1.2,
        adx=(15.0, 14.0, 13.0),
    )
    bars = _bars([100.0] * 24 + [94.0])
    sig = ss.generate_short_signal(
        "TESTZ", snap, bars, regime_id="bear", category="stocks_large",
        risk_tier="moderate", preferred_risk="moderate",
        sentiment_avg=None, sentiment_count=0, today=date(2026, 9, 15),
    )
    assert sig is None


# ── Phase 5b: fade-top family ────────────────────────────────────────────────

# Task 2: activation predicate
def test_tape_deteriorating_true_when_vix_rising_and_spx_below_20ma():
    vix = pd.Series([15.0] * 20 + [18.0])
    spx = pd.Series([100.0] * 20 + [95.0])
    assert ss._tape_deteriorating(vix, spx) is True


def test_tape_deteriorating_false_in_calm_uptrend():
    vix = pd.Series([15.0] * 21)
    spx = pd.Series([float(x) for x in range(100, 121)])
    assert ss._tape_deteriorating(vix, spx) is False


# Task 3: fade-top triggers
def test_exhaustion_fires_on_overbought_rolling_over_with_fading_volume():
    snap = _snap(rsi=78.0, macd_hist_slope=-0.4, volume_ratio=0.8)
    bars = _bars([100.0] * 22 + [110, 111, 111], vols=[3e6] * 22 + [3e6, 2e6, 1.5e6])
    assert ss.exhaustion(snap, bars) is True


def test_exhaustion_no_fire_when_rsi_not_extreme():
    snap = _snap(rsi=62.0, macd_hist_slope=-0.4, volume_ratio=0.8)
    bars = _bars([100.0] * 25)
    assert ss.exhaustion(snap, bars) is False


def test_exhaustion_fires_with_only_macd_rollover():
    # RSI>70 anchor + MACD rolling over, flat volume (only one confirmation) -> fires.
    snap = _snap(rsi=75.0, macd_hist_slope=-0.3)
    bars = _bars([100.0] * 25)  # flat volume
    assert ss.exhaustion(snap, bars) is True


def test_exhaustion_no_fire_when_overbought_but_no_rollover():
    # RSI>70 but MACD still rising and volume flat -> no rollover confirmation -> no fire.
    snap = _snap(rsi=75.0, macd_hist_slope=0.2)
    bars = _bars([100.0] * 25)
    assert ss.exhaustion(snap, bars) is False


def test_distribution_fires_near_high_with_obv_rolling_over():
    snap = _snap(obv=(1_000_000.0, -5.0))
    closes = [100.0] * 20 + [118, 119, 120, 120, 119]
    bars = _bars(closes)
    assert ss.distribution(snap, bars) is True


def test_distribution_no_fire_when_obv_confirms():
    snap = _snap(obv=(1_000_000.0, 5.0))
    closes = [100.0] * 20 + [118, 119, 120, 120, 119]
    bars = _bars(closes)
    assert ss.distribution(snap, bars) is False


# Task 4: bespoke top-quality scorer
def test_top_quality_score_textbook_top_scores_high():
    snap = _snap(rsi=85.0, macd_hist_slope=-0.5, obv=(1e6, -5.0),
                 volume_ratio=0.7, stochastic=(85.0, 80.0))
    assert ss.top_quality_score(snap) >= 90


def test_top_quality_score_marginal_top_scores_low():
    snap = _snap(rsi=71.0, macd_hist_slope=0.1, obv=(1e6, 1.0),
                 volume_ratio=1.1, stochastic=(60.0, 55.0))
    assert ss.top_quality_score(snap) < 40


# Task 5: family-aware detect + generate routing
def test_detect_fade_top_requires_above_ema200_and_deteriorating_tape():
    snap = _snap(price=120.0, ema200=100.0, rsi=80.0, macd_hist_slope=-0.4,
                 obv=(1e6, -5.0), volume_ratio=0.7, stochastic=(85.0, 80.0))
    closes = [100.0] * 20 + [118, 119, 120, 120, 120]
    bars = _bars(closes, vols=[3e6] * 22 + [3e6, 2e6, 1.5e6])
    assert "exhaustion" in ss.detect_short_triggers(
        snap, bars, regime="neutral", tape_deteriorating=True)
    assert ss.detect_short_triggers(snap, bars, regime="neutral", tape_deteriorating=False) == []


def test_generate_fade_top_uses_top_quality_score():
    snap = _snap(price=120.0, atr=2.0, ema200=100.0, rsi=85.0, macd_hist_slope=-0.5,
                 obv=(1e6, -5.0), volume_ratio=0.7, stochastic=(85.0, 80.0))
    closes = [100.0] * 20 + [118, 119, 120, 120, 120]
    bars = _bars(closes, vols=[3e6] * 22 + [3e6, 2e6, 1.5e6])
    sig = ss.generate_short_signal(
        "TESTZ", snap, bars, regime_id="neutral", category="stocks_large",
        risk_tier="moderate", preferred_risk="moderate",
        sentiment_avg=None, sentiment_count=0, today=date(2026, 9, 15),
        tape_deteriorating=True,
    )
    assert sig is not None and sig.trigger in ("exhaustion", "distribution")
    assert sig.stop_loss > sig.entry > sig.take_profit
