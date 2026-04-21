# Signal Quality Improvement — Design Spec
**Date:** 2026-04-16  
**Branch:** fix/strategy_enhance  
**Approach:** Option A — Narrow & high-confidence (hybrid hard gate + soft penalties)

---

## Context & Motivation

Current backtest results (post-session 2026-04-16):
- Win rate: 34.7% | Sharpe: 2.439 | Max DD: 203.59R | Avg P&L: 0.114R | Trades: 3851

Breakdown from `backtest_trades` (all historical runs):
- Longs: 29,010 trades → 37.0% WR
- Shorts: 9,250 trades → 22.8% WR (globally disabled)
- Crisis: 8.3% WR → hard-blocked
- High_vol: 26.0% WR → hard-blocked
- Bear: 804 trades → 31.8% WR

Three levers identified to push WR toward 37%+ and improve Sharpe:
1. ADX hard gate — filter trendless/choppy market entries
2. Soft penalties for OBV distribution and fading sentiment velocity (capped, non-stacking)
3. Selective short re-enablement in bear regime only with raised confidence floor

---

## Section 1 — ADX Hard Gate

**File:** `app/services/recommendations/engine.py` → `_build_candidate()`  
**Location:** Immediately after the technicals snapshot is built, before confidence scoring.

**Rule:**
- If `snap.adx` is available and `adx_value < 20` → drop candidate, log as `no_trend_adx`
- If `snap.adx` is `None` → do NOT gate (missing data ≠ no trend)

**Threshold rationale:** ADX < 20 = trendless/choppy market per Wilder's original definition, confirmed by backtest literature. Our 14-day momentum signals are unreliable without a clear directional trend. ADX ≥ 20 is the minimum for a tradeable trend.

**Applies to:** all directions (long and bear-regime shorts).

---

## Section 2 — Soft Penalties (OBV + Sentiment Velocity)

**File:** `app/services/recommendations/engine.py` → `_build_candidate()`  
**Location:** In the penalty chain, after existing penalties.

**New penalties (longs only):**

| Signal | Condition | Penalty | Log label |
|--------|-----------|---------|-----------|
| OBV distribution | `snap.obv` slope < 0 | -5 pts | `obv_distribution` |
| Sentiment fading | `sentiment_velocity < -0.05` | -7 pts | `sentiment_fading` |

**Note on OBV:** OBV confirmation already contributes +10 pts to confidence when confirming direction. The explicit -5 penalty when OBV disagrees keeps the total OBV swing at 15 pts (miss +10, get -5) — proportionate for a single indicator.

**Combined cap:** `new_penalty = min(obv_pen + vel_pen, 15)` — applied as a single subtraction so it cannot exceed -15 regardless of how many signals fire.

**Shorts exclusion:** Neither penalty applies to bear-regime shorts. OBV falling is neutral-to-positive for shorts; sentiment dynamics differ for downside trades.

**Anti-stacking enforcement:**
- New penalties are applied before the existing penalty chain
- A global confidence floor check runs after ALL penalties: `final_confidence = start - new_penalties - existing_penalties`
- Decision gate is `final_confidence < min_confidence_gate` — single decision point, not accumulated penalty sum
- Full breakdown logged per candidate: `start=62, new_filters=-8(obv), existing=-7, final=47, gate=45 → pass`

---

## Section 3 — Bear Regime Shorts Re-enablement

**File:** `app/services/recommendations/engine.py`  
**New flag:** `SHORTS_BEAR_REGIME_ENABLED = True` (alongside existing `SHORTS_ENABLED = False`)

**Conditions for a bear-regime short to be allowed:**
1. `market_regime == "bear"` AND `SHORTS_BEAR_REGIME_ENABLED = True`
2. `SHORTS_ENABLED` global kill switch is not False (backward compat — flipping it off kills all shorts instantly)
3. Confidence ≥ 50 (raised floor vs ~35-40 for longs)
4. Price below EMA200 (existing hard requirement, unchanged)
5. RSI above short floor (existing gate, unchanged)
6. ADX ≥ 20 (Section 1 gate applies equally)

**Logging:** candidates dropped due to raised confidence floor logged as `short_low_confidence_bear`

**Kill switch:** `SHORTS_ENABLED = False` remains the global override. Bear-regime shorts are a conditional layer on top, not a bypass.

---

## Confidence Flow (post-implementation)

```
computed_confidence (from existing scoring)
  - new filter penalties: min(obv_pen + vel_pen, 15)   ← capped at -15
  - existing penalties (already reduced 2026-04-16)
= final_confidence
  → if final_confidence < min_confidence_gate: drop
  → else: candidate passes to ranking
```

---

## Success Criteria

- Overall WR improves from 34.7% toward 37%+
- Sharpe stays ≥ 2.439 or improves
- Max drawdown stays ≤ 203.59R
- No regression in avg P&L (stays ≥ 0.114R)
- Bear regime short WR > 30% (validates re-enablement is net positive)
- Candidate count does not drop below ~80% of current volume (ADX gate should not wipe out the pipeline)
