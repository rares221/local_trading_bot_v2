# Shorts Engine — Signal Design Spec (Phase 5)

> Companion to `.cursor/plans/shorts-recovery-plan_fb6be623.plan.md`. That plan fixes the
> *architecture* (separate shorts engine) and the *integration* constraints (activation regime,
> capacity, conflict-prevention, P&L, watchlist). THIS spec defines the net-new **signal-generation
> logic** — how short candidates are detected and scored.

**Status:** design approved 2026-09-17. Prerequisite (regime taxonomy unification / engine-parity
Phase 0) is DONE, so `risk_off + bear` activation and the shared `score_candidate` pipeline are both
available.

---

## 1. Scope & phasing

The engine generates shorts from **two thesis families**, shipped in two phases. **The shorts engine
is not complete until both phases ship** — 5b is required scope, not optional.

- **Phase 5a — Breakdown family (ships first, validate-first):** short *confirmed weakness*.
  Triggers: support breakdown, failed breakout, failed retest. Scored with the shared
  `score_candidate(direction="short")` — which already rewards bearish momentum — so 5a reuses the
  validated parity pipeline with almost no new scoring code.
- **Phase 5b — Fade-top family (required follow-on):** short *exhausted strength at highs*.
  Triggers: distribution (OBV/price divergence at highs), exhaustion (RSI>70 + MACD histogram peaking
  + fading volume). These need a **bespoke "top-quality" scorer** (see §5) because the shared
  trend-following scorer *penalizes* high-RSI shorts — it would score the best tops the lowest.

**Why breakdown-first:** it inherits the validated scorer (fast to ship + validate), and it is the
family that fires in the sustained bear trends (2022-style) where shorts matter most. Fade-top is
deferred only to isolate its one genuinely-new calibration surface, not because it is lower value.

## 2. Module boundary

New module `app/services/recommendations/shorts_signals.py`, isolated from the long path:

- `detect_short_triggers(snap: TechnicalSnapshot) -> list[TriggerMatch]` — pure; returns the set of
  breakdown (5a) / top (5b) triggers whose strict conditions match. `TriggerMatch` carries the
  trigger name + family.
- `score_short_candidate(snap, trigger, regime_id, category, preferred_risk, sentiment, *, today)
  -> ShortScore` — routes to the family-appropriate scorer (§4/§5) + per-trigger offset.
- A screener entry point the orchestrator calls as a **sequential second pass after longs**, honoring
  conflict-prevention and the shared capacity cap (per the plan).

Isolation contract: the long path imports nothing from here; this module depends only on
`technicals` (snapshot + `compute_atr_levels`) and `scoring` (`score_candidate` for the breakdown
family). Independently unit-testable without DB/network.

## 3. Triggers

### 3a. Breakdown family (Phase 5a) — strict discrete gates

A candidate qualifies if **any one** matches. All use walk-forward-safe snapshot fields
(`supports`, `resistances`, `price`, `volume_ratio`, `ema200`):

1. **Support breakdown** — latest close < the established support (`snap.supports` nearest below the
   prior close; fall back to the 20-bar low when no swing support is available) by a margin, **and**
   `volume_ratio > 1.0` (breakdown on participation, not drift).
2. **Failed breakout (bull trap)** — within ~10 bars price pushed *above* a prior resistance, but the
   latest close is back *below* it.
3. **Failed retest** — support broken within ~15 bars; price rallied back to that broken level (now
   resistance) and the latest close was rejected below it. Highest-precision breakdown entry.

### 3b. Fade-top family (Phase 5b) — required follow-on

1. **Distribution** — OBV diverging from price at highs (price near local high while OBV rolls over).
2. **Exhaustion** — RSI > 70 + MACD histogram peaking (slope turning negative) + volume declining over
   the last ~3 bars.

### Shared pre-gates (both families, parity with the long engine)

- Must be **below EMA200** (structural down-bias; already a short gate in the long engine).
- **Hard-blocked in `crisis` / `high_vol`** (same block the long engine + replay apply).
- Active only in **`risk_off + bear`** (plan §activation; unblocked by Phase 0).

## 4. Scoring — breakdown family (Phase 5a)

Reuse the shared composition directly with `direction="short"`:

