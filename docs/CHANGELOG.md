# Changelog — Local Trading Bot v2

> Format: date · branch · what changed and why

---

## 2026-09-23 — `feature/shorts_fix`

### Shorts engine — Phase 2 (confirmation-bar entry delay) tried, made things worse — effort CONCLUDED

Picked up the shorts redesign where the 09-18 session left off, per the "what's left" options
recorded then: built Phase 2 (the stateful confirmation-bar entry delay), the more invasive of
the two remaining options. Full detail in `superpowers/plans/2026-09-18-shorts-engine.md` §6-8.

- **Built**: two-stage entry (`try_arm_short` → `check_confirmation` → `finalize_short_signal`,
  replacing the old one-shot `generate_short_signal`) — arm on the trigger day, wait up to 3
  trading days for a close below the 10-day EMA (re-checking the family's structural gates
  against fresh data each day), only then score/level using the confirmation day's data. Live
  persists the pending-arm state in a new `armed_shorts` table (migration 009); replay uses an
  equivalent in-memory dict local to its day-loop. 386 unit tests, independently designed via the
  full brainstorming flow (architecture approved section-by-section before implementation).
- **Validated** on the same 2021-2024 checkpoint (directional-only, already spent) plus two fresh
  blind-holdout episodes never touched during design: **Aug-Oct 2011** (debt-ceiling + European
  crisis) and **Aug 2015-Feb 2016** (China deval + oil crash) — 2018 Q4 and 2020 COVID were
  already spent from Phase 1's holdout.
