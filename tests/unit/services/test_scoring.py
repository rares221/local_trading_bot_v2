"""
Unit tests for the shared pure scorer (app/services/recommendations/scoring.py).

These cover apply_signal_bonuses and compute_soft_penalties directly (the engine golden
test covers them in-context). Behavior is the extracted-verbatim engine logic.
"""
from datetime import date

import pytest

from app.services.recommendations.scoring import (
    apply_ofi_gate,
    apply_signal_bonuses,
    compute_soft_penalties,
    is_rsi_exhausted,
    rsi_exhaustion_ceiling,
)
from app.services.technicals import TechnicalSnapshot

_MID_MONTH = date(2026, 9, 15)  # no turn-of-month bonus


def _snap(**kw) -> TechnicalSnapshot:
    return TechnicalSnapshot(symbol="TEST", price=kw.pop("price", 100.0), **kw)


class TestApplySignalBonuses:
    def test_stacked_long_bonuses(self):
        # macd_rsi(+6) + volume_surge(+5) + rsi_momentum(+4) + triple_confluence(+5)
        # + ema200_sweet_spot(+3) = +23 over base 50.
        snap = _snap(
            price=100.0, macd=(1.0, 0.5, 0.5), rsi=55.0, rsi_slope=1.0,
            volume_ratio=1.6, atr_percentile=0.5, ema200=95.0, pct_from_52w_high=0.20,
        )
        conf, notes = apply_signal_bonuses(50, snap, "long", _MID_MONTH)
        assert conf == 73
        assert notes == [
            "macd_rsi_aligned=+6",
            "volume_surge=+5 (1.60x)",
            "rsi_momentum=+4 (slope=+1.0)",
            "triple_confluence=+5",
            "ema200_sweet_spot=+3 (5.3% above)",
        ]

    def test_stepwise_clamp_at_100(self):
        # Only macd_rsi (+6) applies; from 98 it clamps to 100, not 104.
        snap = _snap(macd=(1.0, 0.5, 0.5), rsi=55.0)
        conf, notes = apply_signal_bonuses(98, snap, "long", _MID_MONTH)
        assert conf == 100
        assert notes == ["macd_rsi_aligned=+6"]

    def test_turn_of_month_applies_without_snapshot(self):
        conf, notes = apply_signal_bonuses(50, None, "long", date(2026, 9, 1))
        assert conf == 52
        assert notes == ["turn_of_month=+2"]

    def test_no_bonuses(self):
        conf, notes = apply_signal_bonuses(50, None, "long", _MID_MONTH)
        assert conf == 50
        assert notes == []


class TestComputeSoftPenalties:
    def test_adx_and_cap_tier(self):
        snap = _snap(adx=(15.0, 10.0, 12.0))  # ADX < 20
        total, notes = compute_soft_penalties(snap, "long", "stocks_small", "moderate", 0)
        assert total == 14  # adx 8 + cap_tier(small) 6
        assert notes == ["adx_weak_trend=-8", "cap_tier=-6"]

    def test_pool_capped_at_25(self):
        # Velocity penalty alone exceeds the cap.
        total, notes = compute_soft_penalties(None, "long", "other", "moderate", 30)
        assert total == 25
        assert notes == ["sentiment_fading=-30"]

    def test_no_penalties(self):
        snap = _snap(adx=(30.0, 28.0, 12.0), macd=(1.0, 0.5, 0.5), macd_hist_slope=0.5,
                     rsi=55.0, rsi_slope=1.0, volume_ratio=1.6)
        total, notes = compute_soft_penalties(snap, "long", "other", "moderate", 0)
        assert total == 0
        assert notes == []

    def test_volume_penalty_stock_only(self):
        low_vol = _snap(volume_ratio=0.5)
        # Non-stock category: no volume penalty.
        assert compute_soft_penalties(low_vol, "long", "other", "moderate", 0)[0] == 0
        # Stock category below the moderate floor (0.75): -6.
        total, notes = compute_soft_penalties(low_vol, "long", "stocks_large", "moderate", 0)
        assert total == 6
        assert notes == ["volume_low=-6"]


class TestRsiExhaustion:
    @pytest.mark.parametrize(
        "regime_id,preferred,expected",
        [
            (None, "moderate", (65, 35)),
            ("bull", "moderate", (70, 30)),
            ("bear", "moderate", (60, 40)),        # 65 capped to 60
            ("high_vol", "moderate", (60, 40)),
            ("bull", "aggressive", (75, 25)),      # 65+5+5, clamp 75
            ("bear", "aggressive", (60, 40)),      # 70 capped to 60
            (None, "conservative", (63, 37)),
        ],
    )
    def test_ceiling(self, regime_id, preferred, expected):
        assert rsi_exhaustion_ceiling(regime_id, preferred) == expected

    def test_long_exhausted_respects_regime(self):
        hot = _snap(rsi=68.0)
        assert is_rsi_exhausted(hot, "long", "bull", "moderate") is False   # ceiling 70
        assert is_rsi_exhausted(_snap(rsi=63.0), "long", "bear", "moderate") is True  # ceiling 60
        assert is_rsi_exhausted(_snap(rsi=90.0), "long", "bull", "moderate") is True

    def test_short_floor(self):
        assert is_rsi_exhausted(_snap(rsi=25.0), "short", "bull", "moderate") is True   # floor 30
        assert is_rsi_exhausted(_snap(rsi=35.0), "short", "bull", "moderate") is False

    def test_no_rsi_never_exhausted(self):
        assert is_rsi_exhausted(_snap(), "long", "bull", "moderate") is False
        assert is_rsi_exhausted(None, "long", "bull", "moderate") is False


class TestOfiGate:
    def test_long_strong_distribution_skips(self):
        conf, skip = apply_ofi_gate(60, _snap(ofi_ratio=0.30), "long")
        assert skip is True

    def test_long_mild_distribution_penalizes(self):
        conf, skip = apply_ofi_gate(60, _snap(ofi_ratio=0.38), "long")
        assert (conf, skip) == (55, False)

    def test_long_accumulation_rewards(self):
        conf, skip = apply_ofi_gate(60, _snap(ofi_ratio=0.65), "long")
        assert (conf, skip) == (63, False)

    def test_short_strong_accumulation_skips(self):
        conf, skip = apply_ofi_gate(60, _snap(ofi_ratio=0.70), "short")
        assert skip is True

    def test_none_ofi_no_effect(self):
        assert apply_ofi_gate(60, _snap(), "long") == (60, False)
