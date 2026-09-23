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
    snap = _snap(resistances=[105.0], volume_ratio=1.2)
    closes = [100.0] * 20 + [101, 106, 104, 103, 102]
    highs = [c * 1.005 for c in closes[:-4]] + [107.0, 106.5, 105.5, 103.0]
    bars = _bars(closes, highs=highs)
    assert ss.failed_breakout(snap, bars) is True


def test_failed_breakout_no_fire_when_still_above_resistance():
    snap = _snap(resistances=[105.0], volume_ratio=1.2)
    closes = [100.0] * 20 + [106, 107, 108, 108, 107]
    highs = [c * 1.01 for c in closes]
    bars = _bars(closes, highs=highs)
    assert ss.failed_breakout(snap, bars) is False


# ── Task 4: failed_retest ────────────────────────────────────────────────────

def test_failed_retest_fires_break_then_rally_then_reject():
    snap = _snap(supports=[95.0], volume_ratio=1.2)
    closes = [100.0] * 12 + [93, 92, 96, 95, 94]
    highs = [c * 1.01 for c in closes[:-3]] + [96.0, 95.2, 94.5]
    bars = _bars(closes, highs=highs)
    assert ss.failed_retest(snap, bars) is True


def test_failed_retest_no_fire_when_reclaimed_support():
    snap = _snap(supports=[95.0], volume_ratio=1.2)
    closes = [100.0] * 12 + [93, 92, 96, 97, 98]
    bars = _bars(closes)
    assert ss.failed_retest(snap, bars) is False


# ── Task 5: detect_short_triggers + pre-gates ────────────────────────────────

def test_detect_returns_matched_triggers_when_gates_pass():
    snap = _snap(supports=[95.0], volume_ratio=1.5, ema200=110.0)
    bars = _bars([100.0] * 24 + [94.0])
    assert "support_breakdown" in ss.detect_short_triggers(
        snap, bars, regime="bear", rs_vs_market=0.85)


def test_detect_empty_when_above_ema200():
    snap = _snap(supports=[95.0], volume_ratio=1.5, ema200=90.0)
    bars = _bars([100.0] * 24 + [94.0])
    assert ss.detect_short_triggers(snap, bars, regime="bear") == []


def test_detect_empty_in_blocked_regime():
    snap = _snap(supports=[95.0], volume_ratio=1.5, ema200=110.0)
    bars = _bars([100.0] * 24 + [94.0])
    assert ss.detect_short_triggers(snap, bars, regime="high_vol") == []


# ── Task 6: try_arm_short + finalize_short_signal (Phase 2: two-stage entry) ─
# generate_short_signal (the old one-shot detect+enter) is RETIRED — Phase 2 replaces it
# with arm (try_arm_short) -> wait for confirmation (check_confirmation) -> finalize
# (finalize_short_signal), so entries never happen on the trigger bar itself. See
# docs/superpowers/plans/2026-09-18-shorts-engine.md §5.

def test_try_arm_short_arms_for_strong_breakdown():
    snap = _snap(
        price=100.0, atr=2.0, rsi=38.0, rsi_slope=-1.0, macd=(-1.0, -0.5, -0.5),
        macd_hist_slope=-0.5, adx=(30.0, 12.0, 28.0), stochastic=(40.0, 45.0),
        bollinger=(105.0, 100.0, 95.0), obv=(1000.0, -5.0), ema200=110.0,
        volume_ratio=1.6, ofi_ratio=0.45, atr_percentile=0.5, supports=[95.0],
    )
    bars = _bars([100.0] * 24 + [94.0])
    trigger = ss.try_arm_short(snap, bars, regime="bear", rs_vs_market=0.85)
    assert trigger == "support_breakdown"
    assert ss.family_of_trigger(trigger) == "breakdown"


def test_try_arm_short_none_when_no_trigger():
    snap = _snap(price=100.0, atr=2.0, ema200=110.0, supports=[95.0], volume_ratio=0.8)
    bars = _bars([100.0] * 25)
    assert ss.try_arm_short(snap, bars, regime="bear", rs_vs_market=0.85) is None


