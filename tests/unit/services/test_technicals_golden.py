"""
Characterization ("golden") tests for the core technicals scoring primitives:
compute_atr_levels, determine_direction, compute_confidence.

These pin the CURRENT behavior byte-for-byte so the Phase 1 refactor (extracting a
shared scorer used by engine/replay/live) can be proven behavior-preserving. The exact
numbers are captured from the live implementation, not hand-derived — if a change moves
them, that is a deliberate decision to re-bless, not an incidental drift.

Golden values captured 2026-09-17 against technicals.py on branch feature/shorts_fix.
"""
import pytest

from app.services.technicals import (
    TechnicalSnapshot,
    compute_atr_levels,
    determine_direction,
    compute_confidence,
)


def _snap(**kw) -> TechnicalSnapshot:
    return TechnicalSnapshot(symbol="TEST", price=kw.pop("price", 100.0), **kw)


# A cleanly bullish snapshot: RSI healthy, MACD hist +, +DI>-DI strong trend,
# stochastic mid-high, price at BB mid, OBV rising. No VP/SR levels (kept None/empty)
# so compute_atr_levels exercises the pure ATR path.
def _bull_snap() -> TechnicalSnapshot:
    return _snap(price=100.0, rsi=58.0, macd=(1.0, 0.5, 0.5), adx=(30.0, 28.0, 12.0),
                 stochastic=(60.0, 55.0), bollinger=(105.0, 100.0, 95.0), obv=(1000.0, 5.0))


def _bear_snap() -> TechnicalSnapshot:
    return _snap(price=100.0, rsi=42.0, macd=(-1.0, -0.5, -0.5), adx=(30.0, 12.0, 28.0),
                 stochastic=(40.0, 45.0), bollinger=(105.0, 100.0, 95.0), obv=(1000.0, -5.0))


# ---------------------------------------------------------------------------
# compute_atr_levels
# ---------------------------------------------------------------------------

class TestComputeAtrLevels:
    @pytest.mark.parametrize(
        "risk_tier,direction,expected_sl,expected_tp",
        [
            ("moderate", "long", 98.0, 103.0),      # 100*(1-0.02), 100*(1+0.03)
            ("moderate", "short", 102.0, 97.0),     # 100*(1+0.02), 100*(1-0.03)
            ("conservative", "long", 98.5, 102.5),  # 0.015 / 0.025
            ("aggressive", "long", 97.0, 105.0),    # 0.03 / 0.05
        ],
    )
    def test_fallback_no_snapshot(self, risk_tier, direction, expected_sl, expected_tp):
        sl, tp = compute_atr_levels(100.0, risk_tier, None, direction)
        assert (round(sl, 4), round(tp, 4)) == (expected_sl, expected_tp)

    def test_atr_snapshot_long_moderate(self):
        # atr=2, moderate mult (2.5, 3.5), stable regime, no ADX widening.
        sl, tp = compute_atr_levels(100.0, "moderate", _snap(price=100.0, atr=2.0), "long")
        assert (round(sl, 4), round(tp, 4)) == (95.0, 107.0)

    def test_atr_snapshot_short_moderate(self):
        sl, tp = compute_atr_levels(100.0, "moderate", _snap(price=100.0, atr=2.0), "short")
        assert (round(sl, 4), round(tp, 4)) == (105.0, 93.0)

    def test_side_invariants(self):
        # Structural intent: long SL below entry & TP above; short SL above & TP below.
        s = _snap(price=100.0, atr=2.0)
        lsl, ltp = compute_atr_levels(100.0, "moderate", s, "long")
        ssl, stp = compute_atr_levels(100.0, "moderate", s, "short")
        assert lsl < 100.0 < ltp
        assert stp < 100.0 < ssl


# ---------------------------------------------------------------------------
# determine_direction
# ---------------------------------------------------------------------------

class TestDetermineDirection:
    @pytest.mark.parametrize("regime", ["bull", "bear", "neutral"])
    def test_bullish_snapshot_is_long(self, regime):
        assert determine_direction(0.5, _bull_snap(), regime=regime) == "long"

    @pytest.mark.parametrize("regime", ["bull", "bear", "neutral"])
    def test_bearish_snapshot_is_short(self, regime):
        assert determine_direction(-0.5, _bear_snap(), regime=regime) == "short"

    def test_sentiment_only_no_snapshot(self):
        assert determine_direction(0.8, None, regime="bull") == "long"
        assert determine_direction(-0.8, None, regime="bull") == "short"


# ---------------------------------------------------------------------------
# compute_confidence
# ---------------------------------------------------------------------------

class TestComputeConfidence:
    @pytest.mark.parametrize("regime", ["bull", "bear", "neutral"])
    def test_bull_snapshot_long(self, regime):
        # RSI 58 sits in the healthy band for all three regime profiles, so this
        # particular snapshot scores 71 regardless of regime — locked as-is.
        assert compute_confidence(0.5, 10, _bull_snap(), "long", regime) == 71

    def test_bear_snapshot_short(self):
        assert compute_confidence(-0.5, 10, _bear_snap(), "short", "bear") == 71

    def test_no_snapshot_sentiment_only(self):
        # sentiment 0.5 -> 7.5 pts, log1p(10)*3 -> 7.19 pts, over max 25 -> 58.
        assert compute_confidence(0.5, 10, None, "long", "bull") == 58

    def test_no_sentiment_no_snapshot_is_zero(self):
        assert compute_confidence(None, 0, None, "long", "bull") == 0

    def test_regime_shifts_score_at_rsi_boundary(self):
        # Intent guard for regime sensitivity: RSI 62 is inside the healthy band for
        # bull (OB=72) and neutral (OB=70) but ABOVE the bear overbought bar (OB=60),
        # so a long in a bear regime must not score higher than in bull/neutral.
        s = _snap(price=100.0, rsi=62.0, macd=(1.0, 0.5, 0.5), adx=(30.0, 28.0, 12.0),
                  stochastic=(60.0, 55.0), bollinger=(105.0, 100.0, 95.0), obv=(1000.0, 5.0))
        bull = compute_confidence(0.5, 10, s, "long", "bull")
        bear = compute_confidence(0.5, 10, s, "long", "bear")
        assert bear <= bull
