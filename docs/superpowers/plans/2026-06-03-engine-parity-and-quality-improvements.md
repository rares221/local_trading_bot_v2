# Engine Parity & Prediction-Quality Improvements Plan

> **Companion to** `.cursor/plans/shorts-recovery-plan_fb6be623.plan.md`. That plan covers the
> separate shorts engine. THIS plan covers the non-shorts issues found in the 2026-06-03 codebase
> scan. The two plans share one prerequisite (regime taxonomy unification) — do it once, both benefit.

**Goal:** Make the live recommendation path, the daily batch engine, and the backtest replay produce
consistent, comparable signals; close the largest correctness and parity gaps; and add the missing
test scaffolding so future changes are safe.

**Scope note:** This is a phased plan with file/line anchors and acceptance gates, not line-by-line
TDD. Each phase is independently shippable and ordered by impact on prediction quality.

---

## Background: three signal paths that have drifted apart

The app scores the same asset in three places that are supposed to agree but do not:

```mermaid
flowchart LR
  subgraph batch [Daily batch - source of truth]
    E["engine._build_candidate\nfull Arc 5 bonuses + gates"]
  end
  subgraph bt [Backtest]
    R["replay._build_signal\nmirrors most of engine"]
  end
  subgraph live [Live streaming]
    L["live_analyzer._compute_signal_sync\ncompute_confidence + compute_live_confidence"]
  end
  E -. "should match" .- R
  E -. "should match" .- L
  R -. "diverges" .- L
```

- **Batch engine** (`engine.py:_build_candidate`, ~1148-1960): the full model — Arc 5 confidence
  bonuses, soft-penalty pool, hard gates (crisis/high_vol, RSI exhaustion, OFI, earnings, conflict),
  market-context adjustments, policy scaling.
- **Replay** (`replay.py:_build_signal`, 204-521): mirrors most Arc 5 bonuses and soft penalties, but
  is missing several hard gates and adjustments (see Phase 3).
- **Live** (`live_analyzer.py:_compute_signal_sync`, 293-447): does **not** run engine scoring at all.
  It blends `compute_confidence()` with `compute_live_confidence()` (a backtest-win-rate blend,
  `live_confidence.py:86-125`) and applies almost none of the Arc 5 bonuses or hard gates.

Net effect: a trade shown live can be one the batch engine would never recommend, and backtest
metrics (which feed `compute_live_confidence`) describe a fourth, slightly different population.

---

## Phase 0: Regime Taxonomy Unification (SHARED PREREQUISITE)

Identical to Phase 4.5 in the shorts plan — track it in one place, not two.

**Problem:** three regime taxonomies that do not align:
- Live 3-label `MarketRegime.regime` ∈ {risk_on, risk_off, cautious} — `market_context.py:89`
- Live 5-label `MarketRegime.extended_regime` ∈ {crisis, high_vol, bull, bear, neutral} — `market_context.py:97-117`
- Backtest `day_regime` ∈ {crisis, high_vol, bull, bear, neutral} (no risk_off) — `regime.py`

**Defects:**
- **`bear` is structurally inverted on VIX between live and backtest — NOT just a threshold offset.**
  Live `extended_regime` (`market_context.py:113-116`) defines `bear` = bearish trend **AND VIX < 25**
  (it is the fall-through after the crisis≥35 / high_vol≥25 checks). Backtest `regime.py:64` defines
  `bear` = SPX below 200-SMA **AND VIX ≥ 20**. The VIX conditions point opposite directions: a day
  with SPX falling and VIX in [25,30) is `high_vol` live but `bear` in backtest. For the shorts engine
  this is a direct conflict — the same day is short-**blocked** in one path and short-**eligible** in
  the other. Aligning the threshold *numbers* (next bullet) does not fix this; the boolean rule itself
  must be unified.
- **`spx_trend` uses different SMAs across paths.** Live requires price below **both** SMA50 and SMA200
  to be "bearish" (`market_context.py:141-146`); backtest `bear` uses **only** the 200-SMA
  (`regime.py:64,84-100`). The canonical helper must standardize the trend definition, not just VIX.
- VIX thresholds differ: extended_regime crisis≥35/high_vol≥25 vs backtest crisis≥40/high_vol≥30.
- `determine_direction()` / `compute_confidence()` only branch on the 3-label scheme
  (`technicals.py ~1195-1240`); replay feeds them 5-label strings that silently fall back to defaults.
