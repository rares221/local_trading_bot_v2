"""
Regression tests for the parity hard-gates wired into live_analyzer._compute_signal_sync.

Live previously emitted signals the batch engine would reject (no RSI-exhaustion gate, no OFI
gate). These pin the wired behavior (2026-09-17): RSI exhaustion (regime + preference aware) and
the OFI gate + nudge now apply in the live path, matching engine/scoring.py.
"""
import app.services.technicals as tech
from app.live_trading import live_analyzer as la
from app.services.technicals import TechnicalSnapshot


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


def _signal(monkeypatch, snapshot, regime="bull", preferred_risk="moderate"):
    # _compute_signal_sync imports build_snapshot from app.services.technicals at call time.
    monkeypatch.setattr(tech, "build_snapshot", lambda symbol: snapshot)
    return la._compute_signal_sync(
        "TESTA", "TESTA", "moderate", "indices",
        0.0, 0, regime, None, None, None, preferred_risk,
    )


def test_healthy_long_emits(monkeypatch):
    # Option A: live confidence == engine pipeline (compute_confidence base + soft-penalties +
    # bonuses) = 83 for this setup, independent of backtest stats.
    r = _signal(monkeypatch, _snap(rsi=55.0))
    assert r is not None and r["direction"] == "long" and r["confidence"] == 83


def test_confidence_independent_of_backtest_stats(monkeypatch):
    # The gating confidence no longer depends on historical win rate — that moves to the
    # display-only overlay. Same setup, wildly different stats → same confidence.
    r_none = _signal(monkeypatch, _snap(rsi=55.0))
    tech.build_snapshot = lambda symbol: _snap(rsi=55.0)
    r_good = la._compute_signal_sync(
        "TESTA", "TESTA", "moderate", "indices", 0.0, 0, "bull",
        {"n": 50, "win_rate": 0.65}, None, None, "moderate",
    )
    assert r_none["confidence"] == r_good["confidence"] == 83
    # ...but the overlay reflects the stats.
    assert r_good["historical_confidence"] == 70
    assert r_none["historical_confidence"] == 42


def test_overbought_long_gated_by_rsi_exhaustion(monkeypatch):
    # RSI 90 > bull ceiling 70 -> hard skip (previously emitted at conf 28).
    assert _signal(monkeypatch, _snap(rsi=90.0)) is None


def test_strong_distribution_long_gated_by_ofi(monkeypatch):
    # OFI 0.30 < 0.35 on a long -> hard skip (previously emitted).
    assert _signal(monkeypatch, _snap(ofi_ratio=0.30)) is None


def test_mild_distribution_applies_ofi_nudge(monkeypatch):
    # OFI 0.38 (< 0.4) nudges the engine-pipeline confidence -5: 83 -> 78.
    r = _signal(monkeypatch, _snap(ofi_ratio=0.38))
    assert r is not None and r["confidence"] == 78


def test_rsi_ceiling_is_regime_aware(monkeypatch):
    # RSI 68 clears the bull ceiling (70) but bear RSI 63 is capped out (ceiling 60).
    assert _signal(monkeypatch, _snap(rsi=68.0), regime="bull") is not None
    assert _signal(monkeypatch, _snap(rsi=63.0), regime="bear") is None