def test_finalize_short_signal_emits_for_strong_breakdown():
    snap = _snap(
        price=100.0, atr=2.0, rsi=38.0, rsi_slope=-1.0, macd=(-1.0, -0.5, -0.5),
        macd_hist_slope=-0.5, adx=(30.0, 12.0, 28.0), stochastic=(40.0, 45.0),
        bollinger=(105.0, 100.0, 95.0), obv=(1000.0, -5.0), ema200=110.0,
        volume_ratio=1.6, ofi_ratio=0.45, atr_percentile=0.5, supports=[95.0],
    )
    bars = _bars([100.0] * 24 + [94.0])
    armed = ss.ArmedShort(symbol="TESTZ", category="stocks_large", family="breakdown",
                          trigger="support_breakdown", armed_date=date(2026, 9, 12))
    sig = ss.finalize_short_signal(
        armed, snap, bars, regime_id="bear", risk_tier="moderate",
        preferred_risk="moderate", sentiment_avg=None, sentiment_count=0,
        today=date(2026, 9, 15),
    )
    assert sig is not None
    assert sig.direction == "short"
    assert sig.trigger == "support_breakdown"
    assert sig.confidence >= ss.SHORT_CONFIDENCE_FLOOR
    assert sig.stop_loss > sig.entry > sig.take_profit  # short: SL above, TP below


def test_finalize_short_signal_none_below_floor():
    snap = _snap(
        price=100.0, atr=2.0, rsi=52.0, ema200=110.0, supports=[95.0], volume_ratio=1.2,
        adx=(15.0, 14.0, 13.0),
    )
    bars = _bars([100.0] * 24 + [94.0])
    armed = ss.ArmedShort(symbol="TESTZ", category="stocks_large", family="breakdown",
                          trigger="support_breakdown", armed_date=date(2026, 9, 12))
    sig = ss.finalize_short_signal(
        armed, snap, bars, regime_id="bear", risk_tier="moderate",
        preferred_risk="moderate", sentiment_avg=None, sentiment_count=0,
        today=date(2026, 9, 15),
    )
    assert sig is None


# ── Phase 1 redesign: relative-strength-vs-market hard gate ─────────────────

class TestRelativeStrengthGate:
    """Breakdown shorts require confirmed relative WEAKNESS vs market (O'Neil's Cardinal
    Rule); fade-top shorts require confirmed PRIOR relative STRENGTH (a real former leader
    completing a top, not a laggard). Both fail closed when RS data is unavailable."""

    def test_breakdown_blocked_when_rs_not_weak_enough(self):
        snap = _snap(supports=[95.0], volume_ratio=1.5, ema200=110.0)
        bars = _bars([100.0] * 24 + [94.0])
        assert ss.detect_short_triggers(
            snap, bars, regime="bear", rs_vs_market=0.95) == []

    def test_breakdown_blocked_when_rs_missing(self):
        snap = _snap(supports=[95.0], volume_ratio=1.5, ema200=110.0)
        bars = _bars([100.0] * 24 + [94.0])
        assert ss.detect_short_triggers(
            snap, bars, regime="bear", rs_vs_market=None) == []

    def test_breakdown_allowed_when_rs_confirms_weakness(self):
        snap = _snap(supports=[95.0], volume_ratio=1.5, ema200=110.0)
        bars = _bars([100.0] * 24 + [94.0])
        assert "support_breakdown" in ss.detect_short_triggers(
            snap, bars, regime="bear", rs_vs_market=0.89)

    def test_fadetop_blocked_when_rs_prior_not_strong_enough(self):
        snap = _snap(price=120.0, ema200=100.0, rsi=80.0, macd_hist_slope=-0.4,
                     obv=(1e6, -5.0), volume_ratio=0.7, stochastic=(85.0, 80.0))
        closes = [100.0] * 20 + [118, 119, 120, 120, 120]
        bars = _bars(closes, vols=[3e6] * 22 + [3e6, 2e6, 1.5e6])
        assert ss.detect_short_triggers(
            snap, bars, regime="neutral", tape_deteriorating=True, rs_prior=1.05) == []

    def test_fadetop_blocked_when_rs_prior_missing(self):
        snap = _snap(price=120.0, ema200=100.0, rsi=80.0, macd_hist_slope=-0.4,
                     obv=(1e6, -5.0), volume_ratio=0.7, stochastic=(85.0, 80.0))
        closes = [100.0] * 20 + [118, 119, 120, 120, 120]
        bars = _bars(closes, vols=[3e6] * 22 + [3e6, 2e6, 1.5e6])
        assert ss.detect_short_triggers(
            snap, bars, regime="neutral", tape_deteriorating=True, rs_prior=None) == []

    def test_fadetop_allowed_when_rs_prior_confirms_former_leader(self):
        snap = _snap(price=120.0, ema200=100.0, rsi=80.0, macd_hist_slope=-0.4,
                     obv=(1e6, -5.0), volume_ratio=0.7, stochastic=(85.0, 80.0))
        closes = [100.0] * 20 + [118, 119, 120, 120, 120]
        bars = _bars(closes, vols=[3e6] * 22 + [3e6, 2e6, 1.5e6])
        assert "exhaustion" in ss.detect_short_triggers(
            snap, bars, regime="neutral", tape_deteriorating=True, rs_prior=1.15)

    def test_breakdown_blocked_exactly_at_rs_boundary(self):
        # rs_vs_market == BREAKDOWN_RS_MAX is NOT < the max -> strict inequality blocks at
        # the boundary itself (fail-closed leans conservative, not permissive, at the edge).
        snap = _snap(supports=[95.0], volume_ratio=1.5, ema200=110.0)
        bars = _bars([100.0] * 24 + [94.0])
        assert ss.detect_short_triggers(
            snap, bars, regime="bear", rs_vs_market=ss.BREAKDOWN_RS_MAX) == []

    def test_fadetop_blocked_exactly_at_rs_boundary(self):
        # rs_prior == FADE_TOP_RS_MIN is NOT > the min -> strict inequality blocks at the edge.
        snap = _snap(price=120.0, ema200=100.0, rsi=80.0, macd_hist_slope=-0.4,
                     obv=(1e6, -5.0), volume_ratio=0.7, stochastic=(85.0, 80.0))
        closes = [100.0] * 20 + [118, 119, 120, 120, 120]
        bars = _bars(closes, vols=[3e6] * 22 + [3e6, 2e6, 1.5e6])
        assert ss.detect_short_triggers(
            snap, bars, regime="neutral", tape_deteriorating=True,
            rs_prior=ss.FADE_TOP_RS_MIN) == []


