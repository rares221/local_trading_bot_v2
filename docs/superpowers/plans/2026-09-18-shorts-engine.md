# Shorts Engine — Consolidated Plan (breakdown + fade-top)

> **Status (2026-09-18 evening):** Tier-1 diagnosis done; a Phase 1 redesign (RS-vs-market gate,
> volume confirmation, horizon-matched R:R sizing, flattened trigger offsets) was built, tested (373
> green), independently code-reviewed, and validated across 3 configurations — **all three fail the
> success bar** (OOS expectancy ≥+0.15R, n≥40 resolved, ≥2 blind-holdout episodes). Verdict: **shorts
> stay disabled** (`SHORTS_ENGINE_ENABLED = False`). Not a "retune and try again" situation — see
> §6 below for the full evidence and what's actually left to try.
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

## 4. Tier-1 diagnosis (DONE, 2026-09-18) — root cause found

Ran the instrumented walk-forward (curated universe, 2021-11→2024-12) with raw per-trade capture
(`scripts/diagnose_shorts_tier1.py`). Findings:

- **Both IS and OOS were dominated by expiry, not stop-outs**: IS expired_rate 72.3%, OOS 54.7%.
  Resolved win rate was already only 17.5% IS (below the 33% breakeven a 2:1 target needs) before
  collapsing further OOS. Root mechanism: the fixed 2.0 R:R floor + "only ever push TP further away"
  stretch created targets that were structurally unreachable in 14 days — shorts fight the market's
  upward drift to get there, so a target sized the same way as a long's (which gets help from that
  drift) rarely resolves at all.
- Backed out that in-sample, unresolved ("expired") trades averaged **+0.52R mark-to-market** (price
  was moving the right way, just not far enough) — but out-of-sample that same bucket was ≈ **+0.01R**,
  essentially flat. That's a genuine OOS directional-edge collapse in the trigger definitions
  themselves, layered on top of the target/horizon mismatch.
- `failed_retest` — the trigger assigned the *highest* confidence bonus ("ordered by precision") — was
  empirically the **worst** performer (67 OOS trades, 0 wins). The precision ordering was backwards.
- Confidence score carried **no positive predictive signal** for shorts (no monotonic relationship with
  outcome).
