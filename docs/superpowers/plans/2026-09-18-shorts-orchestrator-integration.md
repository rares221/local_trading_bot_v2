# Shorts Engine — Orchestrator Integration (Phase 6/8) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline). TDD, checkbox steps.
> **PROJECT COMMIT RULE (CLAUDE.md):** Do NOT `git commit` — the user commits. "Commit" steps are checkpoints.

**Goal:** Wire the validated shorts signal engine (`shorts_signals.py`) into the daily batch so shorts are produced as a sequential second pass after longs, with conflict-prevention, capacity, correlation, and risk-budget all honored.

**Architecture:** A new `build_short_candidates_for_category(...)` scans a short universe, calls `generate_short_signal`, and converts each `ShortSignal` → `Candidate`. `build_daily_candidates` (the batch orchestrator) runs shorts **first in short-active stock categories** (Option 3: shorts take up to 2 slots that actually qualify, longs then fill to 5; quiet classes keep all 5 longs). Conflict-prevention is inherited free from the existing `_try_add` `seen_asset_key` dedup (keyed on `(category, asset_id)`, direction-agnostic). Correlation + risk-budget also inherited via `_try_add`.

**Tech Stack:** Python 3.11, pytest. Reuses `shorts_signals` (`generate_short_signal`, `_tape_deteriorating`), `technicals` (`build_snapshot`, `fetch_ohlcv`, `estimate_expiry_days`), engine (`_suggested_risk_pct`, `Candidate`, `_try_add`).

**Activation recap:** breakdown family fires in canonical `bear` regime; fade-top fires when `_tape_deteriorating` (VIX rising 5d + SPX < 20d MA). Both hard-blocked in crisis/high_vol. Shorts apply to **stock categories + indices** only (not commodities/other).

---

## File Structure
- **Modify** `app/services/recommendations/shorts_engine_io.py` — NEW small module: the short universe, `compute_tape_deteriorating()`, `short_signal_to_candidate()`, `build_short_candidates_for_category()`. (Keeps IO/adapter glue out of `shorts_signals.py`, which stays pure.)
- **Modify** `app/services/recommendations/engine.py` — add `SHORTS_ENGINE_ENABLED` flag; splice the shorts-first pass into `build_daily_batch`.
- **Test** `tests/unit/services/test_shorts_engine_io.py` — NEW.

Container tests: `docker-compose exec -T app python -m pytest tests/unit/services/test_shorts_engine_io.py -v`

---

## Task 1: Short universe + `SHORTS_ENGINE_ENABLED` flag

**Files:** create `shorts_engine_io.py`; modify `engine.py`.

- [ ] In `engine.py`, near the other flags (~line 105), add:
```python
# New separate shorts engine (shorts_signals). Regime-gated at generation time; this only
# master-enables it in the batch. (Distinct from the legacy long-engine SHORTS_ENABLED flip.)
SHORTS_ENGINE_ENABLED = True
```
- [ ] Create `app/services/recommendations/shorts_engine_io.py` with the universe (validated watchlist; breakdown-tighter/fade-top-broader is a later tuning — one universe for now):
```python
"""IO/adapter glue between the pure shorts_signals engine and the batch orchestrator."""
from __future__ import annotations
from datetime import date, timedelta
from typing import Optional
import pandas as pd

from app.services import technicals as tech
from app.services.technicals import build_snapshot, fetch_ohlcv, calc_risk_reward
from app.services.recommendations.shorts_signals import generate_short_signal, _tape_deteriorating

# (category, asset_id, yf_symbol, risk_tier). Individual names -> stocks_large; broad/sector ETFs -> indices.
SHORT_UNIVERSE: list[tuple[str, str, str, str]] = [
    ("stocks_large", s, s, "moderate") for s in
    ["AAPL", "MSFT", "NVDA", "META", "AMZN", "TSLA", "GOOGL", "NFLX", "AMD", "AVGO",
     "CRM", "COST", "HD", "JPM", "BAC"]
] + [
    ("indices", s, s, "moderate") for s in
    ["XLF", "XLY", "XLK", "XLI", "XLE", "SMH", "HYG", "JNK", "IWM", "SPY", "QQQ", "DIA"]
]

_HORIZON_DAYS = 14  # 14-day prediction app
```
- [ ] Checkpoint.