- **Result: activation collapsed even further, and profitability didn't improve.** Checkpoint: 10
  in-sample signals total across 8 windows / 3 years, and **all 10 expired** — zero trades in the
  entire window ever hit a stop or a target, in-sample or out-of-sample. Blind holdout: **zero
  signals in the 2011 debt-ceiling crisis** (joining 2020 COVID's zero from Phase 1), only 2
  signals across the full 2015-16 selloff. Stacking the confirmation delay on Phase 1's RS-gate
  didn't fix the win-rate problem — it compounded the activation problem instead.
- **Decision: shorts effort CONCLUDED, not paused.** `SHORTS_ENGINE_ENABLED` stays `False`
  indefinitely. Five configurations now tested (original, Phase 1 with/without the RS-gate, Phase
  2) across four separate historical crisis episodes — every one either failed on profitability
  or failed to produce a testable sample. Per systematic-debugging discipline, that consistent a
  pattern across that many independent tests means the problem is architectural (daily-bar TA +
  these trigger definitions don't carry robust short-side edge here), not a threshold worth
  another tweak. No further gate-tuning is planned on this architecture.
- **Next:** external research (web + open-source/GitHub repo search) dispatched into whether a
  genuinely different data source (short interest, options flow, breadth, forensic-accounting red
  flags) could work where daily-bar TA didn't — a separate future project, not a continuation of
  this one.

---

## 2026-09-18 — `feature/shorts_fix`

### Docs housekeeping — one-plan-one-spec convention

The `docs/` folder was slimmed to a durable shape. Going forward we keep **one active plan and one
active spec** at a time; finished work is summarized here in the changelog (as the consequences of the
finished plan) and the plan/spec files are deleted rather than left to accumulate.

- **Kept:** `docs/AGENT_REFERENCE.md` (architecture reference), `docs/CONFIDENCE_MODEL.md` (confidence
  model source of truth), `docs/CHANGELOG.md`, the one active plan
  (`superpowers/plans/2026-09-18-shorts-engine.md`) and the one active spec
  (`superpowers/specs/2026-09-17-shorts-signal-design.md`).
- **Consolidated:** the three shorts plan files (breakdown-family, fade-top-family,
  orchestrator-integration) → a single `2026-09-18-shorts-engine.md`.
- **Deleted (outcomes captured below or already in code):** `docs/IMPROVEMENT_PLAN.md`,
  `docs/LIVE_TRADING_PLAN.md`, `docs/LLM_IMPROVEMENT_PLAN.md`, `docs/FUTURE_RECOMMENDATION_IMPROVEMENTS.md`,
  `superpowers/plans/2026-04-16-signal-quality-improvement.md`,
  `superpowers/plans/2026-06-03-engine-parity-and-quality-improvements.md`,
  the three shorts plans above, `superpowers/specs/2026-04-13-domain-sentiment-correction-design.md`,
  `superpowers/specs/2026-04-16-signal-quality-improvement-design.md`.

### Engine parity & quality plan — COMPLETE (consequences)

The engine-parity plan (`2026-06-03-engine-parity-and-quality-improvements.md`, Phases 0–7) shipped in
full. Net delivered state:

- **Phase 0 — regime taxonomy unification.** NEW `app/services/regime_map.py`
  (`classify_canonical_regime` + profile accessors). One canonical 5-label regime (crisis / high_vol /
  bear / neutral / bull) now feeds live, batch and backtest (fixed an inverted-VIX bear defect and an
  SMA-mismatch). Re-baseline PASSED: OOS Sharpe 1.699 → **2.845**, avg +0.067R → **+0.105R**, "robust".
- **Phase 1 — shared scorer.** NEW `app/services/recommendations/scoring.py`: the confidence pipeline
  (base `compute_confidence` → pooled soft-penalties → signal bonuses), plus `score_candidate`,
  RSI-exhaustion and OFI gates. Engine + live + replay now share it, so the three paths cannot drift.
  `compute_live_confidence` (historical win-rate blend) demoted to a display-only overlay
  (`historical_confidence`) — it no longer gates.
- **Phase 2 — confidence coherence.** Raw `confidence_score` is the single gating number in all paths;
  `confidence_bucket` buckets that same number; `confidence_calibrated` (Platt) is display-only, now
  labeled as such in the UI. NEW `docs/CONFIDENCE_MODEL.md` documents the model.
- **Phase 3 — replay↔engine parity.** Documented diff table of every intentional backtest/engine
  difference; confirmed zero unintentional divergence.
- **Phase 4 — data reliability.** OHLCV cache TTL (live-only; 15 min daily / 5 min intraday;
  `technicals._cache_expired`) so no signal is emitted off hours-stale bars. `is_stale` snapshots now
  discount confidence uniformly via `scoring.STALE_CONFIDENCE_PENALTY` (was a dead engine var + absent in
  live). Finnhub coverage audited (51 eligible / 24 Yahoo-only of the 75-symbol base; ~60–90s scan target).
  Earlier in the plan: fixed the **EMA200/52w lookback parity bug** — `build_snapshot` fetched 90d (~63
  bars) so `ema200` was `None` in every live snapshot; now fetches `period="1y"`, restoring the EMA200
  gate/bonus, the `compute_confidence` EMA200 filter, and the fade-top gate in live.
- **Phase 5 — timezone / market-calendar correctness.** NEW `app/core/market_time.py`
  (`market_date()` anchored to US/Eastern). Every `date.today()` app-wide (engine, scheduler, routes,
  earnings/macro calendars, simulator, briefing, market_data, research, backtest_routes) now uses it, so
  expiry / turn-of-month / earnings-proximity / macro-day / batch-date logic no longer misalign when the
  UTC container rolls past midnight ET. Deprecated `datetime.utcnow()` in `macro_calendar_fetcher`
  replaced with `datetime.now(timezone.utc)`.
- **Phase 6 — test scaffolding.** Golden tests for `technicals` and `_build_candidate`; shared-scorer
  tests; replay long-path e2e (build → simulate → metrics); scheduler daily-batch persistence smoke test.
- **Phase 7 — housekeeping.** `.env.example` + `validate_startup_config()` wired into startup; FinBERT
  dead blend deleted (VADER-only, no torch/transformers); duplicate `GC=F` in `trade_watcher` removed;
  stale comments fixed.

Test suite grew to **353 green** across the plan.

### Shorts engine (breakdown + fade-top) — built & validated in isolation, NEGATIVE out-of-sample → disabled

A separate shorts signal engine was built and integrated, then honestly validated and turned off. See
the active plan `superpowers/plans/2026-09-18-shorts-engine.md` and spec
`superpowers/specs/2026-09-17-shorts-signal-design.md` for the full state.

- **NEW `app/services/recommendations/shorts_signals.py`** — pure engine, two families: **breakdown**
  (support_breakdown / failed_breakout / failed_retest; bear regime, below EMA200; shared
  `score_candidate(direction="short")` + per-trigger offset + 60 floor) and **fade-top** (distribution /
  exhaustion; deteriorating tape via `_tape_deteriorating`, above EMA200; bespoke `top_quality_score`).
  Both hard-blocked in crisis/high_vol.
- **NEW `app/services/recommendations/shorts_engine_io.py`** — adapter glue: `SHORT_UNIVERSE` (27),
  `compute_tape_deteriorating`, `short_signal_to_candidate`, `build_short_candidates_for_category`.
- **Batch + walk-forward wiring** — Option-3 capacity in `build_daily_candidates` (shorts first, ≤2/class,
  longs fill to 5; conflict/correlation/risk-budget inherited via `_try_add`); per-signal shorts wired
  into `replay._replay_window` with `by_direction` metrics. All behind `SHORTS_ENGINE_ENABLED`.
- **Isolation validation (positive):** breakdown 92 signals / WR 44.4% / +0.2846R; fade-top 29 / WR
  52.2% / +0.49R net.
- **Walk-forward (negative):** OOS shorts net-negative both full-universe (112 trades, WR 14.3%,
  −0.082R) and curated (avg −0.66R, textbook IS→OOS overfit collapse). Longs stable IS→OOS = real edge.
- **Decision:** `SHORTS_ENGINE_ENABLED = False`. Code retained. Next step is a Tier-1 diagnosis
  (regime × family × trigger × year breakdown of the OOS losses) before any rework or re-enable.

### Shorts Phase 1 redesign — built, tested, validated across 3 configurations, all failed the bar (same day, evening session)

Deep-research (3 parallel agents) + Tier-1 diagnosis found the dominant OOS failure mode: a fixed 2.0
R:R floor forced short targets to a distance rarely reachable in 14 days (shorts fight the market's
upward drift to get there) — 72%/55% (IS/OOS) of trades simply timed out rather than resolving.
Redesigned and re-validated; still net negative. Full detail in
`superpowers/plans/2026-09-18-shorts-engine.md` §4-6.

- **Fixed and kept:** horizon-matched R:R sizing (cap the TP stretch at `avg_daily_move × 10 days`
  reachable distance, floor lowered to 1.5, reject rather than force an unreachable target) — verified,
  cuts the expiry rate roughly in half everywhere tested. Also added: RS-vs-market hard gate (breakdown
  requires confirmed weakness `rs_20d<0.90`; fade-top requires confirmed prior leadership `rs_60d>1.10`,
  reusing the existing `compute_relative_strength`), volume confirmation on all 3 breakdown triggers
  (previously only `support_breakdown` had it), flattened `TRIGGER_BASE_OFFSET` (the old "precision
  ranking" was empirically backwards — the highest-ranked trigger was the worst performer).
- **Validated across 3 configurations, all failed the success bar** (OOS expectancy ≥+0.15R, n≥40
  resolved, ≥2 blind-holdout episodes): Phase 1 with the RS-gate (thin sample, −0.22R); Phase 1 with the
  RS-gate ablated to isolate that variable (larger sample n=21 resolved, still −0.19R, and a *worse*
  IS→OOS collapse — 49.5%→14.3% WR — ruling out "the gate is the problem"). Blind holdout (2018 Q4 +
  2020 COVID, never touched during design) produced only 1 signal total and **zero during the entire
  COVID crash** — the RS-gate structurally suppresses activation in exactly the highest-conviction
  systemic-crash case, since a synchronized crash compresses single-stock relative-strength dispersion
  instead of widening it.
- **Decision: `SHORTS_ENGINE_ENABLED` stays `False`.** Three configurations failing the same way is the
  systematic-debugging "architectural, not parametric" signal — confirms rather than contradicts what
  all three research agents flagged going in: pure daily-bar technical-analysis shorting may lack
  robust edge at this fidelity without a different data class (short-interest, options flow, breadth).
  Next steps if revisited: either the stateful confirmation-bar entry delay (Phase 2, unproven — the
  problem looks like signal quality, not entry timing) or a different data class entirely (bigger,
  separate project). New reusable diagnostic scripts in `scripts/`: `diagnose_shorts_tier1.py`,
  `validate_shorts_blind_holdout.py`, `diagnose_shorts_rs_ablation.py`.

### Production bug: zero-R:R Volume-Profile TP snap (found + fixed same evening, unrelated to shorts)

Live symptom: today's first daily batch in ~3.5 months (the first real-data run since the whole
engine-parity refactor landed) produced only 6 recommendations, none for stocks_mid/small/indices.
Investigated via a spawned agent, independently re-verified.

- **Root cause:** `compute_atr_levels()` in `app/services/technicals.py` — the Volume-Profile take-profit
  snap (HVN/VAH for longs, HVN/VAL for shorts) had no R:R floor check, unlike the swing-S/R fallback
  right below it which already has one. When price sits just under its own Value Area High (an ordinary,
  common state), TP collapsed to within 1-5% of entry while SL stayed several ATR-widths away — R:R as
  low as 0.09, failing the category floor and silently dropping the candidate. Confirmed live for RXRX
  (0.09), ^IXIC/NDX (0.41-0.48), SMCI (~1.07). Pre-existing since May 27 — unrelated to the engine-parity
  refactor, just never manifested until many non-mega-cap names traded near their VAH simultaneously.
- **Fixed:** added the same R:R≥1.0 guard to all four VP-snap branches. New test
  `tests/unit/services/test_technicals_atr_levels_vp_guard.py` (TDD, git-stash verified fail-before/
  pass-after). Also added observability logging at 6 previously-silent skip sites in `engine.py`
  (`regime_hard_block`, `short_disabled`, `short_above_ema200`, `rsi_exhausted`,
  `sentiment_velocity_reversal`, `ofi_gate`) — pure logging, zero behavior change.
- **Separately investigated and cleared (not a bug):** today's still-low count even after the fix is
  dominated by `short_disabled` (91 candidates, nearly every global index plus many stocks_mid names
  called "short" and dropped by the legacy long-only `SHORTS_ENABLED=False`). Verified live: a genuine
  narrow-breadth, mega-cap-led bull market — the bull-regime profile deliberately weights technicals 4:1
  over sentiment (`direction=(0.20, 0.80)` in `regime_map.py`), and SPX's own MACD/ADX/OBV are
  short-term bearish even though the structural regime call is bullish. META/GOOGL (control) show
  coherently bullish technicals+sentiment and passed fine. Left as a product decision, not fixed.
- Today's batch (2026-09-18) was purged and regenerated via the existing
  `POST /api/actions/refresh-todays-batch` endpoint after the fix: 6→8 recommendations.

### Archived April planning docs (outcomes live in the code)

These `improvements_trade_builder`-era planning/design docs were deleted; their implemented outcomes are
in the codebase:

- **`IMPROVEMENT_PLAN.md`** — consolidated improvements roadmap (system state, April fixes, trader-view
  backlog, implementation order).
- **`LIVE_TRADING_PLAN.md`** — design + implementation plan for the Live Trading feature (data stream
  engine, LLM stream processing, selected-trade tracker, live-trading UI page, new DB tables and API
  routes). Implemented under `app/live_trading/`.
- **`LLM_IMPROVEMENT_PLAN.md`** — LLM validation layer, anti-hallucination feedback loop, near/far
  horizon-aware prompts, P1–P15 audit. Implemented under `app/services/research/` + the hallucination-
  profile scheduler job.
- **`FUTURE_RECOMMENDATION_IMPROVEMENTS.md`** — research-backed idea backlog for recommendation accuracy
  (never a work item; superseded by the engine-parity work above).
- **`superpowers/plans/2026-04-16-signal-quality-improvement.md`** + specs
  **`2026-04-16-signal-quality-improvement-design.md`**, **`2026-04-13-domain-sentiment-correction-design.md`**
  — signal-quality and domain-sentiment-correction designs; folded into the shipped scoring/sentiment code.

---

## 2026-04-13 — `realtime_stream_analize`

### Finnhub market data routing layer

**New: `app/services/market_data.py`**
- Central routing module: if `FINNHUB_KEY` is set in `.env`, eligible symbols (stocks/ETFs without `=`, `^`, `-`, `:`) are served by Finnhub. Futures, indices, FX, crypto and DXY stay on Yahoo Finance automatically.
- Functions: `fetch_ohlcv`, `fetch_quote`, `fetch_last_price`, `fetch_company_profile`, `fetch_metrics`, `fetch_next_earnings`, `search_symbol`.

**Modified: `app/core/config.py`** — added `finnhub_user`, `finnhub_key`

**Modified: `requirements.txt`** — `finnhub-python>=2.4.19` (Docker rebuild required)

**Refactored: 9 files** that imported `yfinance` directly now go through `market_data`:
`technicals.py`, `earnings_calendar.py`, `stream_engine.py`, `routes.py`, `live_routes.py`, `research/__init__.py`, `ticker_resolver.py`, `data_provider.py`, `briefing.py`

---

## 2026-04-07 — `improvements_trade_builder` (session 9)

### LLM — P16, P18 implemented and verified

#### P16 — Grade rubric corrected in `_BACKTEST_INSIGHT_SYSTEM`

**Modified: `app/services/research/llm.py`**
- `_BACKTEST_INSIGHT_SYSTEM` rewritten with a strict quantitative rubric (first-match wins):
  - **A**: win_rate ≥55% AND avg_pnl >0.3R AND sharpe >1.0 AND max_drawdown <4R
  - **B**: (win_rate ≥45% AND avg_pnl >0.2R AND max_drawdown <5R) OR (≥55% missing exactly one A criterion)
  - **C**: win_rate 35–44% OR (≥45% AND avg_pnl 0–0.2R) OR max_drawdown 5–8R
  - **D**: win_rate 25–34% OR negative avg_pnl > -0.2R
  - **F**: win_rate <25% OR avg_pnl ≤ -0.2R
  - Override: max_drawdown >8R forces a minimum of D regardless of win_rate
- `{horizon_days}` placeholder injected before each provider call: `_system = _BACKTEST_INSIGHT_SYSTEM.replace("{horizon_days}", str(horizon_days))`

#### P18 — Daily briefing LLM prompt (narrative morning brief)

**Modified: `app/services/research/llm.py`**
- `_BRIEF_NARRATIVE_SYSTEM`: senior trading desk analyst prompt — morning brief of 3–5 sentences; structure: (1) macro environment VIX+SPX → risk regime, (2) position implications + sizing, (3) the day's directional bias; specific numbers mandatory, no disclaimers
- `generate_brief_narrative(checklist_items, recs_count, recs_long, recs_short, active_trades_count, market_regime, dxy_trend, dxy_change_5d)`: user message built from checklist + regime + DXY; max_tokens=250; all 4 providers covered; returns `str | None`

**Modified: `app/services/briefing.py` — `run_briefing()`**
- Counts recs_today per direction (`recs_long`, `recs_short`)
- Counts `active_trades_count` (ActiveTrade with outcome IS NULL)
- Fetches market_regime and DXY via `asyncio.to_thread` (non-fatal)
- Calls `generate_brief_narrative()` non-fatally; stores it in `payload["narrative"]`
- `content_text` unchanged — backward compatible

**Modified: `app/static/briefing.html`**
- CSS: `.briefing-narrative` (blue border-left, muted background), `.briefing-narrative-label`
- `<div id="briefing-narrative" style="display:none;">` added above the metrics section
- `showBriefing()` updated: renders `data.payload.narrative` → `#briefing-narrative`; hides the div if narrative is missing

---

## 2026-04-07 — `improvements_trade_builder` (session 8-b)

### LLM — P5, P6, P13, P14, P15, P17 implemented

#### P5 + P6 + P17 — Context enrichment + Horizon-aware prompts + Extended sentiment window

**Modified: `app/services/research/__init__.py` — `build_research_context()`**
- `sentiment_trend`: read from `analysis.payload["by_asset_sentiment"][asset]["sentiment_trend"]`
- `headline_ages: list[float]`: age in hours per headline from `FeedItem.fetched_at`; headlines extended from 2 to 3
- `market_regime`: `detect_market_regime().regime` (`risk_on | risk_off | cautious`)
- `dxy_trend`, `dxy_change_5d`, `dxy_change_20d`: `detect_dxy_macro()`
- `upcoming_events`: `get_upcoming_events(days_ahead=expiry_days)` — FOMC/CPI/NFP within the trade window
- `pe_ratio`: `yfinance info["trailingPE"]` for stocks/ETFs
- `position_52w`: computed from `fiftyTwoWeekHigh/Low` + `buy_in_price`
- `sentiment_5d_avg`, `sentiment_10d_avg` (far horizon only): query FeedItem over the last 10 days; omitted for near horizon for efficiency

**Modified: `app/services/research/llm.py`**
- `SYSTEM_PROMPT` replaced with `SYSTEM_PROMPT_NEAR` + `SYSTEM_PROMPT_FAR` format-strings
  - Near: tactical — direction over `{horizon_days}` days, ≥2 indicators, macro events, SL/TP vs VA; long-term language forbidden
  - Far: structural — EMA/SMA trend + ADX, DXY/regime macro context, uncertainty over `{calendar_weeks}` weeks; intraday language forbidden
- `_format_user_content()` restructured with Common / Near / Far sections
- `generate_rationale()`: selects the prompt based on `context["horizon"]`; persists `_prompt_version` in logs
- `snapshot_keys` extended with: horizon, expiry_days, sentiment_trend, dxy_trend, dxy_change_5d/20d, headline_ages, sentiment_5d_avg/10d_avg

#### P13 — Hallucination profile builder (weekly job)

**New: `app/services/research/bias_detector.py`**
- `build_hallucination_profile(session, days_back=30)`: async aggregation per `(provider_id, model)` — v4a_rate, v4b_rate, v4c_rate, v4d_rate, any_v4_rate, retry_rate; atomic write `.tmp → rename` to `data/hallucination_profile.json`

**Modified: `app/jobs/scheduler.py`**
- `job_build_hallucination_profile()`: async wrapper; cron Sunday 06:00

#### P14 — `hallucination_guard` injected into prompts

**Modified: `app/services/research/bias_detector.py`**
- `build_hallucination_guard(provider_id, model)`: reads the profile; returns `""` if sample_size < 10 or no flag rate ≥ 25%; otherwise generates a warning block personalized per indicator

**Modified: `app/services/research/llm.py` — `generate_rationale()`**
- `build_hallucination_guard(provider_id, model)` called before building the system prompt
- The result replaces `{hallucination_guard}` in `SYSTEM_PROMPT_NEAR.format()` and `SYSTEM_PROMPT_FAR.format()`

#### P15 — Few-shot selection + injection

**New: `app/services/research/few_shot.py`**
- `get_verified_few_shot_examples(session, provider_id, model, horizon, limit=2)`: bootstrap guard (returns `""` if total logs with outcome < 10); 3-step selection: exact provider+model+horizon match → same horizon → any; filters: outcome IS NOT NULL, metrics_accuracy_score > 0.8, no V4a/V4b in flags; formats a `--- VERIFIED EXAMPLES ---` block

**Modified: `app/services/research/llm.py` — `generate_rationale()`**
- `get_verified_few_shot_examples()` called async (own session); block injected at the start of the user message if non-empty

---

## 2026-04-07 — `improvements_trade_builder` (session 8)

#### Code verification (session 8)

All changes from sessions 7–8 were verified in code before being marked ✅ Done in `LLM_IMPROVEMENT_PLAN.md`:

| Plan | Verified files | Status |
|---|---|---|
| P1 — model + migration | `app/models/llm_rationale_log.py`, `migrations/versions/003_llm_rationale_log.py`, `app/models/__init__.py` | ✅ |
| P2 — validator V1–V3, V5, V7 | `app/services/research/validator.py` (all rules present + V7 wired into gap) | ✅ |
| P3 — max_tokens + thinking budget + config hierarchy | `providers.py` (max_thinking_budget, clamp), `preferences.py` (clamp + thinking_budget_max), `llm.py` (max_tokens=400, parametrized output_buffer, no hardcoding) | ✅ |
| P4 — horizon classification | `technicals.py` (max_days=60, classify_horizon), `research/__init__.py`, `scheduler.py`, `routes.py` | ✅ |
| P7 — fire-and-forget rationale logging | `llm.py` (_log_rationale_async + asyncio.create_task in both branches) | ✅ |
| P8 — capture metrics at entry | `llm.py` (_capture_metrics_at_entry_async), `research/__init__.py`, `routes.py` (mark_took_it) | ✅ |
| P9 — verify outcomes + quality score | `scheduler.py` (_verify_outcomes_for_logs + _calculate_quality_score) | ✅ |
| P10 — validator V4a–V4d + V8 | `validator.py` (new rules), `research/__init__.py` (rsi/atr_pct/adx/macd_histogram fields) | ✅ |
| P11 — verify_news_references | `hallucination.py` (new function), `llm.py` (_verify_news_for_log in the same task after logging) | ✅ |
| P12 — verify_metrics_claims | `hallucination.py` (new function), `scheduler.py` (_verify_outcomes_for_logs, inline at outcome) | ✅ |

> ⚠️ **Rebuild required** to apply migration 003: `make build start`

#### P1 — LLMRationaleLog + Validator + Retry Loop (complete)

**New: `app/models/llm_rationale_log.py`**
- SQLAlchemy model `LLMRationaleLog` with 25+ columns: id, created_at, recommendation_id/active_trade_id (FK SET NULL), asset_id, asset_category, direction, horizon, horizon_days, provider_id, model, prompt_version, context_snapshot (JSON), rationale_text, validation_flags (JSON), retry_count
- Outcome-feedback columns: news_verified, metrics_at_entry, metrics_verification, metrics_accuracy_score, outcome, outcome_at, closing_price, pnl_r, quality_score, hallucination_rate
- 4 composite indexes: asset+direction+outcome, category+horizon+outcome, provider+model+outcome, created_at
- Helper methods: `get_validation_flags()`, `set_validation_flags()`, `get_context_snapshot()`, `get_metrics_at_entry()`

**New: `migrations/versions/003_llm_rationale_log.py`**
- Alembic migration revision="003" → creates the `llm_rationale_logs` table with all columns, FKs, server_defaults and 6 indexes

**New: `app/services/research/validator.py`**
- `ValidationFlag` + `ValidationResult` dataclasses
- V1: length check (< 50 chars) → blocking retry
- V2: truncation heuristic (text > 80 chars, not ending with `.!?"'`) → blocking retry
- V3: per-sentence direction alignment, with concession-marker detection ("despite", "although") → blocking retry
- V5: specificity score (< 2 technical terms from a set of ~25) → blocking retry
- V7: gap advice word count > 30 → blocking retry
- `validate_rationale(text, context, prompt_type)` — public API; `prompt_type="gap"` enables V7

**Modified: `app/services/research/llm.py`**
- `generate_rationale()` rebuilt with a retry loop (MAX_RETRIES=2, 3 attempts total)
- Every LLM response passes through `validate_rationale()` before being returned
- On validation failure: injects `_corrective_instruction` into the context and retries
- `_log_rationale_async()`: fire-and-forget (asyncio.create_task) that persists `LLMRationaleLog` in its own DB session; logs on pass or after all retries
- Cleaned-up context snapshot (excludes OHLCV bulk) — stores: asset, direction, sentiment, price, levels, technicals, VP fields

**Modified: `app/models/__init__.py`**
- Import + export `LLMRationaleLog`

#### P11 — `verify_news_references` (news cross-ref immediately after generation)

**New: `app/services/research/hallucination.py`** — new module

`verify_news_references(rationale_text, context_headlines, session, asset_id, hours_back=72)`:
- Extracts sentences with reporting verbs (`announced/reported/said/following/after X said`)
- Checks against context headlines (fuzzy threshold 0.35) and against `feed_items` in the DB (72h, threshold 0.40)
- Per-citation classification: `verified` / `found_in_db_only` (context leak) / `hallucinated` (pure invention)
- Returns: `verified` (bool), `citations_found`, `missing_from_context`, `found_in_db_only`, `pure_hallucinations`, `detail` (per-sentence)

**Modified: `app/services/research/llm.py`**
- `_log_rationale_async` now returns the internal `log_id` after `session.refresh(entry)`
- New function `_verify_news_for_log(log_id, rationale_text, headlines, asset_id)` — opens its own session, runs `verify_news_references`, persists `news_verified` + `news_verification_detail` (JSON)
- `_log_rationale_async` calls `_verify_news_for_log` in the same task, after commit — no impact on `generate_rationale` latency

#### P12 — `verify_metrics_claims` (post-outcome, inline in job_update_active_trades)

**New in `app/services/research/hallucination.py`**

`verify_metrics_claims(rationale_text, metrics_at_entry)`:
- Dedicated patterns: `_RSI_CLAIM`, `_ADX_CLAIM`, `_ATR_CLAIM`, `_MACD_BULL`/`_MACD_BEAR`
- Compares cited values against actuals from `metrics_at_entry` (±15% tolerance)
- MACD: qualitative direction check (bullish/bearish vs positive/negative histogram)
- Returns: `claims` (per indicator: cited, actual, delta_pct, is_hallucination), `accuracy_score` (0-1), `hallucinated_indicators`
- No-op if `metrics_at_entry` is empty or there are no detectable claims

**Modified: `app/jobs/scheduler.py` — `_verify_outcomes_for_logs`**
- Runs `verify_metrics_claims` inline at outcome (if `metrics_at_entry` is populated and `metrics_accuracy_score` is NULL)
- Persists `metrics_accuracy_score` and `metrics_verification` (detailed JSON)
- `quality_score` computed with the real `metrics_accuracy_score` (no longer None as in P9)

**Post-verification fix: `app/services/research/llm.py` — `_capture_metrics_at_entry_async`**
- Bug: `atr_pct` was not stored in `metrics_at_entry` (only the absolute `atr` was stored)
- Fix: added `metrics["atr_pct"] = round(snap.atr_pct * 100, 2)` — P12 looks for `atr_pct` (as %) in `metrics_at_entry`; without the fix, ATR claims were always skipped

#### P10 — Validator V4a–V4d + V8

**Modified: `app/services/research/validator.py`** — fully rewritten

- `BLOCKING_RULES` extended: `{"V1","V2","V3","V4a","V4b","V5","V7","V8"}`
- `FLAG_ONLY_RULES = {"V4c","V4d"}` (do not block retry, only logged)
- Helper `_within_tolerance(actual, cited, tol=0.15)` — relative ±15% comparison
- Helper `_fuzzy_match(text, candidates, threshold=0.35)` — `difflib.SequenceMatcher`, no extra dependencies

**V4a — price hallucination** (blocking)
- Regex `_PRICE_CONTEXT`: extracts numbers from a price context (support/resistance/target/entry/$)
- Ignores values < 5% of the lowest known price (avoids false positives on RSI/ADX/%)
- Fail: a cited number > 15% off from ALL context prices (buy_in, SL, TP, VPOC, VAH, VAL)

**V4b — indicator value hallucination** (blocking)
- Separate regexes: `_RSI_PATTERN`, `_ADX_PATTERN`, `_ATR_PATTERN`
- Only checks if the context contains the actual value (`rsi`, `adx`, `atr_pct`)
- Fail: cited value > 15% off from actual; the retry message explicitly includes the correct value

**V4c — news hallucination** (flag-only)
- Regex `_NEWS_REPORTING`: sentences with reporting verbs (announced/reported/said/following)
- Fuzzy match against `context["headlines"]` (threshold 0.35 — lax, flag-only)
- No-op if there are no headlines in the context

**V4d — temporal hallucination** (flag-only)
- Regex `_TEMPORAL_RECENT`: yesterday/last week/this morning/just announced/hours ago
- Cross-ref with `context["headline_ages"]` (introduced in the current session)
- Threshold: "yesterday/this morning" → min headline < 48h; "last week" → < 168h

**V8 — horizon consistency** (blocking)
- Near (≤14d): blocks `long-term / over the months / structural / fundamental`
- Far (>14d): blocks `this week / by Friday / intraday / today's session`
- Retry message includes `horizon_days` from the context

**Modified: `app/services/research/__init__.py`**
- Added to the context from the snapshot: `rsi`, `atr_pct` (as %, e.g. 2.3), `adx`, `macd_histogram` — used by V4b

#### P8 — `capture_metrics_at_entry` (fire-and-forget on took-it)

**New: `_capture_metrics_at_entry_async()` in `app/services/research/llm.py`**
- Captures a live `TechnicalSnapshot` via `compute_sl_tp_for_symbol` (async via `asyncio.to_thread`)
- Saves to `metrics_at_entry` (JSON): price, RSI, MACD histogram, ATR, ADX, VPOC, VAH, VAL, vp_bias, sentiment_avg
- Sets `active_trade_id` on all `LLMRationaleLog` with matching `recommendation_id` and `metrics_at_entry IS NULL`

**Modified: `app/services/research/__init__.py`**
- Exposes the `capture_metrics_at_entry()` wrapper in `__all__`

**Modified: `app/api/routes.py` — `mark_took_it`**
- Retains `first_active_id` after `db.flush()`
- `asyncio.create_task(capture_metrics_at_entry(...))` after `db.commit()` — does not block the response

#### P9 — `_verify_outcomes_for_logs` (periodic job at the end of update_trades)

**New: `_calculate_quality_score()` in `app/jobs/scheduler.py`**
- Formula from the plan: baseline 5 ±outcome (±3) ±metrics_accuracy ±news_verified ±flags −retry_count +pnl_r bonus
- Degrades gracefully when `metrics_accuracy_score` and `news_verified` are `None` (P10/P11 fill them in later)

**New: `_verify_outcomes_for_logs(session)` in `app/jobs/scheduler.py`**
- JOIN `active_trades` → `recommendations` → filter `llm_rationale_logs` where `outcome IS NULL`
- Copies: `outcome`, `outcome_at`, `closing_price`
- Computes `pnl_r`: `(closing_price − entry) / risk` (long/short adjusted)
- Computes and sets `quality_score`
- Updates all logs with matching `recommendation_id` and `outcome IS NULL`

**Modified: `app/jobs/scheduler.py` — `job_update_active_trades`**
- Calls `await _verify_outcomes_for_logs(session)` + `await session.commit()` after the main commit

#### P4 — Horizon classification + estimate_expiry_days max→60

**Modified: `app/services/technicals.py`**
- `estimate_expiry_days()`: `max_days` parameter changed from 30 → **60** — now supports the far horizon (>14 days)
- New function: `classify_horizon(expiry_days: int) -> str` — returns `"near"` (≤14) or `"far"` (>14)

**Modified: `app/services/research/__init__.py`**
- `build_research_context()`: if the recommendation has `expiry_date`, computes `expiry_days` and `horizon` and adds them to ctx
- If SL/TP are computed fresh from the snapshot, calls `estimate_expiry_days()` + `classify_horizon()` for the same effect

**Modified: `app/jobs/scheduler.py`**
- Batch loop: adds `expiry_days` + `horizon` from `c.expiry_date` to ctx (if not already present from `build_research_context`)

**Modified: `app/api/routes.py`**
- `trade_builder_from_asset`: adds `expiry_days` + `horizon` from `cand.expiry_date` to ctx

---

## 2026-04-07 — `improvements_trade_builder` (session 7)

### LLM — Full analysis + thinking-budget fixes + improvement plan

#### Analysis and planning

**New: `docs/LLM_IMPROVEMENT_PLAN.md`** (rev 2)
- Full analysis of the 5 existing prompts from a senior developer + professional trader perspective
- Gaps identified: prediction horizon absent, sentiment without trend/context, confidence score not sent to the LLM, empty macro context, Volume Profile without execution context, poorly calibrated backtest grade rubric, no news streaming, LLM-less daily briefing
- Logic errors: `max_tokens=200` truncates, `THINKING_BUDGET` hardcoded, zero rationale validation, context dict without schema, numeric and news hallucinations undetected, no prompt versioning
- **Near/far horizon distinction**: near ≤ 14 trading days (technicals + news + imminent events), far > 14 days (structural trend + fundamentals + macro cycle)
- **Validator plan V1–V8**: length, truncation, direction alignment, price/indicator/news/temporal hallucinations, specificity score, grade consistency, horizon consistency
- **Anti-hallucination feedback-loop plan**: `llm_rationale_log` table, news cross-reference vs `feed_items`, metrics snapshot at entry, metrics verification at outcome, hallucination profile per model (weekly), hallucination guard injected into future prompts, few-shot with verified examples
- 9 objections documented with mitigations: few-shot API cost, V4b/V4c false positives, bootstrap problem, NEAR/FAR prompt divergence, better model vs feedback loop, far horizon without full fundamentals, metrics-capture delay, longer prompts, non-existent extended sentiment window

#### Implemented fixes

**Modified: `app/services/research/llm.py`**
- `THINKING_BUDGET = 2000` hardcoded in `generate_gap_advice` → removed; now reads `thinking_budget = int(cfg.get("thinking_budget") or 0)` from config (same as all other functions)
- `_call_anthropic()`: added parameter `output_buffer: int = 1024` — explicitly separates the thinking budget from the tokens reserved for the response; formula: `max_tokens = thinking_budget + output_buffer`
- `max_tokens` rationale: 200 → **400** for OpenAI, Anthropic (output_buffer=400), OpenRouter; prevents frequent truncation on verbose models
- `generate_backtest_insight`: output buffer raised to **1500** (complex JSON can exceed 1024 tokens)
- `generate_gap_advice`: output buffer **150** tokens (25 words + margin), non-thinking `max_tokens`: 80 → 100

**Modified: `app/services/research/providers.py`**
- `ProviderDef` dataclass: new field `max_thinking_budget: int = 0`
- Anthropic: `max_thinking_budget=32000` (claude-opus-4-6/sonnet-4-6 support extended thinking up to 32K tokens)
- OpenAI / Ollama / OpenRouter: `max_thinking_budget=0` (no thinking tokens)
- New function: `get_max_thinking_budget(provider_id)` — returns the provider's ceiling
- New function: `clamp_thinking_budget(provider_id, requested)` — automatically clamps a configured budget to the provider's maximum; returns 0 if the provider does not support thinking

**Modified: `app/core/preferences.py`**
- `get_research_llm_config()`: applies `clamp_thinking_budget()` after reading the value — budgets above the provider's maximum are silently clamped
- `get_research_llm_config()`: returns a new field `thinking_budget_max` — the current provider's ceiling; used for UI validation (slider max)
- `set_research_llm_config()`: new parameter `thinking_budget: int | None` — allows saving the budget in `preferences.json`; applies `clamp_thinking_budget()` before persisting

#### Priority hierarchy for `thinking_budget` (highest to lowest)

```
preferences.json["research_llm_thinking_budget"]   ← PATCH /preferences or manual edit
    ↓ fallback if absent
.env RESEARCH_LLM_THINKING_BUDGET=16000             ← override on container restart
    ↓ fallback if .env does not set it
config.py default: research_llm_thinking_budget = 0 ← thinking disabled
```

The configured budget is automatically clamped to the provider's `max_thinking_budget` (32000 for Anthropic, 0 for the rest).

---

## 2026-04-07 — `improvements_trade_builder` (session 6)

### Impact analysis PRIORITY 7 and 8 — decision pending

**Modified: `docs/IMPROVEMENT_PLAN.md`**
- PRIORITY 7 (News latency < 15 minutes): added per-area impact analysis — the real value is in on-demand research, not in the batch; zero impact on backtesting; mandatory recurring cost (~$29/month); marked `⚠️ Decision pending`
- PRIORITY 8 (Dynamic correlation): added per-area impact analysis — the real value is only in the batch filter (fewer correlated signals under stress); zero impact on research and current backtesting; marginal improvement over VP and RS already implemented; marked `⚠️ Decision pending`
- Both remain undecided until backtesting validates that VP (PRIORITY 5) and RS (PRIORITY 6) add real value

---

## 2026-04-07 — `improvements_trade_builder` (session 5)

### Relative Strength vs Benchmark (PRIORITY 6 — complete)

**New: `compute_relative_strength()` in `app/services/technicals.py`**
- Computes the RS ratio `(1 + ret_asset) / (1 + ret_benchmark)` over N trading days
- >1.0 = asset outperforms the benchmark; <1.0 = underperforms
- Reuses `fetch_ohlcv()` with the existing cache — zero extra API calls
- Automatically walk-forward: respects `_backtest_cutoff` via `_apply_backtest_cutoff()`
- Output clamped to `[0.1, 10.0]` for sparse/bad data; returns `None` if there are insufficient bars

**New: `SECTOR_ETF_DIRECT` + `get_sector_etf_for_asset()` in `app/services/market_context.py`**
- Direct dict asset_id → sector ETF Yahoo symbol (30+ mappings: AAPL→XLK, XOM→XLE, JPM→XLF, JNJ→XLV etc.)
- Fallback via `get_sector_for_asset()` → `SECTOR_ETFS` for assets not mapped directly

**Modified: `app/services/recommendations/engine.py`**
- `Candidate` dataclass: new fields `rs_20d`, `rs_60d`, `sector_rs`
- New import: `compute_relative_strength`, `get_sector_etf_for_asset`
- `_build_candidate()`: computes RS only for `category == "stocks"`; direction-aware confidence:
  - RS > 1.2 + long → +5; RS > 1.2 + short → −5
  - RS < 0.8 + long → −8; RS < 0.8 + short → +5
- `sector_aligned` extended: revoked if `sector_rs < 0.9` (stock weak vs its sector), granted if `sector_rs > 1.15` (stock strong even if the sector is neutral)
- `Candidate(...)` constructor: populated with `rs_20d`, `rs_60d`, `sector_rs`

**Modified: `app/jobs/scheduler.py`**
- New module-level dict `_rs_by_symbol: dict[str, dict]`
- `job_daily_batch()`: populates `_rs_by_symbol[sym]` from each candidate's RS fields

**Modified: `app/api/routes.py`**
- `_rec_to_item()`: imports `_rs_by_symbol`, exposes `rs_20d`, `rs_60d`, `sector_rs` in the per-recommendation JSON

**Modified: `app/services/backtesting/replay.py`**
- New import: `compute_relative_strength`
- `run_backtest()`: guaranteed pre-fetch for `^GSPC` and sector ETFs (XLK/XLE/XLF/XLV/XLY/XLI/XLB)
- `_build_signal()`: computes `rs_20d` walk-forward for stocks; applies the same confidence adjustments as the live engine; adds `rs_20d` to the signal dict

**Modified: `app/services/backtesting/metrics.py`**
- `compute_metrics()` returns `relative_strength`: strong (>1.2) / neutral (0.8–1.2) / weak (<0.8) / no_data RS stats + `pct_strong`, `pct_weak`

**Modified: `app/static/recommendations.html`**
- New CSS: `.badge--rs-strong`, `.badge--rs-weak`, `#rec-rs-tooltip` (fixed global)
- Global tooltip IIFE with 3s delay, `initRsBadgeTooltips()` called on `renderComplete`
- `rsBadge(rec)`: badge `RS+` (green) if `rs_20d > 1.2`, `RS−` (red) if `rs_20d < 0.8`
- Tooltip: explains how far it deviated from SPX, whether it confirms or contradicts the direction, and the impact on confidence
- Asset column formatter: RS badge added after the VP badge

**Modified: `app/services/research/__init__.py`**
- `build_research_context()`: computes `rs_20d`, `rs_60d`, `sector_rs` for stocks after `compute_sl_tp_for_symbol`; adds them to `ctx`

**Modified: `app/services/research/llm.py`**
- `_format_user_content()`: includes an RS line in the LLM prompt — 20d/60d ratio vs SPX, ratio vs sector ETF, outperforming/underperforming label

**Modified: `app/static/research.html`**
- New CSS: `.rs-rs-strip`, `.rs-rs-badge-strong/weak/neut`
- `renderRsStrip(ctx)`: RS strip in the "Market Context" panel with a STRONG/WEAK/NEUTRAL badge, 20d/60d/sector values, 3s tooltip (reuses `rs-vp-tooltip-anchor` + `initRsTooltips()`)
- `renderContext()`: inserts the RS strip before the VP strip

---

## 2026-04-07 — `improvements_trade_builder` (session 4)

### Volume Profile — VPOC, VAH, VAL, HVN/LVN (PRIORITY 5 — complete)

**New: `compute_volume_profile()` in `app/services/technicals.py`**
- Distributes daily volume uniformly across the High–Low range into 120 bins (60-day lookback)
- VPOC = the bin with the maximum volume (the market's equilibrium price)
- Value Area (VAH/VAL): expansion from VPOC until 70% of total volume
- HVN: bins > 65th percentile (volume clusters — price magnet); LVN: bins < 25th percentile (gaps — fast moves)
- `_cluster()`: merges levels less than 0.5% apart

**Modified: `TechnicalSnapshot` in `app/services/technicals.py`**
- New fields: `vpoc`, `vah`, `val`, `hvn_levels`, `lvn_levels`, `vp_bias`, `vp_overextension`
- New methods: `in_value_area()`, `nearest_hvn_below()`, `nearest_hvn_above()`, `nearest_lvn_below()`, `nearest_lvn_above()`
- `build_snapshot()` calls `compute_volume_profile(df)` and populates all VP fields

**Modified: `compute_atr_levels()` in `app/services/technicals.py`**
- VP prioritized over swing S/R for SL and TP placement
- Long SL: `nearest_hvn_below()` → `snap.val` → 0.2% below the level; fallback to swing S/R
- Long TP: capped at the HVN above price or at VAH if it is in the way (`vah * 0.998`)
- Short: symmetric logic with HVN/VAL

**Modified: `app/services/recommendations/engine.py`**
- `Candidate` dataclass: new fields `vpoc`, `vah`, `val`, `vp_bias`, `vp_overextension`, `in_value_area`
- `_build_candidate()`: VP confidence adjustments — `in_value_area` −8, near LVN +5, overextension >2× −10, >1.3× −5, VP confirms the direction +4
- Return: VP fields populated from `snap`

**Modified: `app/services/backtesting/replay.py`**
- `_build_signal()`: the same VP confidence adjustments as the live engine (automatically walk-forward correct)
- Signal dict includes: `vpoc`, `vah`, `val`, `vp_bias`, `vp_flag`, `in_value_area`

**Modified: `app/services/backtesting/metrics.py`**
- `compute_metrics()` returns `volume_profile`: `in_value_area` / `outside_value_area` stats (total, win_rate, avg_pnl_r) + `pct_signals_in_va`

**Modified: `app/jobs/scheduler.py`**
- New module-level dict `_vp_by_symbol: dict[str, dict]`
- `job_update_active_trades()` — populates `_vp_by_symbol[symbol]` from the already-fetched `snap`
- `job_daily_batch()` — populates `_vp_by_symbol` from each candidate's VP fields

**Modified: `app/api/routes.py`**
- `/api/active-trades` — new per-trade fields: `vpoc`, `vah`, `val`, `vp_bias`, `vp_overextension`, `in_value_area`, `vp_distance_pct`
- `/api/recommendations` — new per-recommendation fields: `vpoc`, `vah`, `val`, `vp_bias`, `vp_overextension`, `in_value_area`

**Modified: `app/static/active-trades.html`**
- `vpStrip(t)`: VP bar with VPOC, bias (↑/↓ fair value), VA range, distance % vs VPOC, IN VA badge, overextension indicator
- Fixed global tooltip (3s hover delay): explains VPOC, Value Area, fair value, dist% to a newbie
- CSS: `.vp-strip`, `.vp-badge-va`, `.vp-dist-pos/.neg`

**Modified: `app/static/recommendations.html`**
- `vpBadge(rec)`: badge `VA ZONE` (gray) if `in_value_area`; badge `VP EXT N×` (purple) if overextension > 1.3×
- Asset column: VP badge shown next to the gap badge
- Size column: VPOC price + bias as subtext under the sizing

**Modified: `app/services/research/__init__.py`**
- `build_research_context()`: extracts `vpoc`, `vah`, `val`, `vp_bias`, `vp_overextension`, `in_value_area` from the `snap` returned by `compute_sl_tp_for_symbol`

**Modified: `app/services/research/llm.py`**
- `_format_user_content()`: includes a VP line in the LLM prompt — VPOC, bias, value area range, in_value_area, overextension
- The model references VP in the generated rationale

**Modified: `app/static/research.html`**
- `renderVpStrip(ctx)`: VP bar in the "Market Context" panel (same information as active-trades)
- Fixed global tooltip (3s hover delay, independent of the gap tooltip)
- `renderContext()`: inserts the VP strip + calls `initRsTooltips()` after render
- CSS: `.rs-vp-strip`, `.rs-vp-badge-va`, `#rs-tooltip-global`

---

## 2026-04-07 — `improvements_trade_builder`

### Earnings Calendar (PRIORITY 2)

**New: `app/services/earnings_calendar.py`**
- Fetches earnings dates per ticker via `yfinance.Ticker.calendar`
- In-memory cache with a 24h TTL — does not hit Yahoo on every batch
- `get_next_earnings(symbol)` → `Optional[date]`
- `is_near_earnings(symbol, window_days)` → `(bool, Optional[int])`

**Modified: `app/services/recommendations/engine.py`**
- `Candidate` dataclass: new fields `near_earnings: bool`, `days_to_earnings: Optional[int]`
- `_build_candidate()`: `-15` confidence penalty for stocks within a ±2-day earnings window
- If `earnings_skip_enabled=True`: the candidate is removed entirely from the batch (skip)
- Note in `market_regime_note`: `"earnings ±Nd"`

**Modified: `app/core/preferences.py`**
- `get_earnings_calendar_config()` / `set_earnings_calendar_config()`
- New keys: `earnings_window_days` (default 2), `earnings_confidence_penalty` (default 15), `earnings_skip_enabled` (default False)

**Modified: `app/api/routes.py`**
- `/api/recommendations` — new fields `near_earnings`, `days_to_earnings` per recommendation
- `/api/active-trades` — new fields `near_earnings`, `days_to_earnings` per active trade (stocks only, using the existing cache)

**Modified: `app/static/recommendations.html`**
- Gold `EARNINGS ±Nd` badge on the stock's symbol in the recommendations table

**Modified: `app/static/active-trades.html`**
- Pulsing red `EARNINGS +Nd` badge in the trade card header
- Warning strip under the header with an explicit gap-risk message when `near_earnings=true`
- CSS: `.badge--earnings-warn` (pulse animation), `.earnings-strip`

---

### Circuit Breaker (PRIORITY 3)

**New: `app/services/circuit_breaker.py`**
- `CircuitBreakerState` dataclass: `mode`, `rolling_loss_r`, `consecutive_losses`, `drawdown_r`, `triggered_by`
- `compute_state(db_trades, ...)` — evaluates 3 independent gates:
  1. **rolling_loss_gate** — R-multiples lost in the last 24h (not a calendar day)
  2. **consecutive_gate** — streak of `hit_sl`; `expired`/`manual` are neutral; only `hit_tp` resets
  3. **drawdown_gate** — peak-to-trough on the cumulative R curve (entire history)
- Modes: `normal` / `defensive` (conf -10, sizing ×0.7) / `paused` (empty batch)

**Modified: `app/services/recommendations/engine.py`**
- `build_daily_candidates()` — new parameter `closed_trades: Optional[list]`
- If the CB is `paused` → returns `[]` immediately
- If the CB is `defensive` → applies the confidence penalty and sizing after the batch is built

**Modified: `app/jobs/scheduler.py`**
- `job_run_batch()` — fetches closed trades and passes them to `build_daily_candidates(closed_trades=...)`
- `job_update_active_trades()` — evaluates the CB on every run (2h); logs and notifies if mode != normal

**Modified: `app/core/preferences.py`**
- `get_circuit_breaker_config()` / `set_circuit_breaker_config()`
- New keys: `circuit_breaker_enabled` (default True), `max_consecutive_losses` (default 5), `max_daily_loss_r` (default 3.0), `drawdown_gate_r` (default 8.0)

**Modified: `app/api/routes.py`**
- `GET /api/circuit-breaker` — new endpoint: current mode, stats, config, triggered_by

**Modified: `app/static/recommendations.html`**
- Banner `⛔ PAUSED` (red) or `⚠️ DEFENSIVE` (yellow) above the table when the CB is active
- CSS: `.alert--cb-defensive`, `.alert--cb-paused`

---

### Circuit breaker fixes (session 2 — 2026-04-07)

**Bug fix: `app/services/circuit_breaker.py`**
- `expired` and `manual` no longer break the consecutive-loss streak — they are treated as neutral
- Before: any non-`hit_sl` outcome reset the streak → an expired trade masked a losing streak
- Now: only `hit_tp` (a confirmed win) resets the streak

**Bug fix: rolling 24h window instead of calendar day**
- Before: `oa_date == today` → losses from 23:55 disappeared at midnight
- Now: `outcome_at >= now - 24h` → correct sliding window

**New: CB evaluated on every active-trades update (2h)**
- Before: the CB was evaluated only when the daily batch was generated
- Now: `job_update_active_trades()` computes the CB state and logs/notifies if it is active

**New: earnings warning on active trades**
- `/api/active-trades` returns `near_earnings` and `days_to_earnings` for each trade
- UI: pulsing badge + warning strip with an explicit gap-risk message

---

### Gap Risk — Active Trades + Backtesting (PRIORITY 4 — session 3 — 2026-04-07)

**Bug fix: `app/services/backtesting/simulator.py` — realistic gap slippage**
- Before: on `sl_hit` without `tp_hit`, `closing = min(bar_low, stop_loss)` → used bar_low (worst case, too pessimistic)
- Now: if `bar_open <= stop_loss` (gap down at the open) → `closing = bar_open` (realistic fill at open)
- If `bar_open > stop_loss` (stop hit intraday) → `closing = stop_loss` (normal fill)
- Same fix for short: `bar_open >= stop_loss` → `closing = bar_open`
- Result: more realistic P&L in R-multiples; no longer penalizes with the worst intraday price when the gap is at the open

**Modified: `app/jobs/scheduler.py`**
- New module-level dict: `_gap_risk_by_symbol: dict[str, dict]`
- `job_update_active_trades()` — computes gap risk per symbol (using the already-fetched `snap`, zero extra API calls)
- The result is stored in `_gap_risk_by_symbol[symbol] = {"level": ..., "note": ...}`
- Populated once per symbol per run (in-memory cache, refreshed on every 2h run)

**Modified: `app/api/routes.py`**
- `/api/active-trades` — new fields: `gap_risk_level` ("low"/"medium"/"high") and `gap_risk_note` per trade
- Imports `_gap_risk_by_symbol` from the scheduler; read from cache (no blocking calls in the async route)
- `get_yf_symbol()` now called once per trade (removed the duplication in the earnings block)

**Modified: `app/static/active-trades.html`**
- Badge `GAP MEDIUM` (amber) / `GAP HIGH` (red) in the badge row of the card header
- Warning strip `gap-strip` (yellow) under the earnings strip, with a specific message from the engine
- CSS: `.badge--gap-high`, `.badge--gap-medium`, `.gap-strip`
- JS: `gapBadge(level)`, `gapStrip(level, note)`

---

## 2026-04-06 — `improvements_trade_builder`

### Backtesting — Quick Backtest (PRIORITY 1)

**Modified: `app/services/backtesting/replay.py`**
- `run_backtest()` — new parameter `max_outcome_bars: int`
- `_simulate_signal_outcome()` — accepts `max_bars` instead of the global constant

**Modified: `app/api/backtest_routes.py`**
- `/api/backtest/quick` — fully redesigned:
  - Fixed window: the last 90 days of signals, outcomes over 14 bars (no longer 1 year)
  - Adds `_compute_current_signal()` — a live engine snapshot for the current asset
  - Calls `generate_backtest_insight()` with the current context + 14-day horizon
  - Returns: `window_days=90`, `outcome_bars=14`, `current_signal`, `llm_analysis`

**Modified: `app/services/research/llm.py`**
- `generate_backtest_insight()` — returns a structured `dict` (not free text):
  - Fields: `grade` (A–F), `grade_reason`, `key_insights[]` (3 bullets), `recommendation`
- `_BACKTEST_INSIGHT_SYSTEM` — new prompt with a grading rubric and an explicit 14-day horizon
- `_parse_backtest_insight_json()` — parser tolerant of markdown fences
- Anthropic fix: `max_tokens = thinking_budget + 1024` when extended thinking is active

**Modified: `app/static/active-trades.html`**
- Backtest modal fully redesigned:
  - "ENGINE SIGNAL — TODAY" card: Confidence, Price, SL, TP in a 4-column grid
  - Colored grade badge (A=green, B=teal, C=amber, D=orange, F=red) with `grade_reason`
  - 4-metric grid: Win Rate, Avg Profit/Loss, Sharpe Ratio, Max Drawdown
  - Stacked W/L/Expired bar with legend
  - AI Key Insights as a bullet list with chevron icons
  - Confidence buckets with colored bars
  - Recommendation box with an amber border
- Overflow fix: `max-height: 90vh` + flexbox on the modal + `overflow-y: auto` on the body
- SL/TP bug fix: `rangeBar()` and the live-price cell use `origTP` instead of `sugTP || origTP`

**Modified: `app/jobs/scheduler.py`**
- `trade.suggested_tp = trade.current_tp` — the per-leg TP is no longer overwritten by the recomputed ATR
- Same fix in `ActiveTradeLevel` logging

**Modified: `docs/IMPROVEMENT_PLAN.md`**
- PRIORITY 1 marked ✅ DONE 2026-04-06
- PRIORITY 2 marked ✅ DONE 2026-04-07
- PRIORITY 3 marked ✅ DONE 2026-04-07

---

## 2026-04-06 — `improvements_trade_builder` (session 1)

### Infrastructure

**New: TimescaleDB**
- `docker-compose.yml` — separate `trading-timescaledb` service (PostgreSQL 16 + TimescaleDB)
- `requirements.txt` — added `asyncpg`, `psycopg2-binary`, `alembic`
- `app/core/database.py` — dual-engine support SQLite (dev) / PostgreSQL (prod)

**New: Alembic migrations**
- `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`
- `migrations/versions/001_initial_schema.py` — all tables + TimescaleDB hypertables
- `scripts/migrate_sqlite_to_pg.py` — idempotent SQLite → PostgreSQL transfer

**Modified: `entrypoint.sh`**
- Runs Alembic + data migration before uvicorn

### Bug fixes

| Bug | File | Fix |
|-----|--------|-----|
| Platt calibration returned `(0.0, 0.0)` at cold start → `sigmoid(0)=0.5` for everything | `calibration.py` | Returns `None`; caller checks |
| `routes.py` called `set_platt_params(0, 0)` unconditionally | `routes.py` | Guard on `None` |
| S/R snapping long: `pass` and `sl = sr_sl` swapped — correct SL ignored | `technicals.py:632` | Logic corrected |
| `yfinance ticker.history()` without a timeout — blocked the schedulers indefinitely | `technicals.py:52` | `ThreadPoolExecutor` with a 30s timeout |
