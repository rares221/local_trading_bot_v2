# Shorts Engine — Consolidated Plan (breakdown + fade-top)

> **STATUS: CONCLUDED (2026-09-23).** `SHORTS_ENGINE_ENABLED = False`, staying off. This is not
> "paused pending more tuning" — it's a closed investigation. Five configurations were built and
> honestly validated (original design; Phase 1 with RS-gate; Phase 1 with the RS-gate ablated;
> Phase 2 confirmation-bar delay on top of Phase 1) across **four separate historical crisis
> episodes** (2018 Q4, 2020 COVID, 2011 debt-ceiling, 2015-16 China/oil) plus two full multi-year
> walk-forward checkpoints. Every configuration either failed on profitability or, in Phase 2's
> case, collapsed activation so far that no resolvable sample remained at all (0 resolved trades
> in a 3-year checkpoint). Full evidence in §6-7 below.
> **Next:** the codebase-modification avenue is closed; the open avenue is a genuinely different
> data source (short interest, options flow, breadth, forensic-accounting red flags) — external
> research on this is in progress (§8, added 2026-09-23) rather than more gate-tuning on daily-bar
> TA.
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

## 6. Phase 2 (confirmation-bar entry delay) — BUILT, TESTED, VALIDATED, MADE THINGS WORSE (2026-09-23)