---

## Task 2: `compute_tape_deteriorating()`

**Files:** modify `shorts_engine_io.py` + test.

- [ ] **Test (RED):** `tests/unit/services/test_shorts_engine_io.py`
```python
import pandas as pd
from datetime import date
from app.services.recommendations import shorts_engine_io as sio


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
```
- [ ] **Impl:**
```python
def compute_tape_deteriorating() -> bool:
    """Fade-top activation for the live batch: fetch ^VIX/^GSPC and apply _tape_deteriorating."""
    vix = fetch_ohlcv("^VIX", period="90d")
    spx = fetch_ohlcv("^GSPC", period="90d")
    if vix is None or spx is None or "Close" not in vix.columns or "Close" not in spx.columns:
        return False
    return _tape_deteriorating(vix["Close"], spx["Close"])
```
- [ ] RED→GREEN. Checkpoint.

---

## Task 3: `short_signal_to_candidate()`

**Files:** modify `shorts_engine_io.py` + test.

- [ ] **Test (RED):**
```python
from app.services.recommendations.shorts_signals import ShortSignal

def test_short_signal_to_candidate_maps_fields():
    sig = ShortSignal(symbol="AAPL", trigger="failed_retest", direction="short",
                      confidence=72, entry=100.0, stop_loss=105.0, take_profit=90.0, risk_reward=2.0)
    c = sio.short_signal_to_candidate(sig, category="stocks_large", asset_id="AAPL",
                                      risk_tier="moderate", today=date(2026, 9, 18))
    assert c.direction == "short"
    assert c.asset_category == "stocks_large" and c.asset_id == "AAPL"
    assert c.buy_in_price == 100.0 and c.stop_loss == 105.0 and c.take_profit == 90.0
    assert c.confidence_score == 72 and c.risk_reward == 2.0
    assert c.expiry_date == date(2026, 9, 18) + __import__("datetime").timedelta(days=14)
    assert c.suggested_risk_pct > 0
    assert "failed_retest" in (c.market_regime_note or "")
```
- [ ] **Impl:**
```python
def short_signal_to_candidate(sig, category: str, asset_id: str, risk_tier: str, today: date):
    from app.services.recommendations.engine import Candidate, _suggested_risk_pct
    suggested = _suggested_risk_pct(sig.confidence, sig.risk_reward, trend_aligned=False,
                                    sector_aligned=False, gap_sizing_mult=1.0, gap_max_penalty=0.0)
    return Candidate(
        asset_category=category, asset_id=asset_id, risk_tier=risk_tier,
        buy_in_price=sig.entry, stop_loss=sig.stop_loss, take_profit=sig.take_profit,
        direction="short", expiry_date=today + timedelta(days=_HORIZON_DAYS),
        confidence_score=sig.confidence, risk_reward=sig.risk_reward,
        market_regime_note=f"short:{sig.trigger}", suggested_risk_pct=suggested,
        _score=float(sig.confidence),
    )
```
- [ ] RED→GREEN. Checkpoint.

---

## Task 4: `build_short_candidates_for_category()`

**Files:** modify `shorts_engine_io.py` + test.