- **The dead `== "bear"` / 5-label-vs-3-label branch exists in THREE live locations, not one:**
  - `engine.py:1252` and `engine.py:1798` compare `regime.regime == "bear"` — 3-label, never "bear".
  - `live_analyzer.py:354,357` short gates check `regime in ("crisis","high_vol")` and
    `regime == "bear"`, but `build_all_signals` feeds them `ctx.regime` (3-label, line 464) — both
    gates can never match. The crisis/high_vol short block and the bear exception are dead in live.
  - `stream_processor.py:379,393`: `_get_regime_summary()` (line 962) returns `regime.regime` (3-label),
    so `_regime_label == "bear"` is always false — the bear-regime short floor exception is dead here too.

**Tasks:**
1. Introduce a single regime-mapping helper (e.g. `app/services/regime_map.py`) that converts any
   taxonomy to one canonical label set and is the only place thresholds **and the trend/`bear` boolean
   rule** live.
2. Align VIX thresholds **and unify the `bear` definition** (trend rule + VIX direction) between
   `market_context.py` and `regime.py` so the same day gets the same canonical label.
3. Make `determine_direction` / `compute_confidence` accept the canonical labels (map 5→weight buckets).
4. Replace ALL dead 5-label-vs-3-label comparisons — not only `engine.py:1252,1798`, but also the
   `live_analyzer.py` short gates (feed them the canonical/extended label, not `ctx.regime`) and the
   `stream_processor.py` short floor (`_regime_label` must come from the extended label).
5. **Test:** a fixed VIX/SPX fixture yields the same canonical label in live and backtest for every day —
   asserting the same **boolean label** across paths (esp. the bear/high_vol boundary), not just matching
   threshold constants.

**Acceptance:** one regime helper, one set of thresholds **and one `bear`/trend rule**, no taxonomy-specific
string literals scattered across engine/replay/live/stream_processor; the bear/high_vol boundary classifies
identically in live and backtest; regression test green.

### LOCKED DESIGN (2026-06-05 brainstorming — ready to implement)

**Decisions locked:** (1) re-baseline now — consuming the 5-label moves Sharpe 3.114, walk-forward is the gate;
(2) canonical = `regime.py` rules; (3) dedicated bear/neutral profiles (bull inherits risk_on);
(4) scoring consumes the 5-label only, 3-label kept for UI + the engine risk_off long→short flip.
**Approach A:** new `app/services/regime_map.py` = classifier + profile accessors; `regime.py`,
`market_context.extended_regime`, and the four `technicals._regime_*` functions all delegate to it.

**§1 Module shape.** `regime_map.py` exposes:
- `classify_canonical_regime(vix: float|None, spx_vs_sma200: float|None) -> str` → `crisis|high_vol|bear|bull|neutral`
- `direction_weights(label)`, `tech_weights(label)`, `rsi_thresholds(label)`, `stoch_thresholds(label)` → the profile tuples.

**§2 Classification rule** (lifted verbatim from `regime.py:39-69`, priority order, first match wins):
`vix≥40→crisis · vix≥30→high_vol · (spx_vs_sma<0 AND vix≥20)→bear · (spx_vs_sma>0 AND vix<20)→bull · else→neutral`.
None-handling made explicit: a `None` vix or `None` spx_vs_sma collapses to `neutral` (matches regime.py fall-through;
backtest calendar unchanged).

**§3 Profiles — 5 labels → 4 buckets** (`crisis`/`high_vol` are hard-blocked before scoring, so they reuse the
most-defensive `bear` bucket as a safety net):

| Canonical label | bucket | dir (sent,tech) | tech (trend,MR) | rsi (bull,bear,OB,OS) | stoch (bull,bear,OB,OS) |
|---|---|---|---|---|---|
| bull | risk_on (inherited) | 0.20 / 0.80 | 1.2 / 0.85 | 52/48/72/28 | 52/48/85/15 |
| neutral | neutral (NEW) | 0.30 / 0.70 | 0.95 / 1.10 | 55/45/70/30 | 55/45/80/20 |
| bear | bear (NEW) | 0.32 / 0.68 | 0.78 / 1.28 | 63/37/60/40 | 63/37/72/28 |
| crisis / high_vol | → bear | (reuses bear values) | | | |