```
# `today` = current market date (date.today() until the Phase 5 US/Eastern market_date helper lands)
res = score_candidate(snap, "short", regime_id, category, preferred_risk,
                      sentiment_avg, sentiment_count, today=today)
short_confidence = min(100, res.confidence + TRIGGER_BASE_OFFSET[trigger])
# gate: short_confidence >= 60  (BEAR_REGIME_SHORT_MIN_CONFIDENCE)
```

- `compute_confidence(short)` (inside `score_candidate`) rewards the bearish momentum breakdowns have
  (minus-DI dominant, bearish MACD, OBV confirming down); `compute_soft_penalties` /
  `apply_signal_bonuses` short branches apply too. No new scorer for 5a.
- **`TRIGGER_BASE_OFFSET`** (calibration starting points, walk-forward-tunable scalars — NOT rubrics):
  originally `failed_retest = +8`, `failed_breakout = +5`, `support_breakdown = +3` ("ordered by
  precision"). **SUPERSEDED (Phase 1 redesign, 2026-09-18):** a Tier-1 walk-forward diagnosis found
  `failed_retest` — the highest-ranked trigger — was empirically the worst OOS performer (0% win rate,
  n=67); the ranking was re-fitting to a spent/curated sample, not a real precision signal. All triggers
  within each family are now flattened to an equal offset (`+5`); see `shorts_signals.py` and
  `docs/superpowers/plans/2026-09-18-shorts-engine.md` for the current state.
- **SL/TP:** `compute_atr_levels(price, risk_tier, snap, direction="short")` — SL above entry (snaps to
  resistance), TP below; R-multiple, no entry-normalization (P&L per plan: `short R = |stop − entry|`,
  `short P&L = (entry − exit) / short_R`).
- **Sentiment optional:** breakdown triggers are technical; `sentiment_avg=None` (what replay passes
  per window) scores fine — no dependence on the sentiment-history gap.

## 5. Scoring — fade-top family (Phase 5b, required)

The shared scorer misfits here (it treats high RSI as bullish → penalizes fade-top shorts). 5b defines
a **bespoke "top-quality" score** on ~3 inputs, rewarding *extremity of the top* rather than bearish
momentum:

- **Overbought extremity** — RSI distance above 70.
- **Divergence strength** — magnitude of OBV/price divergence at the high.
- **Volume-decline clarity** — how cleanly volume faded into the high.

Plus the per-trigger offset and the same 60 floor. This is the one new calibration surface in the
whole engine; it is validated as its own cohort (§6) before 5b ships.

## 6. Testing & the validation gate

1. **Trigger unit tests** (`detect_short_triggers`) — table-driven synthetic snapshots: each trigger
   fires on its exact conditions and NOT on near-misses (e.g. support breakdown with vs. without
   `volume_ratio > 1.0`; failed breakout where close is still above resistance).
2. **Scoring unit tests** — a breakdown short routes through `score_candidate(short)` + offset and
   clears / does not clear the 60 floor at the boundary.
3. **Orchestration tests** — conflict-prevention (no asset opens long+short simultaneously); capacity
   (≤2 shorts/class, remaining slots fill from longs).
4. **THE VALIDATION GATE — backtest over pooled bear periods (2022 + 2020 crash tail):**
   - (a) **Signal frequency** — target **n ≥ 30** at the 60 floor.
   - (b) **Short-only edge** — avg-R > 0 and WR meaningfully above the ~33% breakeven.
   - Per the plan's rule: if it cannot reach n ≥ 30 at a reasonable floor, **iterate the triggers — do
     NOT lower the floor.** Breakdown-only (5a) ships only if it clears this gate.
5. **Walk-forward** — shorts reported as a **separate cohort**: must not degrade the combined OOS
   baseline (Sharpe 2.845), and their own OOS metrics tracked independently. Same gate applies to 5b.

## 7. Out of scope (owned by the shorts-recovery-plan, not this spec)

Activation-regime detection, the max-2-per-class capacity mechanics, conflict-prevention enforcement
in the orchestrator + replay, the R-multiple P&L plumbing audit, and the short-candidate watchlist
(large-caps + sector leaders XLF/XLY/HYG/JNK/IWM). This spec assumes those and defines only the
signal-generation layer.
