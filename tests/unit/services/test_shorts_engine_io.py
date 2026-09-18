"""
Unit tests for the shorts engine IO/adapter glue (batch integration).
See docs/superpowers/plans/2026-09-18-shorts-orchestrator-integration.md.
"""
from datetime import date, timedelta

import pandas as pd

from app.services.recommendations import shorts_engine_io as sio
from app.services.recommendations.shorts_signals import ShortSignal


# ── Task 2: compute_tape_deteriorating ───────────────────────────────────────

def test_compute_tape_deteriorating_uses_vix_and_spx(monkeypatch):
    def fake_fetch(symbol, **kw):
        if symbol == "^VIX":
            return pd.DataFrame({"Close": [15.0] * 20 + [18.0]})
        if symbol == "^GSPC":
            return pd.DataFrame({"Close": [100.0] * 20 + [95.0]})
        return None
    monkeypatch.setattr(sio, "fetch_ohlcv", fake_fetch)
    assert sio.compute_tape_deteriorating() is True


def test_compute_tape_deteriorating_false_when_missing(monkeypatch):
    monkeypatch.setattr(sio, "fetch_ohlcv", lambda symbol, **kw: None)
    assert sio.compute_tape_deteriorating() is False


# ── Task 3: short_signal_to_candidate ────────────────────────────────────────

def test_short_signal_to_candidate_maps_fields():
    sig = ShortSignal(symbol="AAPL", trigger="failed_retest", direction="short",
                      confidence=72, entry=100.0, stop_loss=105.0, take_profit=90.0, risk_reward=2.0)
    c = sio.short_signal_to_candidate(sig, category="stocks_large", asset_id="AAPL",
                                      risk_tier="moderate", today=date(2026, 9, 18))
    assert c.direction == "short"
    assert c.asset_category == "stocks_large" and c.asset_id == "AAPL"
    assert c.buy_in_price == 100.0 and c.stop_loss == 105.0 and c.take_profit == 90.0
    assert c.confidence_score == 72 and c.risk_reward == 2.0
    assert c.expiry_date == date(2026, 9, 18) + timedelta(days=14)
    assert c.suggested_risk_pct > 0
    assert "failed_retest" in (c.market_regime_note or "")


# ── Task 4: build_short_candidates_for_category ──────────────────────────────

def test_build_short_candidates_filters_category_and_ranks(monkeypatch):
    class _Snap:
        price = 100.0
    monkeypatch.setattr(sio, "build_snapshot", lambda s: _Snap())
    monkeypatch.setattr(sio, "fetch_ohlcv", lambda s, **kw: pd.DataFrame({"Close": [1.0] * 80}))

    def fake_gen(symbol, snap, bars, **kw):
        conf = {"AAPL": 80, "MSFT": 65}.get(symbol)
        if conf is None:
            return None
        return ShortSignal(symbol, "failed_retest", "short", conf, 100.0, 105.0, 90.0, 2.0)
    monkeypatch.setattr(sio, "generate_short_signal", fake_gen)

    out = sio.build_short_candidates_for_category(
        "stocks_large", regime="bear", tape_deteriorating=False,
        exclude_taken={("stocks_large", "MSFT")}, today=date(2026, 9, 18))
    ids = [c.asset_id for c in out]
    assert ids == ["AAPL"]  # MSFT excluded; only qualifying, ranked by confidence desc
    assert all(c.direction == "short" for c in out)


def test_build_short_candidates_wrong_category_empty(monkeypatch):
    monkeypatch.setattr(sio, "build_snapshot", lambda s: None)
    out = sio.build_short_candidates_for_category(
        "commodities", regime="bear", tape_deteriorating=False,
        exclude_taken=None, today=date(2026, 9, 18))
    assert out == []