NEW-bucket rationale: **neutral** nudges tech toward mean-reversion (0.95/1.10) vs old default's 1.0/1.0;
**bear** is deliberately stricter than old risk_off — more sentiment weight (a long needs tech *and* news),
stronger MR tilt (only oversold bounces qualify in a downtrend), tighter bars. Values are calibration
starting points; the structure (bear stricter than neutral stricter than risk_on for longs) is the contract,
the decimals are what walk-forward tunes.

**§4 Call-site changes:**
- *Tier 1 — `technicals.py`:* the four `_regime_*` functions (`:1195/1208/1220/1232`) stop branching on
  `risk_on/risk_off` strings and delegate to `regime_map` accessors keyed on the 5-label. Signatures unchanged;
  internal consumers (`:1276/1284/1303/1383/1453`) need no edits.
- *Tier 2 — feeds switch to 5-label:* `live_analyzer.py:464` `regime = ctx.regime` → `ctx.extended_regime`
  (currently feeds 3-label all the way into `determine_direction`:337 / `compute_confidence`:387);
  `replay.py:260` already passes `day_regime` (5-label) — now actually consumed instead of silently defaulting;
  engine scorer feeds switch to `extended_regime`.
- *Tier 3 — dead branches now reachable:* `engine.py:1252` & `:1798` `regime.regime=="bear"` → `extended_regime=="bear"`;
  `stream_processor.py:958` `_get_regime_summary` returns `extended_regime` so `:379-384` `_regime_label=="bear"` can match;
  `engine.py:1208` already uses `extended_regime` ✓.
- `market_context.extended_regime`(`:98`) rewritten to delegate to `regime_map.classify_canonical_regime()` — this is
  what fixes the inverted-VIX-bear discrepancy.

**§5 Re-baseline + validation.** Phase 0 ships unflagged (correctness fix), but measured before/after. Full
walk-forward re-run → new Sharpe/WR/avg-R/maxDD/degradation become the new locked baseline (record old+new
side-by-side). Acceptance bar: degradation ratio stays ≤ ~1.1 (currently 1.019) and Sharpe regresses no more
than a stated tolerance; if bear/neutral profiles hurt, tune the §3 decimals (structure stays).

**§5 RESULT (2026-09-17) — PASSED.** Identical config both runs: 2023-05-26→2026-05-26, IS 252 / OOS 63,
conf 40, moderate, 7 windows. Baseline = stored WF run `6963a1e4` (pre-Phase-0). NEW LOCKED BASELINE below.

| OOS metric | Baseline (pre) | Phase 0 (new) | Δ |
|---|---|---|---|
| Sharpe | 1.699 | **2.845** | +67% |
| Avg P&L (R) | 0.0671 | 0.1053 | +57% |
| Win rate | 31.71% | 32.60% | +0.9pp |
| Total R | 285.9 | 597.1 | +109% |
| Max DD (R) | 250.1 | 224.0 | −10% (better) |
| Trades | 4,260 | 5,672 | +33% |
| Degradation | 1.0193 | **1.0445** | still "robust" (≥0.85), OOS>IS |

IS side for the record: Sharpe 1.679→2.762, avg-R 0.0658→0.1036, trades 17,739→22,488, MaxDD 289→353,
total-R 1167→2330. Acceptance met on all three gates (degradation ≤1.1 ✅, Sharpe up not down ✅, robust ✅).
The +33% OOS trades is the expected mechanism: pre-fix bull days silently used the `default` profile; post-fix
they use the more-lenient `risk_on` profile → more candidates pass, and they pass *profitably*. Bear/neutral
profiles required NO tuning. Note: run was direct `run_walkforward_backtest()` (console), NOT persisted to the
walkforward_runs table — so it does not show on the backtest page.

**§6 Testing.** (a) `regime_map` unit tests — table over the priority ladder + the three None-collapse cases;
(b) cross-path regression — same VIX/SPX series → identical label via live (`market_context.extended_regime`) and
backtest (`regime.classify_regime`), explicit cases on the bear/high_vol boundary (VIX 25-30, SPX falling) and the
VIX-20 bear edge; (c) profile-wiring — each label resolves to the expected bucket (bull→risk_on, crisis/high_vol→bear).

---

## Phase 1: Unify scoring across batch / replay / live (HIGH)

**Problem:** `live_analyzer.py` re-implements a reduced scoring model. Missing vs `engine.py`
(present in engine, and mostly in replay):

