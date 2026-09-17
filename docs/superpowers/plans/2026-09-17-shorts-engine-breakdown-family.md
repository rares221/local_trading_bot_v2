# Shorts Engine — Breakdown Family (Phase 5a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **PROJECT COMMIT RULE (CLAUDE.md):** Do NOT run `git commit` — the user commits manually at the end. Treat every "Commit" step below as a **checkpoint marker** (a good place to pause for review), not an instruction to actually commit.

**Goal:** Build the isolated short-signal generator for the *breakdown* family — detect three breakdown setups and score them with the existing shared scorer, returning short signals ready for later orchestrator integration.

**Architecture:** A new, self-contained module `app/services/recommendations/shorts_signals.py`. It exposes pure functions: three trigger detectors over recent OHLCV bars + snapshot, an aggregator with the short pre-gates, and `generate_short_signal(...)` which scores a matched trigger via the already-validated `score_candidate(direction="short")`, applies a per-trigger offset and the 60 floor, computes short SL/TP, and returns a `ShortSignal`. No DB/network; no edits to the long path. Orchestrator wiring, capacity, and conflict-prevention are OUT OF SCOPE (owned by the shorts-recovery-plan).

**Tech Stack:** Python 3.11, pandas (OHLCV frames), pytest. Reuses `app/services/technicals.py` (`TechnicalSnapshot`, `compute_atr_levels`, `calc_risk_reward`) and `app/services/recommendations/scoring.py` (`score_candidate`).

---

## File Structure

- **Create** `app/services/recommendations/shorts_signals.py` — the entire breakdown signal generator (dataclass `ShortSignal`, constants, level helpers, 3 trigger detectors, `detect_short_triggers`, `generate_short_signal`).
- **Create** `tests/unit/services/test_shorts_signals.py` — unit tests for every trigger + the scorer + the top-level generator.
- **Create** `scripts/validate_shorts_breakdown.py` — the validation-gate harness (runs the generator over pooled bear windows, reports frequency + short-only metrics).
- No modifications to existing files in this plan.

**Container test command (Podman, from repo root, PowerShell):**
`docker-compose exec -T app python -m pytest tests/unit/services/test_shorts_signals.py -v`

---

## Task 1: Module scaffold — `ShortSignal`, constants, level helpers

**Files:**
- Create: `app/services/recommendations/shorts_signals.py`
- Test: `tests/unit/services/test_shorts_signals.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/services/test_shorts_signals.py
import pandas as pd
import pytest

from app.services.technicals import TechnicalSnapshot
from app.services.recommendations import shorts_signals as ss


def _bars(closes, highs=None, lows=None, vols=None):
    """Build an OHLCV frame from close list; highs/lows/vols default around close."""
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


def test_resolve_support_prefers_nearest_swing_below_prev_close():
    snap = _snap(supports=[95.0, 90.0, 80.0])
    bars = _bars([100.0] * 25)
    # prev close = 100; nearest swing support below is 95.
    assert ss._resolve_support(snap, bars, prev_close=100.0) == 95.0


def test_resolve_support_falls_back_to_20bar_low():
    snap = _snap(supports=[])
    lows = [90.0] * 24 + [100.0]  # 20-bar low (excluding last bar) = 90
    bars = _bars([100.0] * 25, lows=lows)
    assert ss._resolve_support(snap, bars, prev_close=100.0) == 90.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker-compose exec -T app python -m pytest tests/unit/services/test_shorts_signals.py -v`
