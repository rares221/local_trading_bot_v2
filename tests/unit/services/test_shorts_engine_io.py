"""
Unit tests for the shorts engine IO/adapter glue (batch integration, Phase 2 two-stage entry).
See docs/superpowers/plans/2026-09-18-shorts-engine.md.

The four DB helpers (_load_pending_arm / _insert_arm / _mark_arm_resolved /
_increment_arm_pending) are monkeypatched directly in every test — the unit suite stubs
SQLAlchemy globally (see tests/conftest.py), so real DB access is never exercised here. A
real-DB smoke test would need the same "restore the real ORM" pattern used in
tests/unit/jobs/test_scheduler_daily_batch.py; not needed for this module's own logic.
"""
from datetime import date, timedelta

import pandas as pd

from app.services.recommendations import shorts_engine_io as sio
from app.services.recommendations.shorts_signals import ShortSignal


def _mock_snap_and_fetch(monkeypatch):
    class _Snap:
        price = 100.0
    monkeypatch.setattr(sio, "build_snapshot", lambda s: _Snap())
    monkeypatch.setattr(sio, "fetch_ohlcv", lambda s, **kw: pd.DataFrame({"Close": [1.0] * 80}))
    monkeypatch.setattr(sio, "compute_relative_strength", lambda sym, bench, period=20: 1.0)


# ── compute_tape_deteriorating ───────────────────────────────────────────────

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


# ── short_signal_to_candidate ────────────────────────────────────────────────

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


# ── build_short_candidates_for_category: wrong category / no pending arms ───

def test_build_short_candidates_wrong_category_empty(monkeypatch):
    monkeypatch.setattr(sio, "build_snapshot", lambda s: None)
    out = sio.build_short_candidates_for_category(
        "commodities", regime="bear", tape_deteriorating=False,
        exclude_taken=None, today=date(2026, 9, 18))
    assert out == []


# ── Phase 2: new arms are recorded, NOT immediately turned into candidates ──

def test_new_trigger_inserts_an_arm_and_produces_no_candidate_yet(monkeypatch):
    _mock_snap_and_fetch(monkeypatch)
    monkeypatch.setattr(sio, "_load_pending_arm", lambda symbol: None)  # nothing armed yet
    inserted = []
    monkeypatch.setattr(
        sio, "_insert_arm",
        lambda symbol, category, family, trigger, armed_date: inserted.append(
            (symbol, category, family, trigger, armed_date)),
    )

    def fake_arm(snap, bars, regime, tape_deteriorating=False, rs_vs_market=None, rs_prior=None):
        return "support_breakdown" if regime == "bear" else None
    monkeypatch.setattr(sio, "try_arm_short", fake_arm)

    out = sio.build_short_candidates_for_category(
        "stocks_large", regime="bear", tape_deteriorating=False,
        exclude_taken=None, today=date(2026, 9, 18))

    assert out == []  # arming produces no candidate this cycle
    assert len(inserted) == 15  # every stocks_large symbol armed (fake_arm ignores which one)
    assert inserted[0][2] == "breakdown"  # family derived correctly from the trigger
    assert inserted[0][3] == "support_breakdown"


def test_excluded_symbol_does_not_get_a_new_arm(monkeypatch):
    _mock_snap_and_fetch(monkeypatch)
    monkeypatch.setattr(sio, "_load_pending_arm", lambda symbol: None)
    inserted = []
    monkeypatch.setattr(
        sio, "_insert_arm",
        lambda symbol, category, family, trigger, armed_date: inserted.append(symbol),
    )
    monkeypatch.setattr(sio, "try_arm_short", lambda *a, **kw: "support_breakdown")

    sio.build_short_candidates_for_category(
        "stocks_large", regime="bear", tape_deteriorating=False,
        exclude_taken={("stocks_large", "AAPL")}, today=date(2026, 9, 18))

    assert "AAPL" not in inserted
    assert "MSFT" in inserted  # not excluded, still arms


# ── Phase 2: a confirmed pending arm becomes a Candidate ────────────────────