| Adjustment | engine.py | replay.py | live_analyzer.py |
|---|---|---|---|
| MACD+RSI dual alignment (+6) | 1442-1451 | 340-344 | missing |
| Volume surge (+5) | 1453-1458 | 345-346 | missing |
| RSI momentum (+4) | 1460-1468 | 347-352 | missing |
| Triple confluence (+5) | 1470-1479 | 353-358 | missing |
| ATR percentile (±3) | 1481-1488 | 359-364 | missing |
| Turn-of-month (+2) | 1490-1495 | 365-368 | missing |
| 52w high proximity (+8/+4) | 1497-1507 | 369-374 | missing |
| EMA200 sweet spot (±3) | 1509-1528 | 375-387 | missing |
| MTF alignment bonus | 1554-1555 | 407-434 | missing |
| Crisis/high_vol hard block | 1205-1209 | 228-231 | **missing (live never blocks)** |
| RSI exhaustion hard gate | 1267-1287 | missing | missing |
| OFI gates | 1533-1553 | missing | missing |
| Stale snapshot penalty | 1175-1177 | n/a | missing (is_stale set, not consumed) |

**Approach:** extract the post-`compute_confidence` adjustment pipeline from `_build_candidate` into a
shared, pure function — `score_candidate(snapshot, direction, regime, policy, context) -> ScoreResult`
— and call it from all three paths. Replay and live become thin adapters.

**Decision (locked): Option A — full live parity.** Live routes through the shared scorer and receives
the exact same bonuses, soft penalties, and hard gates as the daily batch engine. A live signal is
therefore one the batch engine would also produce. The historical-win-rate blend
(`compute_live_confidence`) is **not** part of gating — it is demoted to an optional, clearly-labeled
display overlay (see Phase 2), never a silent replacement of the engine score. Option B (a deliberately
reduced live model) was rejected: it leaves live and batch showing different numbers for the same asset.

**Progress (2026-09-17): Step 1 DONE — engine golden safety net (both paths).**
`tests/unit/services/test_engine_build_candidate_golden.py` (10 tests) pins current `_build_candidate`
output before extraction:
- market_ctx=None path (5): emit (ad_hoc long → conf 83, sl 95.0, tp 108.53, rr 1.71, score 1.15)
  + four skip paths (low-R:R, RSI exhaustion, OFI distribution, shorts-disabled).
- market_ctx path (5): stub MarketContext over "indices" — bull emit (conf 83), bull RSI68 emit
  (conf 73, ceiling widened to 70), bear RSI63/68 skip (ceiling capped 60), crisis hard-block.
Harness uses frozen mid-month date + monkeypatched build_snapshot/check_trend_alignment/
get_preferred_risk/velocity/get_sector_for_asset. Full unit suite **273 green**.

**Phase 0 FOLLOW-UP FIX (2026-09-17): RSI-exhaustion ceiling now 5-label.** engine.py:1275/1281
compared `regime_id` (switched to 5-label in Phase 0) against the dead 3-label `"risk_on"`/`"risk_off"`
→ the ceiling's regime-awareness was silently disabled. Fixed: `"risk_on"→"bull"` widens ceiling to 70,
`"risk_off"→("bear","high_vol","crisis")` caps it at 60. This lives in the **live/batch engine path only**;
replay has NO RSI-exhaustion gate (see Phase 1/3 gap table), so the walk-forward does NOT exercise it —
a WF re-run would be a null test. Validated by the market_ctx golden tests above instead. The Phase 0
re-baseline (OOS Sharpe 2.845) is unaffected and stands.