Expected: FAIL — `ModuleNotFoundError` / `AttributeError: module ... has no attribute '_resolve_support'`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/recommendations/shorts_signals.py
"""
Shorts engine — breakdown family signal generator (Phase 5a).

Isolated from the long path. Detects three "confirmed weakness" breakdown setups and scores
them with the shared, validated scorer (score_candidate with direction="short"). See
docs/superpowers/specs/2026-09-17-shorts-signal-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd

from app.services.technicals import TechnicalSnapshot

# Per-trigger base confidence offset (walk-forward-tunable), ordered by precision.
TRIGGER_BASE_OFFSET: dict[str, int] = {
    "failed_retest": 8,
    "failed_breakout": 5,
    "support_breakdown": 3,
}

# Shorts require higher conviction than longs (historically lower WR).
SHORT_CONFIDENCE_FLOOR: int = 60

# Regimes where NO position (long or short) may open — hard block, mirrors the long engine.
_BLOCKED_REGIMES = ("crisis", "high_vol")

_LOOKBACK_BREAKOUT = 10   # bars to look back for a failed breakout
_LOOKBACK_RETEST = 15     # bars to look back for a broken-support retest
_SWING_WINDOW = 20        # fallback swing high/low window
_BREAK_MARGIN = 0.001     # 0.1% — close must clear the level by this to count as a break


@dataclass
class ShortSignal:
    symbol: str
    trigger: str
    direction: str  # always "short"
    confidence: int
    entry: float
    stop_loss: float
    take_profit: float
    risk_reward: Optional[float]


def _resolve_support(snap: TechnicalSnapshot, bars: pd.DataFrame, prev_close: float) -> Optional[float]:
    """Nearest swing support below prev_close; fall back to the 20-bar low (excluding last bar)."""
    below = [s for s in (snap.supports or []) if s < prev_close]
    if below:
        return max(below)  # nearest below
    if bars is not None and len(bars) > _SWING_WINDOW:
        return float(bars["Low"].iloc[-(_SWING_WINDOW + 1):-1].min())
    return None


def _resolve_resistance(snap: TechnicalSnapshot, bars: pd.DataFrame, prev_close: float) -> Optional[float]:
    """Nearest swing resistance above prev_close; fall back to the pre-window 20-bar high."""
    above = [r for r in (snap.resistances or []) if r > prev_close]
    if above:
        return min(above)  # nearest above
    if bars is not None and len(bars) > (_SWING_WINDOW + _LOOKBACK_BREAKOUT):
        pre = bars["High"].iloc[-(_SWING_WINDOW + _LOOKBACK_BREAKOUT):-_LOOKBACK_BREAKOUT]
        return float(pre.max())
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker-compose exec -T app python -m pytest tests/unit/services/test_shorts_signals.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add app/services/recommendations/shorts_signals.py tests/unit/services/test_shorts_signals.py
git commit -m "feat(shorts): scaffold breakdown signal module + level helpers"
```

---

## Task 2: `support_breakdown` trigger

**Files:**
- Modify: `app/services/recommendations/shorts_signals.py`
- Test: `tests/unit/services/test_shorts_signals.py`

- [ ] **Step 1: Write the failing test**

```python
def test_support_breakdown_fires_on_close_below_support_with_volume():
    snap = _snap(supports=[95.0], volume_ratio=1.5)
    bars = _bars([100.0] * 24 + [94.0])  # latest close 94 < support 95
    assert ss.support_breakdown(snap, bars) is True


def test_support_breakdown_no_fire_without_volume():
    snap = _snap(supports=[95.0], volume_ratio=0.8)  # below 1.0
    bars = _bars([100.0] * 24 + [94.0])
    assert ss.support_breakdown(snap, bars) is False


def test_support_breakdown_no_fire_when_close_above_support():
    snap = _snap(supports=[95.0], volume_ratio=1.5)
    bars = _bars([100.0] * 25)  # close 100 > support 95
    assert ss.support_breakdown(snap, bars) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker-compose exec -T app python -m pytest tests/unit/services/test_shorts_signals.py -k support_breakdown -v`
Expected: FAIL — `AttributeError: ... 'support_breakdown'`.

- [ ] **Step 3: Write minimal implementation** (append to `shorts_signals.py`)

```python
def support_breakdown(snap: TechnicalSnapshot, bars: pd.DataFrame) -> bool:
    """Latest close < established support (by margin) AND volume_ratio > 1.0."""
    if bars is None or len(bars) < 2:
        return False
    if snap.volume_ratio is None or snap.volume_ratio <= 1.0:
        return False
    prev_close = float(bars["Close"].iloc[-2])
    support = _resolve_support(snap, bars, prev_close)
    if support is None:
        return False
    close = float(bars["Close"].iloc[-1])
    return close < support * (1 - _BREAK_MARGIN)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker-compose exec -T app python -m pytest tests/unit/services/test_shorts_signals.py -k support_breakdown -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add app/services/recommendations/shorts_signals.py tests/unit/services/test_shorts_signals.py
git commit -m "feat(shorts): support_breakdown trigger"
```

---

## Task 3: `failed_breakout` trigger

**Files:**
- Modify: `app/services/recommendations/shorts_signals.py`
- Test: `tests/unit/services/test_shorts_signals.py`

- [ ] **Step 1: Write the failing test**

```python
def test_failed_breakout_fires_when_high_pierced_resistance_but_closed_back_below():
    snap = _snap(resistances=[105.0])
    # last 10 bars include a high of 107 (pierced 105), latest close 102 (back below 105)
    closes = [100.0] * 20 + [101, 106, 104, 103, 102]
    highs = [c * 1.005 for c in closes[:-4]] + [107.0, 106.5, 105.5, 103.0]
    bars = _bars(closes, highs=highs)
    assert ss.failed_breakout(snap, bars) is True


def test_failed_breakout_no_fire_when_still_above_resistance():
    snap = _snap(resistances=[105.0])
    closes = [100.0] * 20 + [106, 107, 108, 108, 107]  # latest close 107 > 105
    highs = [c * 1.01 for c in closes]
    bars = _bars(closes, highs=highs)
    assert ss.failed_breakout(snap, bars) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker-compose exec -T app python -m pytest tests/unit/services/test_shorts_signals.py -k failed_breakout -v`
Expected: FAIL — `AttributeError: ... 'failed_breakout'`.

- [ ] **Step 3: Write minimal implementation** (append)

```python
def failed_breakout(snap: TechnicalSnapshot, bars: pd.DataFrame) -> bool:
    """Within the last ~10 bars a high pierced resistance, but the latest close is back below it."""
    if bars is None or len(bars) < 2:
        return False
    prev_close = float(bars["Close"].iloc[-2])
    resistance = _resolve_resistance(snap, bars, prev_close)
    if resistance is None:
        return False
    recent_high = float(bars["High"].iloc[-_LOOKBACK_BREAKOUT:].max())
    close = float(bars["Close"].iloc[-1])
    return recent_high > resistance and close < resistance
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker-compose exec -T app python -m pytest tests/unit/services/test_shorts_signals.py -k failed_breakout -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add app/services/recommendations/shorts_signals.py tests/unit/services/test_shorts_signals.py
git commit -m "feat(shorts): failed_breakout trigger"
```

---

## Task 4: `failed_retest` trigger

**Files:**
- Modify: `app/services/recommendations/shorts_signals.py`
- Test: `tests/unit/services/test_shorts_signals.py`

- [ ] **Step 1: Write the failing test**

```python
def test_failed_retest_fires_break_then_rally_then_reject():
    snap = _snap(supports=[95.0])
    # break below 95, rally back to ~95, then reject and close below 95.
    closes = [100.0] * 12 + [93, 92, 96, 95, 94]  # broke (92/93), retested (~95/96), rejected (94)
    highs = [c * 1.01 for c in closes[:-3]] + [96.0, 95.2, 94.5]
    bars = _bars(closes, highs=highs)
    assert ss.failed_retest(snap, bars) is True


def test_failed_retest_no_fire_when_reclaimed_support():
    snap = _snap(supports=[95.0])
    closes = [100.0] * 12 + [93, 92, 96, 97, 98]  # reclaimed above 95 -> not a rejection
    bars = _bars(closes)
    assert ss.failed_retest(snap, bars) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker-compose exec -T app python -m pytest tests/unit/services/test_shorts_signals.py -k failed_retest -v`
Expected: FAIL — `AttributeError: ... 'failed_retest'`.

- [ ] **Step 3: Write minimal implementation** (append)

```python
def failed_retest(snap: TechnicalSnapshot, bars: pd.DataFrame) -> bool:
    """
    Support broken within ~15 bars, price rallied back to it (now resistance), latest close
    rejected below. Precise: some close in the window < support; a subsequent high came back
    within 1% of support; latest close < support.
    """
    if bars is None or len(bars) < 3:
        return False
    prev_close = float(bars["Close"].iloc[-2])
    support = _resolve_support(snap, bars, prev_close)
    if support is None:
        return False
    window = bars.iloc[-_LOOKBACK_RETEST:]
    broke = bool((window["Close"] < support * (1 - _BREAK_MARGIN)).any())
    retested = bool((window["High"] >= support * (1 - 0.01)).iloc[1:].any())  # rallied back near level
    rejected = float(bars["Close"].iloc[-1]) < support * (1 - _BREAK_MARGIN)
    return broke and retested and rejected
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker-compose exec -T app python -m pytest tests/unit/services/test_shorts_signals.py -k failed_retest -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add app/services/recommendations/shorts_signals.py tests/unit/services/test_shorts_signals.py
git commit -m "feat(shorts): failed_retest trigger"
```

---

## Task 5: `detect_short_triggers` aggregator + short pre-gates

**Files:**
- Modify: `app/services/recommendations/shorts_signals.py`
- Test: `tests/unit/services/test_shorts_signals.py`

- [ ] **Step 1: Write the failing test**

```python
def test_detect_returns_matched_triggers_when_gates_pass():
    snap = _snap(supports=[95.0], volume_ratio=1.5, ema200=110.0)  # price 100 < ema200 110 (below)
    bars = _bars([100.0] * 24 + [94.0])
    assert "support_breakdown" in ss.detect_short_triggers(snap, bars, regime="bear")


def test_detect_empty_when_above_ema200():
    snap = _snap(supports=[95.0], volume_ratio=1.5, ema200=90.0)  # price 100 > ema200 90 (above)
    bars = _bars([100.0] * 24 + [94.0])
    assert ss.detect_short_triggers(snap, bars, regime="bear") == []


def test_detect_empty_in_blocked_regime():
    snap = _snap(supports=[95.0], volume_ratio=1.5, ema200=110.0)
    bars = _bars([100.0] * 24 + [94.0])
    assert ss.detect_short_triggers(snap, bars, regime="high_vol") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker-compose exec -T app python -m pytest tests/unit/services/test_shorts_signals.py -k detect -v`
Expected: FAIL — `AttributeError: ... 'detect_short_triggers'`.

- [ ] **Step 3: Write minimal implementation** (append)

```python
def detect_short_triggers(snap: TechnicalSnapshot, bars: pd.DataFrame, regime: Optional[str]) -> list[str]:
    """
    Return the breakdown triggers that match, after the shared short pre-gates:
      - blocked entirely in crisis / high_vol,
      - must be BELOW EMA200 (structural down-bias).
    (Activation in risk_off+bear is the caller's precondition; this only enforces the hard gates.)
    """
    if regime in _BLOCKED_REGIMES:
        return []
    if snap.ema200 is not None and snap.price > snap.ema200:
        return []
    matched: list[str] = []
    if support_breakdown(snap, bars):
        matched.append("support_breakdown")
    if failed_breakout(snap, bars):
        matched.append("failed_breakout")
    if failed_retest(snap, bars):
        matched.append("failed_retest")
    return matched
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker-compose exec -T app python -m pytest tests/unit/services/test_shorts_signals.py -k detect -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add app/services/recommendations/shorts_signals.py tests/unit/services/test_shorts_signals.py
git commit -m "feat(shorts): detect_short_triggers aggregator + pre-gates"
```

---

## Task 6: `generate_short_signal` — score, level, gate

**Files:**
- Modify: `app/services/recommendations/shorts_signals.py`
- Test: `tests/unit/services/test_shorts_signals.py`

Uses `score_candidate(direction="short")` (from `app/services/recommendations/scoring.py`), adds the
per-trigger offset, applies the 60 floor, computes short SL/TP via `compute_atr_levels(direction="short")`,
and gates R:R with `calc_risk_reward`. When multiple triggers match, the highest-offset trigger is used.

- [ ] **Step 1: Write the failing test**

```python
def test_generate_short_signal_emits_for_strong_breakdown():
    # A snapshot bearish enough that score_candidate(short) + offset clears the 60 floor.
    snap = _snap(
        price=100.0, atr=2.0, rsi=38.0, rsi_slope=-1.0, macd=(-1.0, -0.5, -0.5),
        macd_hist_slope=-0.5, adx=(30.0, 12.0, 28.0), stochastic=(40.0, 45.0),
        bollinger=(105.0, 100.0, 95.0), obv=(1000.0, -5.0), ema200=110.0,
        volume_ratio=1.6, ofi_ratio=0.45, atr_percentile=0.5, supports=[95.0],
    )
    bars = _bars([100.0] * 24 + [94.0], vols=[1_000_000] * 25)
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
    bars = _bars([100.0] * 25)  # no breakdown
    sig = ss.generate_short_signal(
        "TESTZ", snap, bars, regime_id="bear", category="stocks_large",
        risk_tier="moderate", preferred_risk="moderate",
        sentiment_avg=None, sentiment_count=0, today=date(2026, 9, 15),
    )
    assert sig is None


def test_generate_short_signal_none_below_floor():
    # Weak indicators -> score_candidate(short)+offset stays under 60.
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker-compose exec -T app python -m pytest tests/unit/services/test_shorts_signals.py -k generate -v`
Expected: FAIL — `AttributeError: ... 'generate_short_signal'`.

- [ ] **Step 3: Write minimal implementation** (append)

```python
from app.services.technicals import compute_atr_levels, calc_risk_reward
from app.services.recommendations.scoring import score_candidate

# Minimum reward:risk for a short to be emitted (mirrors the long floor; tune in backtest).
_SHORT_MIN_RR = 2.0


def generate_short_signal(
    symbol: str,
    snap: TechnicalSnapshot,
    bars: pd.DataFrame,
    *,
    regime_id: Optional[str],
    category: str,
    risk_tier: str,
    preferred_risk: str,
    sentiment_avg: Optional[float],
    sentiment_count: int,
    today: date,
) -> Optional[ShortSignal]:
    """Full breakdown-short pipeline: detect -> score -> level -> gate. None if it does not qualify."""
    triggers = detect_short_triggers(snap, bars, regime_id)
    if not triggers:
        return None
    # Highest-precision trigger wins when several match.
    trigger = max(triggers, key=lambda t: TRIGGER_BASE_OFFSET[t])

    res = score_candidate(
        snap, "short", regime_id, category, preferred_risk,
        sentiment_avg, sentiment_count, today=today,
    )
    confidence = min(100, res.confidence + TRIGGER_BASE_OFFSET[trigger])
    if confidence < SHORT_CONFIDENCE_FLOOR:
        return None

    sl, tp = compute_atr_levels(snap.price, risk_tier, snapshot=snap, direction="short")
    rr = calc_risk_reward(snap.price, sl, tp)
    if rr is None or rr < _SHORT_MIN_RR:
        return None

    return ShortSignal(
        symbol=symbol, trigger=trigger, direction="short", confidence=confidence,
        entry=round(snap.price, 4), stop_loss=round(sl, 4), take_profit=round(tp, 4),
        risk_reward=round(rr, 4),
    )
```

Move the two new imports (`compute_atr_levels, calc_risk_reward`, `score_candidate`) to the top-of-file
import block during this step rather than mid-file.

- [ ] **Step 4: Run test to verify it passes**

Run: `docker-compose exec -T app python -m pytest tests/unit/services/test_shorts_signals.py -v`
Expected: PASS (all tests; ~15).

> If `test_generate_short_signal_emits_for_strong_breakdown` fails on the `confidence >= 60`
> assertion, the fixture's indicator strength is the tuning surface — capture the actual
> `score_candidate(snap,"short",...)` value in a scratch run and adjust the fixture's RSI/ADX/MACD so a
> genuinely strong breakdown clears 60. Do NOT lower `SHORT_CONFIDENCE_FLOOR` to pass the test.

- [ ] **Step 5: Commit (checkpoint)**

```bash
git add app/services/recommendations/shorts_signals.py tests/unit/services/test_shorts_signals.py
git commit -m "feat(shorts): generate_short_signal (score + level + gate)"
```

---

## Task 7: Validation-gate harness (the go/no-go)

**Files:**
- Create: `scripts/validate_shorts_breakdown.py`

This runs the generator over pooled bear windows and reports **signal frequency** (target n ≥ 30) and
**short-only edge** (avg-R > 0, WR meaningfully above ~33% breakeven). It reuses the backtest data
provider + regime calendar so it is walk-forward-safe. It does NOT wire shorts into the live engine.

- [ ] **Step 1: Write the harness**

```python
# scripts/validate_shorts_breakdown.py
"""
Phase 5a validation gate: run the breakdown-short generator over pooled bear/risk_off windows and
report signal frequency + short-only outcome metrics. Go/no-go for shipping breakdown-only.

Run in the container:
  docker-compose exec -T app python scripts/validate_shorts_breakdown.py
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from app.services import technicals as tech
from app.services.backtesting.data_provider import prefetch_symbols, get_trading_days
from app.services.backtesting.regime import build_regime_calendar
from app.services.backtesting.simulator import simulate_outcome
from app.services.recommendations.shorts_signals import generate_short_signal

# Short candidate watchlist (large-caps + sector leaders that distribute first — per plan).
SHORT_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "META", "AMZN", "TSLA", "JPM", "BAC", "XLF", "XLY",
    "HYG", "JNK", "IWM", "SPY", "QQQ",
]