def test_confirmed_pending_arm_produces_a_candidate_and_marks_resolved(monkeypatch):
    _mock_snap_and_fetch(monkeypatch)

    def fake_load(symbol):
        if symbol == "AAPL":
            return {"id": 1, "family": "breakdown", "trigger": "support_breakdown",
                    "armed_date": date(2026, 9, 15), "days_since_armed": 1}
        return None
    monkeypatch.setattr(sio, "_load_pending_arm", fake_load)
    monkeypatch.setattr(sio, "check_confirmation", lambda *a, **kw: "confirmed")
    resolved = []
    monkeypatch.setattr(sio, "_mark_arm_resolved", lambda row_id, status: resolved.append((row_id, status)))
    monkeypatch.setattr(
        sio, "finalize_short_signal",
        lambda armed, *a, **kw: ShortSignal(
            armed.symbol, armed.trigger, "short", 70, 100.0, 105.0, 90.0, 2.0,
        ) if armed.symbol == "AAPL" else None,
    )

    out = sio.build_short_candidates_for_category(
        "stocks_large", regime="bear", tape_deteriorating=False,
        exclude_taken=None, today=date(2026, 9, 18))

    ids = [c.asset_id for c in out]
    assert ids == ["AAPL"]
    assert out[0].direction == "short"
    assert (1, "confirmed") in resolved


def test_confirmed_but_excluded_symbol_still_resolves_without_a_candidate(monkeypatch):
    _mock_snap_and_fetch(monkeypatch)
    monkeypatch.setattr(
        sio, "_load_pending_arm",
        lambda symbol: {"id": 1, "family": "breakdown", "trigger": "support_breakdown",
                         "armed_date": date(2026, 9, 15), "days_since_armed": 1}
        if symbol == "AAPL" else None,
    )
    monkeypatch.setattr(sio, "check_confirmation", lambda *a, **kw: "confirmed")
    resolved = []
    monkeypatch.setattr(sio, "_mark_arm_resolved", lambda row_id, status: resolved.append((row_id, status)))
    monkeypatch.setattr(
        sio, "finalize_short_signal",
        lambda armed, *a, **kw: ShortSignal(armed.symbol, armed.trigger, "short", 70, 100.0, 105.0, 90.0, 2.0),
    )

    out = sio.build_short_candidates_for_category(
        "stocks_large", regime="bear", tape_deteriorating=False,
        exclude_taken={("stocks_large", "AAPL")}, today=date(2026, 9, 18))

    assert out == []  # excluded -- no candidate surfaced
    assert (1, "confirmed") in resolved  # but still marked resolved (bookkeeping happens either way)


# ── Phase 2: an expired pending arm is dropped and marked resolved ──────────

def test_expired_pending_arm_produces_no_candidate_and_marks_resolved(monkeypatch):
    _mock_snap_and_fetch(monkeypatch)
    monkeypatch.setattr(
        sio, "_load_pending_arm",
        lambda symbol: {"id": 2, "family": "breakdown", "trigger": "support_breakdown",
                         "armed_date": date(2026, 9, 15), "days_since_armed": 3}
        if symbol == "AAPL" else None,
    )
    monkeypatch.setattr(sio, "check_confirmation", lambda *a, **kw: "expired")
    resolved = []
    monkeypatch.setattr(sio, "_mark_arm_resolved", lambda row_id, status: resolved.append((row_id, status)))

    out = sio.build_short_candidates_for_category(
        "stocks_large", regime="bear", tape_deteriorating=False,
        exclude_taken=None, today=date(2026, 9, 18))

    assert out == []
    assert (2, "expired") in resolved


# ── Phase 2: a still-pending arm just increments, no candidate, no new arm ──

def test_still_pending_arm_increments_and_does_not_rearm(monkeypatch):
    _mock_snap_and_fetch(monkeypatch)
    monkeypatch.setattr(
        sio, "_load_pending_arm",
        lambda symbol: {"id": 3, "family": "breakdown", "trigger": "support_breakdown",
                         "armed_date": date(2026, 9, 15), "days_since_armed": 1}
        if symbol == "AAPL" else None,
    )
    monkeypatch.setattr(sio, "check_confirmation", lambda *a, **kw: "pending")
    incremented = []
    monkeypatch.setattr(sio, "_increment_arm_pending", lambda row_id: incremented.append(row_id))
    arm_attempts = []
    monkeypatch.setattr(sio, "try_arm_short", lambda *a, **kw: arm_attempts.append(1) or None)

    out = sio.build_short_candidates_for_category(
        "stocks_large", regime="bear", tape_deteriorating=False,
        exclude_taken=None, today=date(2026, 9, 18))

    assert out == []
    assert incremented == [3]
    # 15 stocks_large symbols total; AAPL is already armed (pending) so try_arm_short is only
    # attempted for the other 14 -- confirms an already-armed symbol is skipped, not re-armed.
    stocks_large_count = sum(1 for (cat, *_r) in sio.SHORT_UNIVERSE if cat == "stocks_large")
    assert len(arm_attempts) == stocks_large_count - 1