**Tasks:**
1. Extract the Arc 5 bonus + soft-penalty + hard-gate logic from `engine._build_candidate` into a new
   `app/services/recommendations/scoring.py` with no DB/IO dependencies. Define `ScoreResult`
   (final confidence, the gate decision + reason, and the itemized adjustments for debugging/UI).
   — IN PROGRESS via incremental byte-identical slices (each verified against the 10 engine goldens):
   - ✅ Slice 1 (2026-09-17): `apply_signal_bonuses(confidence, snap, direction, today)` — the Arc-5
     bonus block (macd_rsi, volume surge, rsi momentum, triple confluence, atr pct, turn-of-month,
     52w high, ema200 sweet spot). This is exactly what live_analyzer was missing.
   - ✅ Slice 2 (2026-09-17): `compute_soft_penalties(snap, direction, category, preferred_risk,
     vel_confidence_penalty)` — the pooled soft-penalty cap (adx, macd-alignment, volume, obv,
     macd-decay, rsi-slope, cap-tier), consolidating 3 scattered pre-scoring snippets + the pool.
   Both live in NEW `app/services/recommendations/scoring.py` (pure, IO-free) with direct unit tests
   `tests/unit/services/test_scoring.py` (8). Full suite **281 green**.
   - ✅ Slice 3 (2026-09-17): hard gates for live parity — `is_rsi_exhausted(snap, direction,
     regime_id, preferred_risk)` (+ `rsi_exhaustion_ceiling`) and `apply_ofi_gate(confidence, snap,
     direction) -> (confidence, skip)`. These are the two gates the Phase 1 table lists as missing in
     live. Engine re-pointed; direct unit tests added (scoring tests now 23). Full suite **296 green**.
   - ✅ COMPOSED (2026-09-17): `ScoreResult` + `score_candidate(...)` in scoring.py compose the canonical
     confidence pipeline (base `compute_confidence` → pooled soft-penalties → signal bonuses). ENGINE and
     LIVE both re-pointed to it — byte-identical (engine 10 goldens + live 6 gate tests green; unused
     `compute_confidence` imports removed). Direct unit tests (scoring suite now 26). Full suite **305 green**.
     REPLAY intentionally NOT re-pointed to score_candidate: its base confidence is consumed mid-pipeline
     by the short-filter (bear-regime floor on base), so it keeps calling `compute_soft_penalties` +
     `apply_signal_bonuses` directly (already shared). Hard gates (RSI-exhaustion/OFI) + path-specific
     adjustments (market_ctx, earnings, conviction) stay caller-side by design — placement differs per path.
   - ⏳ Remaining (optional cleanup): extract the engine's market_ctx/earnings/conflict adjustment blocks
     too if a single monolithic scorer is wanted; not required for parity (behavior already shared).
   - ✅ Task 4 PARTIAL (2026-09-17): WIRED the two hard gates into `live_analyzer._compute_signal_sync`
     — `is_rsi_exhausted` (after the short-gate block) + `apply_ofi_gate` (after conviction, before the
     bear-short floor). `preferred_risk` (= `get_preferred_risk()`) threaded through `build_all_signals`.
     Before/after on fixed fixtures (regime=bull/bear, moderate): overbought long RSI90 28→SKIP,
     distribution long OFI0.30 42→SKIP, bear RSI63 28→SKIP, mild-dist OFI0.38 42→37 (nudge), healthy
     42→42, bull RSI68 42→42 (regime-aware ceiling). Regression test `test_live_analyzer_gates.py` (5).
     Full suite **301 green**.
   - ✅ Task 4 — LIVE CONFIDENCE PARITY (Option A, 2026-09-17): `_compute_signal_sync` now gates on the
     engine pipeline — base `compute_confidence` → pooled `compute_soft_penalties` (incl. sentiment-
     velocity fade + strong-reversal hard-skip) → `apply_signal_bonuses`. The `compute_live_confidence`
     backtest-winrate blend is DEMOTED to a display-only overlay (new signal field `historical_confidence`),
     no longer gating. Before/after (fixed fixtures): healthy long 42→83 (== engine golden), and crucially
     healthy-no-stats vs healthy-good-stats now BOTH 83 (were 42 vs 70) — gating confidence decoupled from
     historical win rate, which now shows only in the overlay (42 vs 70). OFI-mild 37→78. Regression tests
     updated (`test_live_analyzer_gates.py`, 6). Full suite **302 green**.
     LLM-floor DECISION: real-universe scan (23 signals post-gates): min 6 / median 28 / max 72;
     **7 clear the 40 floor** (10 clear 35, 5 clear 50). KEEP `_MIN_CONFIDENCE_LONG=40` — 7 LLM-eligible
     signals is healthy (not starved/flooded); no re-tune needed. Precise reconciliation of 40 vs the
     engine's per-category min-confidence + policy offsets stays Phase 1 Task 6. Aside: scan surfaced
     delisted tickers still in the live universe (SWEEP/CFD/ZEC/PXD) — pre-existing universe staleness,
     unrelated, worth a cleanup pass.
   - ✅ Task 3 — RE-POINTED `replay._build_signal` (2026-09-17): replaced its inline soft-penalty pool
     + signal-bonus blocks with `compute_soft_penalties(snap, direction, category, "moderate", 0)` and
     `apply_signal_bonuses(confidence, snap, direction, sim_date)`. Replay's inline code was already
     line-for-line identical to scoring.py (vol_floor 0.75 = moderate, no velocity → vel_pen 0,
     sim_date = today), so this is BYTE-IDENTICAL — walk-forward baseline unchanged (verified: 7 fixed
     fixtures 83/60/83/73/71/83/79 identical before/after; full suite 302 green incl. replay short tests).
     Deliberately did NOT add RSI-exhaustion/OFI gates to replay (genuinely absent there; adding them
     would move the baseline — that's a separate decision). No WF re-run needed (byte-identical proof).
   Seam confirmed: engine keeps all IO, passes materialized values to pure scoring fns; risk_off
   long→short flip stays in engine (needs SHORTS_ENABLED + policy).
2. Re-point `_build_candidate` at it (behavior-preserving refactor; lock with a golden test on a few
   fixed snapshots so output is byte-identical before/after).
3. Re-point `replay._build_signal` at the shared function; delete the mirrored copy.
4. Re-point `live_analyzer._compute_signal_sync` at the shared function (Option A). Remove the
   reduced/duplicated scoring there; the engine score becomes the live confidence that gates.
   `compute_live_confidence` is no longer called in the gating path — if kept at all, it is computed
   separately and attached only for display (Phase 2).
5. Make live consume the gates the shared scorer enforces — including the crisis/high_vol hard block
   (`engine.py:1205-1209`), the RSI exhaustion gate, and `is_stale` handling — which live skips today.
6. Reconcile the live LLM eligibility floor (`stream_processor.py:_MIN_CONFIDENCE_LONG = 40`) with the
   engine's category min-confidence + policy offsets so the same asset is not eligible in one path and
   filtered in the other.

**Acceptance:** ONE scoring implementation imported by engine, replay, and live; the golden test proves
the batch engine's output is unchanged by the refactor; and a fixed snapshot scores **identically** in
engine vs replay vs live (the live display overlay, if any, is additive metadata and does not change the
gating score or decision).

**Risk:** highest-value but highest-touch phase, and Option A widens the blast radius to the live path.
Sequence to de-risk: ship Task 1-2 (extract + golden test on engine) first; then Task 3 (replay); then
Tasks 4-6 (live) as a separate change so live parity can be validated/rolled back independently. Before
enabling, diff a live scan against a batch run on the same universe/day and confirm the gating scores
match.

---

## Phase 2: Confidence model & calibration coherence (HIGH)

**Problem:** at least three confidence numbers exist and none agree:
- Batch gates on raw `confidence_score` + policy offsets (`engine.py:346-355, 2075+`).
- Live blends in historical win rate via `compute_live_confidence` (`live_confidence.py:86-125`).
- Platt calibration is computed but **display-only** (`routes.py:228-233`, `calibration.py:92-104`);
  it never gates a trade, and `confidence_bucket()` (`routes.py:201-211`) buckets the raw score, so the
  UI can show a calibrated probability next to a bucket derived from a different number.

**Decision (locked by Phase 1, Option A):** the gating confidence is the shared engine score. This phase
finishes the cleanup so the UI and calibration agree with it.

**Tasks:**
1. Confirm the shared engine score (from Phase 1) is the single number that gates trades in all paths.
2. `compute_live_confidence` is already removed from the gating path in Phase 1. Here, either keep it as
   an explicit, separately-labeled display overlay ("historical hit-rate adjustment") or remove it
   entirely — decide based on whether the overlay still adds UI value once live shows the engine score.
3. Make calibration either (a) gate consistently or (b) be clearly labeled display-only everywhere,
   including making `confidence_bucket()` operate on the same number the UI displays.
4. Document the chosen model in `docs/`.

**Acceptance:** one gating number; UI labels match what gates trades; no calibrated-vs-raw mismatch in
the same view.

---

## Phase 3: Close remaining replay↔engine gaps (MEDIUM)

After Phase 1, most gaps disappear via the shared scorer. The ones that need explicit decisions:

- RSI exhaustion hard gate (`engine.py:1267-1287`) — add to replay.
- Sentiment velocity hard skip (`engine.py:1333-1340`) — replay only adjusts score.
- OFI gates (`engine.py:1533-1553`), earnings/event penalties (`engine.py:1673-1715`), indicator
  conflict **skip** (`engine.py:1773-1794`) — add to replay or document why backtest omits them.
- Preference-aware volume floor: replay hardcodes 0.75 (`replay.py:319`, "Step 4 will make this
  preference-aware") — wire to preference.
- VP LVN +5 boost present in engine (`1730-1738`) but not in replay's partial VP block (`389-404`).

**Acceptance:** a documented diff table showing every intentional backtest/engine difference, with
zero unintentional ones.

---

## Phase 4: Data layer reliability (MEDIUM)

**Problems:**
- `_ohlcv_cache` (`technicals.py:22-87`) has no TTL and is **never invalidated by age** — once a symbol
  is fetched, the cached DataFrame is returned for the entire process lifetime until a scheduled job
  calls `clear_cache()`. This is not just a rate-limit-burst issue: `get_cached_price` (`:90-94`) returns
  the last bar of a possibly hours- or days-old cached frame on every call between cache clears. Verify
  how often `clear_cache()` actually runs against how often live scans read the cache.
- **The cache is global module state shared between live and backtest mode.** `fetch_ohlcv` applies
  `_apply_backtest_cutoff` (`:79,87`) to whatever is cached, but the cache key is **not** mode-aware. A
  frame populated in one mode is reused (and sliced) in the other; a short live period (90d) cached first
  can yield an empty slice for an older backtest cutoff → silent `None`. Confirm live and backtest never
  share a process, or key the cache by mode.
- `build_snapshot` falls back to a persisted stale snapshot on fetch failure (`technicals.py:744-757`);
  `is_stale` is set but **not consumed anywhere in live** (`live_analyzer._compute_signal_sync` never reads
  `snap.is_stale`) — a stale-fallback snapshot scores and gates identically to a fresh one (see Phase 1).
- Live analyzer: 10-thread pool over ~80+ symbols against a 0.8s-throttled yfinance lock
  (`market_data.py:29-33, 161-184`; `live_analyzer.py:136, 485-505`) ≈ 60-90s+ per scan; Finnhub
  routing only covers plain tickers (`market_data.py:38-43`), futures/FX/crypto stay Yahoo-only.

**Tasks:**
1. Add a TTL to the OHLCV/snapshot cache; treat expired entries as misses.
2. Consume `is_stale` in live (penalize or drop), consistent with the engine.
3. Audit Finnhub coverage for the live universe; document which symbols are Yahoo-only and the latency
   budget per scan.

**Acceptance:** no signal is emitted from data older than the TTL without being flagged stale; scan
latency documented with a target.

---

## Phase 5: Timezone / market-calendar correctness (MEDIUM)

**Problem:** widespread `date.today()` without timezone. The surface is **much larger than first
documented** — a full grep finds ~30 call sites across 9 modules, several of which directly affect
**gating/scoring**, not just logging. In a UTC Docker container these misalign with the US market
calendar (UTC rolls to "tomorrow" at 19:00–20:00 ET).

**Gating-relevant sites (fix first):**
- `engine.py:1320` — `expiry = date.today() + timedelta(days=expiry_days)`: trade expiry date can be off
  by a day in the evening ET, shifting how long every trade is held.
- `engine.py:1491` — turn-of-month bonus (±confidence).
- `engine.py:2329` — `is_high_impact_day(date.today())`: macro-day confidence penalty keyed to wrong day.
- `engine.py:2250` — `batch_date`.
- `earnings_calendar.py:52` — `(next_date - date.today()).days`: earnings-proximity gate.
- `macro_calendar.py:121,136,161` — macro-day detection.
- `scheduler.py:379,408,429` — active-trade expiry checks; `:116,740` batch ids / ranges.
- `simulator.py:46,62` — expired-outcome date.

**Lower-priority sites:** `routes.py:299,365,605,613,844,850,1041,1785,1813`, `briefing.py:122,157`,
`backtest_routes.py:301`, `market_data.py:381,404`, `research/llm.py:1218`, `research/__init__.py:85,265`,
`macro_calendar_fetcher.py:199`. Also `macro_calendar_fetcher.py:217,231` use deprecated `datetime.utcnow()`.

**Tasks:**
1. Centralize "current market date" in one helper anchored to US/Eastern.
2. Replace `date.today()` in the gating-relevant paths first, then the rest (leave pure logging alone).
3. Replace deprecated `datetime.utcnow()` with `datetime.now(timezone.utc)`.

**Acceptance:** turn-of-month, expiry, earnings-proximity, macro-day, and batch-date logic reference the
market calendar, not server TZ; a test pins behavior across a UTC-vs-Eastern date boundary (evening ET).

---

## Phase 6: Test scaffolding for critical untested modules (LOW but enabling)

**Currently untested** (existing tests are under `tests/unit/{backtesting,services,sentiment}`):
- `app/services/technicals.py` — snapshots, SL/TP, direction, confidence
- `app/services/recommendations/engine.py` — candidate building, filters, ranking
- `app/services/backtesting/replay.py` — long signal path (only shorts gates tested today)
- `app/jobs/scheduler.py` — job smoke tests
- `app/live_trading/*` — analyzer, live_confidence, stream_processor
- `app/api/*` — routes, backtest_routes, live_routes

**Tasks (prioritized):**
1. ✅ DONE (2026-09-17): `technicals`: golden tests for `compute_atr_levels` (long & short),
   `determine_direction`, `compute_confidence` on fixed snapshots — these underpin Phase 1's golden
   test. See `tests/unit/services/test_technicals_golden.py` (21 tests, values captured from live impl;
   includes a regime-sensitivity guard). Full unit suite still green.
2. `engine`: candidate-filter unit tests (RSI exhaustion, OFI, correlation/risk-budget via `_try_add`).
3. `replay` long path: at least one end-to-end window with asserted metrics.
4. Scheduler: smoke test that `job_daily_batch` persists candidates given a mocked builder.

**Acceptance:** the modules touched by Phases 1-5 have characterization tests before they are refactored.

---

## Phase 7: Config & housekeeping (LOW)

- Add `.env.example` documenting required keys (no `.env.example` exists today; `config.py:36` requires
  an LLM provider but nothing validates it at startup).
- Validate required settings at startup with a clear error.
- Triage dead/disabled paths:
  - FinBERT blend (`vader.py:90-96`) imports `transformers`/`torch`, which are **not in
    `requirements.txt`** — the blend silently never activates in the container, so production sentiment
    is VADER-only despite the code path. Decide: add the deps or delete the blend.
  - Backtest-stats job: only the **startup** auto-run is commented out (`scheduler.py:846-852`); the
    **cron job still runs** (`:853+`). Not a dead path — correct the assumption; just confirm the cron
    cadence is frequent enough given that `compute_live_confidence` blends these stats into live scores.
  - Duplicate `GC=F` in `trade_watcher.py:24` (`DEFAULT_WATCHLIST` lists it twice). Confirmed.
- Note: the short `pnl_r` test comment is stale (`test_simulator.py:118-120`) — `_pnl_r` already
  handles direction; update the assertion/comment.
- Note: `live_confidence.py:104-105` comment still says "0.50 cap → max ~30" but the code uses `0.70`
  (line 107). Cosmetic, but it is in a load-bearing confidence formula — fix the comment.

**Acceptance:** new contributor can configure from `.env.example`; startup fails fast on missing
required config; dead-path triage decisions recorded.

---

## Suggested execution order

1. **Phase 0** (regime unification) — unblocks both this plan and the shorts plan.
2. **Phase 6 Task 1** (technicals golden tests) — needed before refactors.
3. **Phase 1** (unify scoring) — highest quality impact.
4. **Phase 2** (confidence coherence).
5. **Phases 3-5** in parallel as capacity allows.
6. **Phase 7** anytime.

## Main files in scope
- `app/services/recommendations/engine.py` — extract scoring, fix regime field
- `app/services/recommendations/scoring.py` — NEW shared scorer
- `app/services/backtesting/replay.py` — re-point to shared scorer, close gaps
- `app/live_trading/live_analyzer.py`, `live_confidence.py`, `stream_processor.py` — route through shared scorer
- `app/services/technicals.py` — regime label handling, cache TTL, is_stale
- `app/services/market_context.py`, `app/services/backtesting/regime.py` — align thresholds
- `app/services/regime_map.py` — NEW canonical regime mapping
- `app/core/config.py` + `.env.example` — config hygiene
- `tests/unit/**` — new characterization tests