- External research (3 parallel agents: structural short-edge literature, short-selling pattern
  validity, backtest-overfitting methodology) converged hard with this: momentum-crash literature, the
  equity risk premium, and established short-selling practitioner rules (O'Neil, Staley) all point to
  the same missing pieces — relative-strength-vs-market confirmation, volume confirmation on breakdown
  failures, and a confirmation-bar delay before entry (don't enter on the trap leg).

## 5. Phase 1 redesign — BUILT, TESTED, VALIDATED, FAILED THE BAR (2026-09-18 evening)

Brainstormed an architectural (not bounded) redesign; approved path = **phased**: cheap stateless fixes
first, checkpoint, only add the expensive stateful confirmation-bar delay (Phase 2) if the checkpoint
fell short.

**Phase 1 (stateless) — shipped:**
- RS-vs-market hard gate: breakdown requires `rs_20d < 0.90` (confirmed weakness vs SPX); fade-top
  requires `rs_60d > 1.10` (confirmed prior leadership) — reuses the existing, already-calibrated
  `compute_relative_strength`. Fails closed on missing data.
- Volume confirmation extended to all 3 breakdown triggers (was only on `support_breakdown`).
- **Horizon-matched R:R sizing** — cap the TP stretch at `avg_daily_move × 10 days` reachable distance,
  lower the floor to 1.5, reject if still unreachable, instead of blindly stretching to a fixed 2.0.
  **This part is verified correct and valuable on its own** — cut the expiry rate roughly in half
  everywhere tested (72%→32% IS, 55%→33% OOS).
- Flattened `TRIGGER_BASE_OFFSET` (removed the falsified "precision ranking").

373 unit tests, independently code-reviewed (sign conventions / RS-gate boolean logic / sizing
arithmetic all traced with concrete numbers — no bugs found).

**Validation — tested across 3 configurations, all fail the success bar (OOS expectancy ≥+0.15R,
n≥40 resolved, holds on ≥2 blind-holdout episodes):**

| Config | OOS resolved WR | OOS avg pnl_r | n resolved |
|---|---|---|---|
| Original (pre-Phase-1) | ~2–22% (universe-dependent) | −0.66R to −0.08R | varies |
| Phase 1 with RS-gate (checkpoint, 2021-2024) | 16.7% | −0.22R | 6 (thin) |
| Phase 1, RS-gate ablated (isolates that variable) | 14.3% | −0.19R | 21 (larger, still fails) |

- **Blind holdout** (2018 Q4 selloff + 2020 COVID crash — never touched during design): only 1 signal
  total across both episodes, **zero during the entire COVID crash** — the single most extreme bear
  scenario available. The RS-gate structurally suppresses activation in exactly the highest-conviction
  systemic-crash case: a synchronized, market-wide crash *compresses* single-stock RS-vs-index
  dispersion (everything falls together) rather than widening it, so "confirmed relative weakness vs the
  broad index" rarely shows up even when a stock is genuinely breaking down.
- **The RS-gate ablation ruled out "the gate is the problem"**: removing it made in-sample performance
  look great (49.5% WR, +0.34R) while out-of-sample got *worse* in relative terms (still −0.19R, on a
  larger, more reliable sample of 21 resolved trades). That's the textbook overfitting signature getting
  more pronounced, not resolving — the underlying trigger definitions don't have real OOS directional
  edge, with or without the RS-gate.

**Decision:** `SHORTS_ENGINE_ENABLED` stays `False`. Per systematic-debugging discipline, 3 tested
configurations failing the same way (in-sample looks fine or at least not-worse, out-of-sample
decisively negative) means this is architectural, not a threshold to keep nudging. All three research
agents independently flagged, before any of this validation ran, that pure daily-bar technical-analysis
shorting may lack robust edge at this fidelity without a different data class. This validation confirms
that concern rather than a specific implementation bug — the horizon-sizing bug WAS real and IS fixed,
but fixing it exposed rather than fixed the deeper signal-quality gap.

## 6. What's actually left, if this gets picked up again

Two live options — neither is "retune the same thresholds again" (2021-2024 is well and truly spent
across 3 iterations now; any further work needs genuinely fresh validation data):

1. **Phase 2 anyway** (stateful confirmation-bar entry delay: arm on the trigger day, require a
   subsequent day's close beyond a short MA before actually entering). Unproven this would help — the
   ablation suggests the problem is signal quality (no real OOS directional edge), not entry timing,
   which Phase 2 doesn't address. Would need a new architecture (stateful, touches both replay and
   live) and a fresh blind-holdout episode (e.g. 2015-16 China/oil selloff, Aug 2011 debt-ceiling
   selloff — NOT 2018 Q4/2020 COVID, both now spent) before it could be trusted.
2. **Approach C — a different data class entirely** (short-interest/borrow data, options flow, market
   breadth) — what the research most strongly points to as the actually-correct fix if pure TA has no
   edge here. Bigger scope, effectively a new project, likely needs a paid data source.

**Reusable tooling from this session** (all in `scripts/`):
- `diagnose_shorts_tier1.py` — instrumented walk-forward over 2021-2024 with full per-trade breakdown
  (family/trigger/regime/year/confidence). `docker-compose exec -T app python -m scripts.diagnose_shorts_tier1`
  (~35-40 min).
- `validate_shorts_blind_holdout.py` — 2018 Q4 + 2020 COVID (now spent — don't reuse without adding a
  genuinely fresh episode alongside).
- `diagnose_shorts_rs_ablation.py` — pattern for isolating one gate/threshold's effect without touching
  the blind holdout; reusable as a template for testing future hypotheses on the spent checkpoint data.

## 7. Deferred / out of scope

- Full **portfolio-aware backtest** (capacity + conflict, not per-signal) — a separate, larger project.
- Single-window backtest loop (`replay.py:146`) is not shorts-wired (only `_replay_window` /
  walk-forward is).
- Per-family universe split (breakdown tighter / fade-top broader) — a tuning follow-up, only relevant
  if the engine is revived.
