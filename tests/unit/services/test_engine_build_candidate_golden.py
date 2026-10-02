"""
Characterization ("golden") test for engine._build_candidate — the Phase 1 safety net.

Phase 1 extracts the confidence-adjustment + hard-gate pipeline out of _build_candidate
into a shared app/services/recommendations/scoring.py. This test pins the CURRENT output
(emitted Candidate fields + the skip/None decisions) for a set of fixtures so that
extraction can be proven behavior-preserving: these assertions must stay green byte-for-byte
across the refactor.

Design of the harness: passing market_ctx=None guards out the entire regime / sector / DXY /
correlation block and the bear-short gates, and a non-stock category ("other") skips the
earnings + relative-strength blocks — collapsing the IO surface to a handful of collaborators,
which are monkeypatched to deterministic values. Date is frozen to a mid-month day so the
turn-of-month bonus does not fire.

Golden values captured 2026-09-17 against engine.py on branch feature/shorts_fix.
"""
import datetime as _dt
import types

import pytest

import app.services.recommendations.engine as eng
import app.core.preferences as prefs
from app.services.market_context import TrendAlignment
from app.services.technicals import TechnicalSnapshot


class _FakeDate(_dt.date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 15)  # mid-month: no turn-of-month bonus


@pytest.fixture
def patched_engine(monkeypatch):
    """Deterministic IO surface for _build_candidate with market_ctx=None."""
    monkeypatch.setattr(eng, "date", _FakeDate)
    # score_candidate's `today` comes from market_date() (US/Eastern calendar date), not
    # date.today() -- must freeze this too or the turn-of-month bonus leaks the real wall-clock
    # date through on the 1st-3rd / last 2 days of any month the suite happens to run on.
    monkeypatch.setattr(eng, "market_date", lambda: _FakeDate(2026, 9, 15))
    monkeypatch.setattr(prefs, "get_preferred_risk", lambda: "moderate")
    monkeypatch.setattr(
        eng, "check_trend_alignment",
        lambda symbol: TrendAlignment(ema_stack="mixed", aligned=False, alignment_score=0.0),
    )
    monkeypatch.setattr(
        eng, "get_sentiment_velocity_and_conviction", lambda *a, **k: (None, None)
    )
    return monkeypatch


def _snap(**kw) -> TechnicalSnapshot:
    base = dict(
        symbol="TESTA", price=100.0, atr=2.0, rsi=55.0, rsi_slope=1.0,
        macd=(1.0, 0.5, 0.5), macd_hist_slope=0.5, adx=(30.0, 28.0, 12.0),
        stochastic=(60.0, 55.0), bollinger=(105.0, 100.0, 95.0), obv=(1000.0, 5.0),
        ema200=95.0, volume_ratio=1.6, ofi_ratio=0.55, atr_percentile=0.5,
        pct_from_52w_high=0.20, volatility_regime="stable",
    )
    base.update(kw)
    return TechnicalSnapshot(**base)


def _build(monkeypatch, snapshot, *, ad_hoc=False):
    monkeypatch.setattr(eng, "build_snapshot", lambda symbol: snapshot)
    return eng._build_candidate(
        "other", "TESTA", "TESTA", "moderate", {}, market_ctx=None, ad_hoc=ad_hoc,
    )


