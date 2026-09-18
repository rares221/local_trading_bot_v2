# Shorts Engine — Consolidated Plan (breakdown + fade-top)

> **Status (2026-09-18):** signal engine BUILT for both families and validated in isolation, wired
> into the batch and the walk-forward — but **disabled** (`SHORTS_ENGINE_ENABLED = False`) because the
> integrated out-of-sample edge is **negative**. Active work = **Tier-1 diagnosis** (below).
> **Spec:** `docs/superpowers/specs/2026-09-17-shorts-signal-design.md` (the one spec for this effort).
> **Commit rule (CLAUDE.md):** the user commits; "checkpoints" are review pauses, not commits.

This is the single living plan for the shorts effort. It supersedes and consolidates the three earlier
plan files (breakdown-family, fade-top-family, orchestrator-integration), whose delivered outcomes are
recorded in `docs/CHANGELOG.md`.

---

## 1. Architecture (as built)

- **`app/services/recommendations/shorts_signals.py`** — pure, IO-free signal engine. Two families:
  - **Breakdown** (`support_breakdown`, `failed_breakout`, `failed_retest`): fires in canonical `bear`
    regime, requires price **below** EMA200. Scored with the shared `score_candidate(direction="short")`
    + per-trigger offset (retest +8 / breakout +5 / breakdown +3), 60 confidence floor, short SL/TP
    stretched to R:R ≥ 2.0.
  - **Fade-top** (`distribution`, `exhaustion`): fires when the tape is deteriorating
    (`_tape_deteriorating` = VIX rising 5d + SPX < 20d MA), requires price **above** EMA200 (a top is
    above its 200-EMA). Scored with a bespoke `top_quality_score` (the shared scorer penalizes
    high-RSI shorts, so fade-top needs its own additive 0–100 scorer).
  - `detect_short_triggers` is family-aware; `generate_short_signal` routes fade-top → `top_quality_score`,
    breakdown → `score_candidate`. Both families hard-blocked in `crisis`/`high_vol`.
- **`app/services/recommendations/shorts_engine_io.py`** — IO/adapter glue (keeps the engine pure):
  `SHORT_UNIVERSE` (27: 15 stocks_large + 12 index/sector ETFs), `compute_tape_deteriorating()`
  (fetches ^VIX/^GSPC), `short_signal_to_candidate()` (ShortSignal → `Candidate`, 14-day expiry),
  `build_short_candidates_for_category()` (scan → generate → convert → rank).
- **Batch integration** (`engine.build_daily_candidates`) — **Option 3 capacity**: in short-active
  stock/index classes, shorts run FIRST (up to `MAX_SHORTS_PER_CLASS = 2` that qualify), longs then fill
  to `cap = 5`; quiet classes keep all 5 longs. Conflict-prevention, correlation, and risk-budget are
  inherited free from the existing `_try_add` `seen_asset_key` dedup (direction-agnostic on
  `(category, asset_id)`). Gated behind `SHORTS_ENGINE_ENABLED`.
- **Walk-forward** (`replay._replay_window`) — shorts wired PER-SIGNAL (no portfolio capacity/conflict —
  same as the long path there); per-day `tape_det` via cutoff-sliced `^VIX`/`^GSPC`; gated behind
  `SHORTS_ENGINE_ENABLED` so long-only baselines stay clean. `compute_metrics.by_direction` splits the
  long/short cohorts.

## 2. What is DONE

- **Both families built + unit-tested** (`tests/unit/services/test_shorts_signals.py`, plus
  `test_shorts_engine_io.py`). Full suite green throughout.
- **Isolation validation** (bespoke harness, pooled top/bear regimes, de-duped):
  - Breakdown: **92 signals, WR 44.4%, +0.2846R** avg (survives ~0.1R friction → ~+0.18R net). Both
    gates pass (n ≥ 30, edge > breakeven).
  - Fade-top: **29 signals, WR 52.2%, +0.49R net** (n < 30 accepted as noise-but-strong; `distribution`
    carries it, 24/29). Breakdown thinned to +0.049R under the broad 27-sym universe → families want
    different universes (breakdown tighter, fade-top broader).
- **Orchestrator integration** complete (Option 3), verified on a live batch (no dup assets, ≤2 shorts/
  class). Surfaced + fixed the EMA200 live/backtest parity bug as a side effect (see engine-parity
  changelog).
- **Walk-forward wiring** complete (API `POST /api/backtest/walkforward`, `by_direction` metrics).

## 3. BLOCKER — negative out-of-sample edge

The isolation numbers did **not** survive an honest walk-forward:

- **Full-universe run** (`951eec19`, ~80 syms, 2021-11→2024): OOS shorts **112 trades, WR 14.3%
  (resolved 21.6%), avg −0.0823R** — below the ~33% breakeven. Longs stable (+0.1235R) = real edge.
- **Curated run** (restricted to the 27-sym `SHORT_UNIVERSE`): still negative — OOS shorts avg
  **−0.66R**, resolved WR ~4%, a textbook IS→OOS overfit collapse (IS shorts +0.42R / 45% WR).

The isolation harness's positive result came from three thumbs on the scale (bear/top-only windows +
curated universe + de-dup); strip them and the edge is gone. **Shorts are DISABLED** pending diagnosis.

## 4. NEXT — Tier-1 diagnosis (the active task)

Determine whether this is an **activation** problem (right signals, wrong times/regimes) or a **signal**
problem (the setups have no forward edge):

1. Break the OOS short losses down by **regime** and by **year/window** — is the loss concentrated in
   specific regimes/periods, or uniform?
2. Break down by **family** (breakdown vs fade-top) and by **trigger** — does any single trigger carry a
   real edge that the pooling masks?
3. Compare activation frequency IS vs OOS — are we firing far more in OOS (overfit activation) than the
   isolation harness implied?
4. **Decision gate:** if a subset (a regime × family × trigger) shows a robust, friction-surviving OOS
   edge → keep only that, re-validate, consider re-enabling. If nothing survives → shelve the shorts
   engine (keep the code, flag off) and record the negative result.

Only after a subset clears an honest walk-forward (n ≥ 30, survives friction, degradation ≤ ~1.1) does
`SHORTS_ENGINE_ENABLED` flip back on.

## 5. Deferred / out of scope

- Full **portfolio-aware backtest** (capacity + conflict, not per-signal) — a separate, larger project.
- Single-window backtest loop (`replay.py:146`) is not shorts-wired (only `_replay_window` /
  walk-forward is).
- Per-family universe split (breakdown tighter / fade-top broader) — a tuning follow-up, only relevant
  if the engine is revived.