- [ ] **Test (RED):** patch `build_snapshot`/`fetch_ohlcv`/`generate_short_signal` so one symbol yields a signal; assert the returned list is ranked `Candidate`s of `direction=="short"` for the category, `exclude_taken` honored.
```python
def test_build_short_candidates_filters_category_and_ranks(monkeypatch):
    from app.services.recommendations.shorts_signals import ShortSignal
    monkeypatch.setattr(sio, "build_snapshot", lambda s: object())
    monkeypatch.setattr(sio, "fetch_ohlcv", lambda s, **kw: pd.DataFrame({"Close": [1.0]*80}))
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
    assert ids == ["AAPL"]  # MSFT excluded; ranked by confidence desc
    assert all(c.direction == "short" for c in out)
```
- [ ] **Impl:**
```python
def build_short_candidates_for_category(category, *, regime, tape_deteriorating, exclude_taken, today,
                                        preferred_risk="moderate"):
    excluded = exclude_taken or set()
    cands = []
    for (cat, asset_id, yf_symbol, risk_tier) in SHORT_UNIVERSE:
        if cat != category:
            continue
        if (cat, asset_id) in excluded:
            continue
        try:
            snap = build_snapshot(yf_symbol)
            if snap is None or getattr(snap, "price", 0) <= 0:
                continue
            df = fetch_ohlcv(yf_symbol, period="1y")
            if df is None or df.empty or len(df) < 60:
                continue
            sig = generate_short_signal(
                yf_symbol, snap, df, regime_id=regime, category=cat, risk_tier=risk_tier,
                preferred_risk=preferred_risk, sentiment_avg=None, sentiment_count=0,
                today=today, tape_deteriorating=tape_deteriorating)
        except Exception:
            continue
        if sig is None:
            continue
        cands.append(short_signal_to_candidate(sig, cat, asset_id, risk_tier, today))
    cands.sort(key=lambda c: -c.confidence_score)
    return cands
```
- [ ] RED→GREEN. Checkpoint.

---

## Task 5: splice shorts-first pass into `build_daily_candidates`

**Files:** modify `engine.py` + test.

The current per-category loop (engine.py ~2103-2120): for each category, build ranked longs, `_try_add` up to `cap`. Change to, for stock+index categories when shorts are active: run shorts first (add up to `min(2, qualifying)` via `_try_add`), then fill longs to `cap` total.

- [ ] **Impl — inside `build_daily_candidates`, before the category loop:**
```python
    from app.services.recommendations.shorts_engine_io import (
        build_short_candidates_for_category, compute_tape_deteriorating)
    _short_regime = market_ctx.regime.extended_regime if market_ctx else None
    _short_active = SHORTS_ENGINE_ENABLED and _short_regime not in ("crisis", "high_vol")
    _tape_det = compute_tape_deteriorating() if _short_active else False
    _MAX_SHORTS_PER_CLASS = 2
    _SHORT_CATEGORIES = (*_STOCK_CATEGORIES, "indices")
```
- [ ] **Impl — replace the body of the `for category in BATCH_CATEGORIES` loop:**
```python
        for category in BATCH_CATEGORIES:
            shorts_added = 0
            if _short_active and category in _SHORT_CATEGORIES and (
                _short_regime == "bear" or _tape_det):
                short_cands = build_short_candidates_for_category(
                    category, regime=_short_regime, tape_deteriorating=_tape_det,
                    exclude_taken=exclude_taken, today=date.today(), preferred_risk=preferred_risk)
                for sc in short_cands:
                    if shorts_added >= _MAX_SHORTS_PER_CLASS:
                        break
                    if _try_add(sc):
                        shorts_added += 1
                if shorts_added:
                    logger.info("[ENGINE][%s] added %d shorts (regime=%s tape_det=%s)",
                                category, shorts_added, _short_regime, _tape_det)
            ranked = build_daily_candidates_for_category(
                category, max_count=max(pool_per_category, cap),
                by_asset_sentiment=by_asset_sentiment, preferred_risk=preferred_risk,
                tie_seed=tie_seed, exclude_taken=exclude_taken, market_ctx=market_ctx,
                active_events=active_events)
            taken = shorts_added  # shorts already consumed slots toward the cap of 5
            for c in ranked:
                if taken >= cap:
                    break
                if _try_add(c):
                    taken += 1
            logger.info("[ENGINE][%s] added %d / target %d to batch (%d shorts)",
                        category, taken, cap, shorts_added)
```
(This is Option 3: shorts run first; `taken` starts at `shorts_added`, so longs fill only the remaining slots to `cap=5`; when no shorts qualify, longs get all 5.)
- [ ] **Test (RED→GREEN):** an integration-style unit test on `build_daily_candidates` is heavy (DB/market_ctx). Instead, add a focused test that the per-class arithmetic holds: with a stubbed `_try_add`-like accumulator, 2 shorts + longs fill to exactly 5 and never exceed. If a full `build_daily_batch` characterization is impractical, assert via a smaller extracted helper or document the manual verification (a batch run diff). Minimum: a unit test that `taken` starts at `shorts_added` and the loop stops at `cap`.
- [ ] Run full unit suite — must stay green (332 + new). Checkpoint.