# ── Phase 1 redesign: volume confirmation on failed_breakout / failed_retest ─

class TestVolumeVetoOnBreakdownTriggers:
    def test_failed_breakout_no_fire_without_volume(self):
        snap = _snap(resistances=[105.0], volume_ratio=0.9)
        closes = [100.0] * 20 + [101, 106, 104, 103, 102]
        highs = [c * 1.005 for c in closes[:-4]] + [107.0, 106.5, 105.5, 103.0]
        bars = _bars(closes, highs=highs)
        assert ss.failed_breakout(snap, bars) is False

    def test_failed_breakout_fires_with_volume(self):
        snap = _snap(resistances=[105.0], volume_ratio=1.2)
        closes = [100.0] * 20 + [101, 106, 104, 103, 102]
        highs = [c * 1.005 for c in closes[:-4]] + [107.0, 106.5, 105.5, 103.0]
        bars = _bars(closes, highs=highs)
        assert ss.failed_breakout(snap, bars) is True

    def test_failed_retest_no_fire_without_volume(self):
        snap = _snap(supports=[95.0], volume_ratio=0.9)
        closes = [100.0] * 12 + [93, 92, 96, 95, 94]
        highs = [c * 1.01 for c in closes[:-3]] + [96.0, 95.2, 94.5]
        bars = _bars(closes, highs=highs)
        assert ss.failed_retest(snap, bars) is False

    def test_failed_retest_fires_with_volume(self):
        snap = _snap(supports=[95.0], volume_ratio=1.2)
        closes = [100.0] * 12 + [93, 92, 96, 95, 94]
        highs = [c * 1.01 for c in closes[:-3]] + [96.0, 95.2, 94.5]
        bars = _bars(closes, highs=highs)
        assert ss.failed_retest(snap, bars) is True


# ── Phase 1 redesign: horizon-matched R:R sizing (replaces blind TP-stretch) ─