Architecturally-brainstormed, TDD-implemented two-stage entry: `try_arm_short` (detect, don't enter) →
`check_confirmation` (each subsequent day, up to 3 trading days: re-check the family's structural gates
against fresh data, confirm if that day's close breaks below its 10-day EMA, else stay pending, else
expire) → `finalize_short_signal` (score/level using the *confirmation* day's data, not the arm day's).
Live persists the pending-arm state in a new `armed_shorts` table (migration 009); replay uses an
equivalent in-memory dict local to `_replay_window`'s day-loop. 386 unit tests green.

**Validated on the SAME already-spent 2021-2024 checkpoint (directional check) plus two genuinely fresh
blind-holdout episodes (2011 debt-ceiling/European crisis, 2015-16 China/oil selloff — 2018 Q4 and 2020
COVID were already spent from Phase 1's holdout):**

- **Checkpoint (2021-2024): activation collapsed to near-nothing.** 10 IS signals total across 8
  windows / 3 years (down from Phase 1's 34, down from the original design's 935), and **all 10
  expired** — not one hit its stop or its target, in-sample or out-of-sample. Resolved win rate is
  undefined; there's no sample left to evaluate.
- **Blind holdout: zero signals in the 2011 debt-ceiling crisis** (another canonical extreme episode,
  joining 2020 COVID's zero from Phase 1's holdout), and only 2 signals across the entire 5+ month
  2015-16 selloff.
- Stacking the confirmation delay on top of Phase 1's RS-gate didn't fix the win-rate problem — it
  compounded the *activation* problem instead. The gate stack (regime + EMA200 + RS-gate + volume +
  confirmation-delay) is now so restrictive in combination that it essentially never fires with
  conviction, independent of whether the underlying pattern has real edge.

**Across everything tried this project (5 configurations × up to 4 historical crisis episodes each),
every single test either failed on profitability or failed to produce a testable sample.** That is a
consistent signal, not one bad run. Per systematic-debugging discipline, this many failed
configurations means the problem is architectural (daily-bar TA + these trigger definitions don't carry
robust short-side edge here), not a threshold worth one more tweak.

## 7. CONCLUSION (2026-09-23) — shorts effort closed, not paused

**Decision: the TA-based shorts engine investigation is concluded.** `SHORTS_ENGINE_ENABLED` stays
`False` indefinitely — this is not "revisit later with fresh eyes on the same approach." The code is
retained (both families, Phase 1 gates, Phase 2 arm/confirm state machine) as a reference for whichever
of the two paths below gets pursued, but no further gate-tuning, threshold-adjusting, or entry-timing
work is planned on the current architecture.

**What's left, if this is ever revisited, is not more of the same:**
1. ~~Phase 2 anyway~~ — done, made things worse (§6). Ruled out.
2. **A genuinely different data source** (short interest / borrow data, options flow, market breadth,
   forensic-accounting red flags à la Kathryn Staley/Beneish M-score) — what the research most strongly
   pointed to from the start. External research on feasibility and prior art is in progress as of
   2026-09-23 (§8) rather than more work on the current daily-bar-TA architecture.

**Reusable tooling from this project** (all in `scripts/`):
- `diagnose_shorts_tier1.py` — instrumented walk-forward with full per-trade breakdown
  (family/trigger/regime/year/confidence). `docker-compose exec -T app python -m scripts.diagnose_shorts_tier1`.
- `validate_shorts_blind_holdout.py` (2018 Q4 + 2020 COVID) and `validate_shorts_blind_holdout_phase2.py`
  (2011 debt-ceiling + 2015-16 China/oil) — both now spent; a future revival needs entirely fresh episodes.
- `diagnose_shorts_rs_ablation.py` — pattern for isolating one gate/threshold's effect on the spent
  checkpoint data without touching a blind holdout; reusable as a template.

## 8. External research on a different data source (DONE, 2026-09-23) — the 14-day horizon is the real blocker

Three parallel research angles (signal-family/horizon academic literature, open-source/GitHub prior art,
data-source feasibility) converged independently on the same conclusion:

- **Every short-side factor family with real, replicated edge clusters at a 1-6+ month holding
  horizon**, several (accruals/Beneish/Montier, quality-minus-junk) locked to quarterly/annual
  fundamental-data cadences that structurally cannot compress to 14 days. The one family built purely
  from price/volume — momentum's short leg — is explicitly characterized in the literature (Daniel &
  Moskowitz) as **a risk exposure to be hedged, not a return source to harvest**: direct academic
  confirmation of our own empirical finding, not a coincidence.
- **There's a named theoretical mechanism, not just a pattern**: Diamond & Verrecchia (1987) show
  formally that short-sale constraints structurally slow how fast negative information gets priced in.
  Chanos's own SEC testimony: shorts must be held "often months — until the market realizes." This
  isn't a data-mining artifact of any one paper — the delay is a first-order feature of how short-side
  mispricing resolves.
- **No credible open-source project demonstrates real short-side edge at a daily-bar, 14-day horizon.**
  The two most rigorous, honest OHLCV-only open-source projects found (`RaulJaspal/Stockpredict`,
  `nalimmm/short-term-reversal`) report the **identical failure signature** we did: in-sample-plausible,
  out-of-sample statistically indistinguishable from zero, traced to momentum/upward-drift overwhelming
  the short signal. Activist short-sellers (Muddy Waters, Hindenburg, Citron, Kerrisdale) — the one
  domain with a genuine, repeatable track record of profitable shorting — is confirmed to be manual,
  low-frequency (a handful of reports/year/firm), ~1-year-horizon forensic investigation. Nobody has
  systematized it into a daily rules engine; the underlying work (site visits, document review) is
  inherently incompatible with an OHLCV-only, 14-day system.
- **Data feasibility for the two least-bad leads, if ever pursued as a genuinely separate project**:
  (1) SEC EDGAR XBRL fundamentals — completely free, sufficient for Beneish M-score/Sloan accruals —
  but the edge is only validated at a **12-month hold**, and that's not an access-latency problem to
  fix, it's simply how often the underlying data changes. (2) Ortex ($39/mo) — the one provider with
  daily-refreshed short-interest/crowding/borrow data, solving FINRA's free-but-disqualifying 8-23-day
  staleness — but even with fresh data, the short-interest factor itself is inherently a
  monthly-rebalance phenomenon per the constraint mechanism above, not a 14-day one.

**Conclusion: this is not "we haven't found the right technique or data source yet."** All three
independent research angles agree the 14-day horizon itself is very likely the actual structural
blocker for ANY known systematic short-side edge — not a gap in our effort. Since this app is a "14-day
prediction" tool by design (a core, stated constraint, not specific to shorts), a short-selling feature
compatible with that design does not appear to exist in the current evidence base. Pursuing shorts
further would mean building a genuinely different, longer-horizon (1-12 month) product with different
data — a separate initiative, not a continuation of this one.

## 9. Deferred / out of scope

- Full **portfolio-aware backtest** (capacity + conflict, not per-signal) — a separate, larger project.
- Single-window backtest loop (`replay.py:146`) is not shorts-wired (only `_replay_window` /
  walk-forward is).
- Per-family universe split (breakdown tighter / fade-top broader) — a tuning follow-up, only relevant
  if the engine is revived.