# Pooled bear / risk_off windows.
WINDOWS = [
    (date(2022, 1, 1), date(2022, 12, 31)),   # Fed tightening bear
    (date(2020, 2, 15), date(2020, 4, 30)),   # COVID crash
]


def _bars_up_to(symbol: str, sim_date: date, ohlcv: dict) -> pd.DataFrame | None:
    df = ohlcv.get(symbol)
    if df is None or df.empty:
        return None
    return df[df.index <= pd.Timestamp(sim_date)]


def main() -> None:
    tech.clear_cache()
    ohlcv = prefetch_symbols(SHORT_WATCHLIST + ["^GSPC", "^VIX"], period="5y")
    signals: list[dict] = []

    for start, end in WINDOWS:
        days = get_trading_days(ohlcv, start, end)
        calendar = build_regime_calendar(days, ohlcv)
        for d in days:
            regime = calendar.get(d, "neutral")
            if regime != "bear":  # breakdown family activates in bear (risk_off+bear precondition)
                continue
            for symbol in SHORT_WATCHLIST:
                bars = _bars_up_to(symbol, d, ohlcv)
                if bars is None or len(bars) < 60:
                    continue
                snap = tech.build_snapshot(symbol)  # walk-forward via _apply_backtest_cutoff
                if snap is None or snap.price <= 0:
                    continue
                sig = generate_short_signal(
                    symbol, snap, bars, regime_id=regime, category="stocks_large",
                    risk_tier="moderate", preferred_risk="moderate",
                    sentiment_avg=None, sentiment_count=0, today=d,
                )
                if sig is None:
                    continue
                outcome = simulate_outcome(sig.entry, sig.stop_loss, sig.take_profit, "short", bars)
                signals.append({"date": d, "symbol": symbol, "trigger": sig.trigger,
                                "confidence": sig.confidence, "pnl_r": outcome.pnl_r,
                                "outcome": outcome.outcome})

    n = len(signals)
    resolved = [s for s in signals if s["outcome"] in ("hit_tp", "hit_sl")]
    wins = sum(1 for s in resolved if s["outcome"] == "hit_tp")
    avg_r = (sum(s["pnl_r"] for s in signals) / n) if n else 0.0
    wr = (wins / len(resolved)) if resolved else 0.0

    print("=== Phase 5a breakdown-short validation ===")
    print(f"signals={n}  resolved={len(resolved)}  win_rate={wr:.3f}  avg_pnl_r={avg_r:.4f}")
    print(f"GATE: frequency n>=30 -> {'PASS' if n >= 30 else 'FAIL'}")
    print(f"GATE: edge avg_r>0 & wr>0.33 -> {'PASS' if (avg_r > 0 and wr > 0.33) else 'FAIL'}")
    by_trigger: dict[str, int] = {}
    for s in signals:
        by_trigger[s["trigger"]] = by_trigger.get(s["trigger"], 0) + 1
    print("by_trigger:", by_trigger)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the harness**