class TestHorizonMatchedSizing:
    def _snap_for_sizing(self, **kw):
        base = dict(
            price=100.0, atr=2.0, rsi=38.0, rsi_slope=-1.0, macd=(-1.0, -0.5, -0.5),
            macd_hist_slope=-0.5, adx=(30.0, 12.0, 28.0), stochastic=(40.0, 45.0),
            bollinger=(105.0, 100.0, 95.0), obv=(1000.0, -5.0), ema200=110.0,
            volume_ratio=1.6, ofi_ratio=0.45, atr_percentile=0.5, supports=[95.0],
        )
        base.update(kw)
        return _snap(**base)

    def _armed(self):
        return ss.ArmedShort(symbol="TESTZ", category="stocks_large", family="breakdown",
                             trigger="support_breakdown", armed_date=date(2026, 9, 12))

    def test_target_rejected_when_unreachable_within_horizon(self):
        # avg_daily_move=0.3%/day x 10-day cap = $3 reachable; the floor-implied stretch
        # needs ~$7.5 -> capped target can't clear the floor -> signal rejected, not
        # stretched to an unreachable distance.
        snap = self._snap_for_sizing(avg_daily_move=0.003)
        bars = _bars([100.0] * 24 + [94.0])
        sig = ss.finalize_short_signal(
            self._armed(), snap, bars, regime_id="bear", risk_tier="moderate",
            preferred_risk="moderate", sentiment_avg=None, sentiment_count=0,
            today=date(2026, 9, 15),
        )
        assert sig is None

    def test_target_not_capped_when_horizon_is_ample(self):
        # avg_daily_move=2%/day x 10-day cap = $20 reachable, well above what's needed ->
        # behaves like the un-capped floor stretch.
        snap = self._snap_for_sizing(avg_daily_move=0.02)
        bars = _bars([100.0] * 24 + [94.0])
        sig = ss.finalize_short_signal(
            self._armed(), snap, bars, regime_id="bear", risk_tier="moderate",
            preferred_risk="moderate", sentiment_avg=None, sentiment_count=0,
            today=date(2026, 9, 15),
        )
        assert sig is not None
        assert sig.risk_reward >= ss._SHORT_MIN_RR

    def test_stretch_skipped_when_risk_not_positive(self, monkeypatch):
        # If the base SL/TP ever came back with risk <= 0 (sl at/below price for a short — an
        # invariant of compute_atr_levels elsewhere, not re-tested here), the new stretch/cap
        # block must skip cleanly rather than compute against a non-positive risk.
        monkeypatch.setattr(ss, "compute_atr_levels", lambda *a, **kw: (99.0, 93.0))
        snap = self._snap_for_sizing(avg_daily_move=0.02)
        bars = _bars([100.0] * 24 + [94.0])
        sig = ss.finalize_short_signal(
            self._armed(), snap, bars, regime_id="bear", risk_tier="moderate",
            preferred_risk="moderate", sentiment_avg=None, sentiment_count=0,
            today=date(2026, 9, 15),
        )
        # tp passed through unstretched (the guard skipped the block entirely).
        assert sig is not None
        assert sig.take_profit == 93.0


# ── Phase 1 redesign: flattened trigger offsets (no unevidenced ranking) ─────

def test_trigger_offsets_flat_within_each_family():
    assert (ss.TRIGGER_BASE_OFFSET["support_breakdown"]
            == ss.TRIGGER_BASE_OFFSET["failed_breakout"]
            == ss.TRIGGER_BASE_OFFSET["failed_retest"])
    assert ss.TRIGGER_BASE_OFFSET["distribution"] == ss.TRIGGER_BASE_OFFSET["exhaustion"]


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
        snap, bars, regime="neutral", tape_deteriorating=True, rs_prior=1.15)
    assert ss.detect_short_triggers(
        snap, bars, regime="neutral", tape_deteriorating=False, rs_prior=1.15) == []