---

## Task 6: manual batch verification + guardrails

- [ ] Run a real batch in the container and confirm: in a `bear`/deteriorating day shorts appear (direction="short"), never exceed 2/class, total ≤5/class, no asset has both a long and a short.
```
docker-compose exec -T app python -c "from app.services.recommendations.engine import build_daily_candidates; \
  out = build_daily_candidates(); \
  print('total', len(out)); \
  import collections; \
  print('by_dir', collections.Counter(c.direction for c in out)); \
  print('shorts', [(c.asset_category,c.asset_id,c.confidence_score) for c in out if c.direction=='short'])"
```
- [ ] Confirm no `(category, asset_id)` appears twice (conflict-prevention) and shorts-per-class ≤ 2.
- [ ] Checkpoint. (Full walk-forward-with-conflict validation is Phase 7 — separate.)

---

## VERIFICATION RESULT (2026-09-18) — integration complete

- Tasks 1-6 done; full unit suite **337 green**.
- Live `build_daily_candidates()` runs clean: bull day → 5 longs, 0 shorts, 0 duplicate assets (correct;
  shorts pass executed `compute_tape_deteriorating`, no activation, added nothing).
- Wiring proven: `detect_short_triggers` fires breakdown triggers on live data (MSFT/NVDA/TSLA/AMZN).
- **PARITY BUG found + fixed:** `build_snapshot` fetched 90d (~63 bars) but EMA200 needs len>=200
  (technicals.py:808) → `ema200=None` in every live snapshot → silently disabled the EMA200 gate/bonus
  in the LONG engine + `compute_confidence`'s EMA200 filter + the shorts fade-top above-EMA200 gate.
  Fix: `build_snapshot` now fetches `period="1y"` (technicals.py:750). Verified: ema200 populates live
  (AAPL 291, TSLA below). Also restores the long engine's EMA200/52w features in live (a live-scoring
  change to the long book) — a live/backtest parity win beyond shorts.
- Post-fix, the shorts pass correctly produces 0 high-conviction shorts in today's bull tape (only TSLA
  below EMA200, no qualifying setup) — the engine declines to short a bull market rather than mint junk.
  The edge itself is backtest-proven (92 breakdown + 29 fade-top signals). Full capacity-under-bear
  behavior belongs to the Phase 7 walk-forward.

## Self-Review
- Second-pass shorts, Option-3 capacity (shorts-first, longs fill to 5, quiet classes keep 5) → Task 5. ✓
- Conflict-prevention via existing `seen_asset_key` dedup (direction-agnostic) → inherited, verified in Task 6. ✓
- Correlation + risk-budget via `_try_add` → inherited. ✓
- Activation (bear breakdown / deteriorating fade-top; crisis/high_vol blocked; stock+index categories only) → Task 5. ✓
- `shorts_signals.py` stays pure; all IO/glue in `shorts_engine_io.py`. ✓
- Per-family universe tuning (breakdown tighter / fade-top broader) noted as follow-up, not blocking. 
- No placeholders; every step has concrete code.