class TestBuildCandidateGolden:
    def test_emit_long_adhoc(self, patched_engine):
        # Clean bullish long. Bare ATR levels give R:R 1.4 (< MIN_RISK_REWARD 2.0), so the
        # normal path skips; ad_hoc lowers the floor to 1.7 and stretches TP to meet it.
        c = _build(patched_engine, _snap(), ad_hoc=True)
        assert c is not None
        assert c.direction == "long"
        assert c.confidence_score == 83
        assert round(c.stop_loss, 4) == 95.0
        assert round(c.take_profit, 4) == 108.53
        assert round(c.risk_reward, 4) == 1.71
        assert round(c._score, 4) == 1.15

    def test_skip_low_rr_non_adhoc(self, patched_engine):
        # Same snapshot without ad_hoc: R:R 1.4 < 2.0 -> hard skip.
        assert _build(patched_engine, _snap()) is None

    def test_skip_rsi_exhaustion(self, patched_engine):
        # RSI 90 above the long ceiling (~65) -> hard skip before scoring completes.
        assert _build(patched_engine, _snap(rsi=90.0)) is None

    def test_skip_ofi_distribution(self, patched_engine):
        # OFI 0.30 < 0.35 on a long (strong distribution) -> hard skip.
        assert _build(patched_engine, _snap(ofi_ratio=0.30)) is None

    def test_skip_short_disabled(self, patched_engine):
        # Bearish snapshot -> determine_direction returns short; SHORTS_ENABLED=False and
        # market_ctx=None means the bear-regime exception can't apply -> hard skip. Pins the
        # current shorts-disabled behavior.
        bearish = _snap(
            price=100.0, rsi=42.0, macd=(-1.0, -0.5, -0.5), adx=(30.0, 12.0, 28.0),
            stochastic=(40.0, 45.0), obv=(1000.0, -5.0), macd_hist_slope=-0.5, ema200=105.0,
        )
        assert _build(patched_engine, bearish) is None


def _ctx(extended_regime, three_label="cautious"):
    """Stub MarketContext exercising the regime/dxy scoring block deterministically."""
    regime = types.SimpleNamespace(
        description="desc", regime=three_label, extended_regime=extended_regime,
    )
    return types.SimpleNamespace(
        regime=regime,
        sector_regimes={},
        has_divergence_warning=False,
        dxy=types.SimpleNamespace(trend="up"),
        regime_adjustment=lambda: 0,
        divergence_adjustment=lambda: 0,
        dxy_confidence_delta=lambda c, a, d: 0,
        dxy_note_for_asset=lambda c, a, d: None,
    )


def _build_mc(monkeypatch, snapshot, ctx, *, ad_hoc=True, category="indices"):
    monkeypatch.setattr(eng, "build_snapshot", lambda symbol: snapshot)
    monkeypatch.setattr(eng, "get_sector_for_asset", lambda asset_id, category: None)
    return eng._build_candidate(
        category, "TESTA", "TESTA", "moderate", {}, market_ctx=ctx, ad_hoc=ad_hoc,
    )


class TestBuildCandidateMarketCtxGolden:
    """Pins the market_ctx-driven scoring path (regime block + regime-aware RSI ceiling).

    Also the regression guard for the 2026-09-17 fix: post-Phase-0 the RSI-exhaustion ceiling
    (engine.py:1275/1281) now reads the canonical 5-label — bull widens the long ceiling to 70,
    defensive regimes (bear/high_vol/crisis) cap it at 60. Before the fix these compared the
    5-label against the dead 3-label strings and never matched.
    """

    def test_bull_healthy_rsi_emits(self, patched_engine):
        c = _build_mc(patched_engine, _snap(rsi=55.0), _ctx("bull", "risk_on"))
        assert c is not None and c.direction == "long"
        assert c.confidence_score == 83

    def test_bull_rsi68_emits_ceiling_widened_to_70(self, patched_engine):
        # RSI 68 clears the widened bull ceiling (70). Pre-fix (ceiling stuck at 65) this skipped.
        c = _build_mc(patched_engine, _snap(rsi=68.0), _ctx("bull", "risk_on"))
        assert c is not None and c.confidence_score == 73

    def test_bear_rsi63_skips_ceiling_capped_at_60(self, patched_engine):
        # The safety-critical direction: RSI 63 emits in bull but is capped out in bear (ceiling 60).
        assert _build_mc(patched_engine, _snap(rsi=63.0), _ctx("bull", "risk_on")) is not None
        assert _build_mc(patched_engine, _snap(rsi=63.0), _ctx("bear", "risk_off")) is None

    def test_bear_rsi68_skips(self, patched_engine):
        assert _build_mc(patched_engine, _snap(rsi=68.0), _ctx("bear", "risk_off")) is None

    def test_crisis_hard_block(self, patched_engine):
        # crisis/high_vol are hard-blocked before scoring when market_ctx is present.
        assert _build_mc(patched_engine, _snap(rsi=55.0), _ctx("crisis")) is None