def test_try_arm_fade_top_then_finalize_uses_top_quality_score():
    snap = _snap(price=120.0, atr=2.0, ema200=100.0, rsi=85.0, macd_hist_slope=-0.5,
                 obv=(1e6, -5.0), volume_ratio=0.7, stochastic=(85.0, 80.0))
    closes = [100.0] * 20 + [118, 119, 120, 120, 120]
    bars = _bars(closes, vols=[3e6] * 22 + [3e6, 2e6, 1.5e6])
    trigger = ss.try_arm_short(
        snap, bars, regime="neutral", tape_deteriorating=True, rs_prior=1.15,
    )
    assert trigger in ("exhaustion", "distribution")
    armed = ss.ArmedShort(symbol="TESTZ", category="stocks_large",
                          family=ss.family_of_trigger(trigger), trigger=trigger,
                          armed_date=date(2026, 9, 12))
    sig = ss.finalize_short_signal(
        armed, snap, bars, regime_id="neutral", risk_tier="moderate",
        preferred_risk="moderate", sentiment_avg=None, sentiment_count=0,
        today=date(2026, 9, 15),
    )
    assert sig is not None and sig.trigger in ("exhaustion", "distribution")
    assert sig.stop_loss > sig.entry > sig.take_profit


# ── Phase 2: confirmation-bar entry delay (arm -> wait -> confirm/expire) ────
# Replaces immediate entry on the trigger bar (which risks catching the "trap" leg of a
# failed breakout / exhaustion signal that reverses instead of following through) with a
# 2-stage flow: arm on the trigger day, then require a subsequent day's close below its
# 10-day EMA (confirming the move actually continued) within a 3-trading-day window. The
# family's structural gates (regime/EMA200/RS) are re-checked at confirmation time using
# THAT day's data, not the arm day's — a setup that's no longer valid expires rather than
# confirming on stale conditions. See docs/superpowers/plans/2026-09-18-shorts-engine.md §5.

class TestCheckConfirmation:
    def _armed_breakdown(self, armed_date=date(2026, 9, 12)):
        return ss.ArmedShort(symbol="TESTZ", category="stocks_large", family="breakdown",
                             trigger="support_breakdown", armed_date=armed_date)

    def _armed_fade_top(self, armed_date=date(2026, 9, 12)):
        return ss.ArmedShort(symbol="TESTZ", category="stocks_large", family="fade_top",
                             trigger="exhaustion", armed_date=armed_date)

    def test_confirmed_when_close_breaks_below_10ema(self):
        # Long flat run (ema10 settles near 100) then a sharp drop on the latest bar -> close
        # clearly below its 10-day EMA -> confirms, given the family gate still holds.
        snap = _snap(price=85.0, ema200=110.0)
        bars = _bars([100.0] * 15 + [85.0])
        status = ss.check_confirmation(
            self._armed_breakdown(), snap, bars, regime="bear", tape_deteriorating=False,
            rs_vs_market=0.85, rs_prior=None, days_since_armed=1,
        )
        assert status == "confirmed"

    def test_pending_when_not_yet_below_ema_and_within_window(self):
        # Flat price the whole way through -> close == ema10, not below it -> not confirmed
        # yet, but still within the window and the family gate holds -> pending.
        snap = _snap(price=100.0, ema200=110.0)
        bars = _bars([100.0] * 16)
        status = ss.check_confirmation(
            self._armed_breakdown(), snap, bars, regime="bear", tape_deteriorating=False,
            rs_vs_market=0.85, rs_prior=None, days_since_armed=1,
        )
        assert status == "pending"

    def test_expired_when_window_elapses_without_confirming(self):
        snap = _snap(price=100.0, ema200=110.0)
        bars = _bars([100.0] * 16)
        status = ss.check_confirmation(
            self._armed_breakdown(), snap, bars, regime="bear", tape_deteriorating=False,
            rs_vs_market=0.85, rs_prior=None, days_since_armed=ss._CONFIRMATION_WINDOW_DAYS,
        )
        assert status == "expired"

    def test_expired_when_regime_no_longer_bear(self):
        # Would otherwise confirm (close well below ema10), but the breakdown family's
        # structural gate (regime=="bear") no longer holds -> expire rather than confirm on a
        # stale read.
        snap = _snap(price=85.0, ema200=110.0)
        bars = _bars([100.0] * 15 + [85.0])
        status = ss.check_confirmation(
            self._armed_breakdown(), snap, bars, regime="neutral", tape_deteriorating=False,
            rs_vs_market=0.85, rs_prior=None, days_since_armed=1,
        )
        assert status == "expired"

    def test_expired_when_rs_gate_no_longer_holds(self):
        snap = _snap(price=85.0, ema200=110.0)
        bars = _bars([100.0] * 15 + [85.0])
        status = ss.check_confirmation(
            self._armed_breakdown(), snap, bars, regime="bear", tape_deteriorating=False,
            rs_vs_market=0.97, rs_prior=None, days_since_armed=1,  # no longer weak enough
        )
        assert status == "expired"

    def test_expired_when_blocked_regime(self):
        snap = _snap(price=85.0, ema200=110.0)
        bars = _bars([100.0] * 15 + [85.0])
        status = ss.check_confirmation(
            self._armed_breakdown(), snap, bars, regime="crisis", tape_deteriorating=False,
            rs_vs_market=0.85, rs_prior=None, days_since_armed=1,
        )
        assert status == "expired"

    def test_fade_top_confirms_using_its_own_family_gate(self):
        snap = _snap(price=85.0, ema200=80.0)  # still above its EMA200 (fade-top requirement)
        bars = _bars([100.0] * 15 + [85.0])
        status = ss.check_confirmation(
            self._armed_fade_top(), snap, bars, regime="neutral", tape_deteriorating=True,
            rs_vs_market=None, rs_prior=1.15, days_since_armed=1,
        )
        assert status == "confirmed"