Run: `docker-compose exec -T app python scripts/validate_shorts_breakdown.py`
Expected: prints signal count + WR + avg-R + the two GATE lines. (First run may take a few minutes for
the 5y prefetch.)

- [ ] **Step 3: Interpret the gate**

- If **frequency FAIL** (n < 30) or **edge FAIL**: the triggers/thresholds need iteration (loosen/tighten
  the lookbacks, margins, or per-trigger offsets in `shorts_signals.py`) — **do NOT lower
  `SHORT_CONFIDENCE_FLOOR`**. Re-run until both gates pass or record why breakdown-only is not viable.
- If **both PASS**: breakdown-only is validated and ready for orchestrator integration (owned by the
  shorts-recovery-plan — Phase 6/8, out of scope here).

- [ ] **Step 4: Commit (checkpoint)**

```bash
git add scripts/validate_shorts_breakdown.py
git commit -m "feat(shorts): breakdown-family validation-gate harness"
```

---

## Self-Review

**Spec coverage:**
- §3a support breakdown → Task 2 ✓; failed breakout → Task 3 ✓; failed retest → Task 4 ✓.
- §3 shared pre-gates (below EMA200, crisis/high_vol block) → Task 5 ✓. (risk_off+bear activation is the
  caller's precondition; the harness enforces `regime == "bear"` and integration owns risk_off — noted.)
- §4 scoring (score_candidate short + per-trigger offset + 60 floor + short SL/TP + R-multiple) → Task 6 ✓.
- §6 testing: trigger unit tests (Tasks 2-4), scoring/floor tests (Task 6), the validation gate (Task 7) ✓.
- §6 orchestration tests (conflict/capacity) and §5 fade-top (5b) → intentionally OUT OF SCOPE (owned by
  the shorts-recovery-plan / deferred to 5b) — consistent with the plan's stated scope.

**Placeholder scan:** no TBD/TODO; every code step has complete code; thresholds have concrete values
(marked tunable where the walk-forward will adjust them). ✓

**Type consistency:** `ShortSignal` fields, `TRIGGER_BASE_OFFSET` keys ("support_breakdown",
"failed_breakout", "failed_retest"), `SHORT_CONFIDENCE_FLOOR`, and the `score_candidate(...)` signature
(matches `scoring.py`: positional snap/direction/regime_id/category/preferred_risk/sentiment_avg/
sentiment_count, keyword `today`) are consistent across Tasks 1-7. ✓

---

## Execution notes (2026-09-17) — deviations from the draft, all TDD-driven

Tasks 1–6 implemented and green (15 unit tests; full suite 320). Two corrections vs. the draft code,
both surfaced by the tests:

1. **`failed_retest` (Task 4)** — the draft's `retested` check (`window["High"] >= support*0.99` over
   `.iloc[1:]`) misfired on a plain breakdown: pre-break bars sitting above support counted as a
   "retest." Fixed to require the rally **after the first break** — find `first_break =
   below.idxmax()` on `reset_index(drop=True)` window, then check `after = window.iloc[first_break+1:]`
   for `High >= support*0.99`. Also resolves support against the **window high** (the rally peak), not
   `prev_close` (which is already back under the level during a retest).