class TestArmConfirmFinalizeEndToEnd:
    """Full flow across simulated days: arm -> pending -> confirmed -> a real signal."""

    def test_full_flow_arms_waits_then_confirms_and_finalizes(self):
        arm_snap = _snap(
            price=100.0, atr=2.0, rsi=38.0, rsi_slope=-1.0, macd=(-1.0, -0.5, -0.5),
            macd_hist_slope=-0.5, adx=(30.0, 12.0, 28.0), stochastic=(40.0, 45.0),
            bollinger=(105.0, 100.0, 95.0), obv=(1000.0, -5.0), ema200=110.0,
            volume_ratio=1.6, ofi_ratio=0.45, atr_percentile=0.5, supports=[95.0],
        )
        arm_bars = _bars([100.0] * 24 + [94.0])
        trigger = ss.try_arm_short(arm_snap, arm_bars, regime="bear", rs_vs_market=0.85)
        assert trigger == "support_breakdown"
        armed = ss.ArmedShort(symbol="TESTZ", category="stocks_large",
                              family=ss.family_of_trigger(trigger), trigger=trigger,
                              armed_date=date(2026, 9, 15))

        # Day +1: a bounce back above the (lagging) 10ema -> not confirmed yet -> pending.
        # (A flat continuation at the drop level would already read as "confirmed" here since
        # the EMA lags a level shift — that's correct EMA behavior, not what this step tests.)
        pending_snap = _snap(price=102.0, ema200=110.0)
        pending_bars = _bars([100.0] * 24 + [94.0, 102.0])
        status = ss.check_confirmation(
            armed, pending_snap, pending_bars, regime="bear", tape_deteriorating=False,
            rs_vs_market=0.85, rs_prior=None, days_since_armed=1,
        )
        assert status == "pending"

        # Day +2: breakdown resumes, closing below its 10ema -> confirmed.
        confirm_snap = _snap(
            price=90.0, atr=2.0, rsi=38.0, rsi_slope=-1.0, macd=(-1.0, -0.5, -0.5),
            macd_hist_slope=-0.5, adx=(30.0, 12.0, 28.0), stochastic=(40.0, 45.0),
            bollinger=(95.0, 90.0, 85.0), obv=(1000.0, -5.0), ema200=110.0,
            volume_ratio=1.6, ofi_ratio=0.45, atr_percentile=0.5, supports=[85.0],
        )
        confirm_bars = _bars([100.0] * 24 + [94.0, 102.0, 90.0])
        status = ss.check_confirmation(
            armed, confirm_snap, confirm_bars, regime="bear", tape_deteriorating=False,
            rs_vs_market=0.85, rs_prior=None, days_since_armed=2,
        )
        assert status == "confirmed"

        sig = ss.finalize_short_signal(
            armed, confirm_snap, confirm_bars, regime_id="bear", risk_tier="moderate",
            preferred_risk="moderate", sentiment_avg=None, sentiment_count=0,
            today=date(2026, 9, 17),
        )
        assert sig is not None
        assert sig.trigger == "support_breakdown"
        # Entry is the CONFIRMATION day's price, not the arm day's.
        assert sig.entry == 90.0
        assert sig.stop_loss > sig.entry > sig.take_profit