2. **`generate_short_signal` (Task 6)** — base ATR R:R for a short is ~1.4 (moderate 3.5/2.5) and the
   short SL snapping only *widens* the stop, so nothing could clear `_SHORT_MIN_RR=2.0` and every signal
   returned `None`. Added an inline **short-TP stretch** to the floor (`tp = min(tp, entry -
   _SHORT_MIN_RR * (sl - entry))`) — mirrors how the long engine stretches levels; keeps isolation (no
   engine import). Full short R:R *redesign* (tighter stops / squeeze-exit) remains a Phase 6 item per
   the spec.

3. **Task 7 harness** — corrected vs. draft: sets `tech.set_backtest_mode(Timestamp(d))` per day so
   `build_snapshot` is walk-forward-safe, and simulates outcomes on `get_future_bars(...)` (future
   only), not the historical window. `simulate_outcome(entry, sl, tp, "short", future_bars, max_bars)`.
   Also added **position de-dup** (one open position per symbol) and extended to `period="max"` +
   pooled four bear windows (2015-16, Q4 2018, 2020, 2022) — the 5y/2-window draft only sampled 2022.

### VALIDATION GATE RESULT (2026-09-17) — PASS
De-duped, pooled across all four bear regimes: **signals=92, WR=44.4%, avg_pnl_r=+0.2846**
(overlap_skipped=1414). Both gates PASS (n≥30 ✓; avg_r>0 & WR>0.33 ✓). Survives the 0.1R friction
haircut (~+0.18R net). Edge softened realistically from 2022-only (50% / +0.37R) when pooling harder
regimes — a real edge, not a single-year artifact. Caveats: `failed_retest` dominates (84/92; offset
tie-break masks `support_breakdown`), NVDA dropped on a yfinance period=max quirk (14 symbols). This is
a frequency+edge gate, NOT the full walk-forward-with-conflict cohort (shorts-recovery-plan Phase 7).
**Breakdown-only signal generation is VALIDATED → ready for orchestrator integration (Phase 6/8).**
