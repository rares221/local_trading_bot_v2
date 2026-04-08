# Local Trading Bot v2 — Agent Reference

> **Purpose**: Complete technical reference for AI agents working on this codebase.
> Covers every module, function, API endpoint, database table, scheduler job, and data workflow.
> **App domain**: Market analysis engine generating 14-day trading recommendations.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Tech Stack & Runtime](#2-tech-stack--runtime)
3. [Environment Variables](#3-environment-variables)
4. [Database Schema](#4-database-schema)
5. [API Endpoints](#5-api-endpoints)
6. [Scheduler Jobs](#6-scheduler-jobs)
7. [Core Services — Functions](#7-core-services--functions)
8. [Data Models (ORM)](#8-data-models-orm)
9. [Static UI Pages](#9-static-ui-pages)
10. [Full Data Workflows](#10-full-data-workflows)
11. [Configuration & Preferences](#11-configuration--preferences)
12. [Build & Ops](#12-build--ops)
13. [Tests](#13-tests)
14. [Dependency Map](#14-dependency-map)

---

## 1. Project Overview

**Local Trading Bot v2** is a Python/FastAPI application that runs inside Docker. It performs:

- RSS news ingestion + multi-model sentiment analysis (VADER, FinBERT, AFINN)
- Asset tagging via NER, keyword dictionaries, and dynamic ticker detection
- Multi-timeframe technical analysis (RSI, MACD, ATR, Volume Profile, S/R, Fibonacci, VWAP)
- Market regime detection (SPX/VIX, DXY, sector regimes, cross-asset correlations)
- Daily recommendation batches across 4 asset categories: stocks, commodities, other (FX/Crypto), indices
- LLM-generated trade rationales (OpenAI, Anthropic, Ollama, OpenRouter)
- Professional trade lifecycle management (open → partial_tp → trailing → closed)
- Circuit breaker, confidence calibration (Platt scaling), A/B shadow mode
- Historical backtesting with Sharpe, win rate, and per-bucket metrics
- Webhook notifications for batch-ready, regime-change, trade-alert events

**All predictions target a 14-day horizon.**

---

## 2. Tech Stack & Runtime

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11 |
| Web framework | FastAPI (async) |
| ORM | SQLAlchemy (async, 2.x) |
| Database (prod) | TimescaleDB (PostgreSQL 16 + extension) |
| Database (dev) | SQLite via aiosqlite |
| Migrations | Alembic |
| Scheduler | APScheduler (asyncio backend) |
| Data fetch | yfinance, feedparser, requests |
| ML / NLP | scikit-learn, transformers (FinBERT), NLTK (VADER), AFINN |
| LLM clients | anthropic SDK, openai SDK, requests (Ollama, OpenRouter) |
| Container | Docker + docker-compose |
| Exposed port | 6001 (host) → 8000 (container) |

---

## 3. Environment Variables

All variables are loaded from `.env` at project root.

### Required

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key (if using Anthropic provider) |
| `OPENAI_API_KEY` | OpenAI key (if using OpenAI provider) |
| `OPENROUTER_API_KEY` | OpenRouter key (optional) |
| `RESEARCH_LLM_PROVIDER` | `anthropic` \| `openai` \| `ollama` \| `openrouter` |
| `RESEARCH_LLM_MODEL` | Model name (e.g. `claude-opus-4-6`, `gpt-4o`) |
| `RESEARCH_LLM_THINKING_BUDGET` | Extended thinking tokens (0 = disabled, max 16000) |

### Auto-configured (defaults in `app/core/config.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/trading.db` | Async DB URL |
| `MIGRATION_ENABLED` | `false` | Run SQLite→PG migration on startup |
| `SQLITE_SOURCE_URL` | `sqlite:///./data/trading.db` | Sync URL for migration source |

### Config defaults (no env var needed)

| Setting | Default | Description |
|---------|---------|-------------|
| `feed_max_age_days` | 30 | Days before feed items are purged |
| `daily_batch_hour` | 6 | UTC hour for daily recommendation batch |
| `update_active_trades_interval_hours` | 2 | How often active trades are updated |
| `analysis_run_interval_hours` | 2 | How often analysis runs between ingestions |
| `preferred_risk` | `moderate` | `conservative` \| `moderate` \| `aggressive` |
| `preferred_exchanges` | `NMS,NYQ,NGM,...` | Yahoo Finance exchange codes for ticker resolution |
| `ticker_cache_max_age_days` | 90 | TTL for resolved ticker cache |

---

## 4. Database Schema

All tables are declared in `app/models/`. TimescaleDB hypertables are created in `app/core/database.py:init_db()`.

### 4.1 `feed_items` — TimescaleDB hypertable (partitioned by `fetched_at`)

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(512) PK | Unique article ID |
| `title` | Text | Article title |
| `description` | Text | Article excerpt |
| `content` | Text | Full content (if available) |
| `link` | String(2048) | Source URL |
| `published` | DateTime(tz) | Original publish timestamp |
| `fetched_at` | DateTime(tz) | When ingested; partition key |
| `category` | String(64) | RSS feed category |
| `source` | String(256) | Feed name (e.g. "Reuters") |
| `keywords` | JSON | Extracted keywords list |
| `is_processed` | Boolean | True after sentiment + tagging |
| `sentiment_score` | Float | Composite score in \[-1, 1\] |
| `asset_tags` | JSON | List of asset names (e.g. `["oil", "AAPL"]`) |

**Indexes**: `category`, `is_processed`, `fetched_at`

---

### 4.2 `recommendations` — TimescaleDB hypertable (partitioned by `created_at`)

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(36) PK | UUID |
| `batch_id` | String(32) | Groups all recs in one daily run |
| `created_at` | DateTime(tz) | Partition key |
| `asset_category` | String(32) | `stocks` \| `commodities` \| `other` \| `indices` |
| `asset_id` | String(32) | Ticker or instrument name |
| `buy_in_price` | Float | Entry price at generation time |
| `stop_loss` | Float | ATR-based stop |
| `take_profit` | Float | ATR-based target |
| `expiry_date` | Date | Optional expiry |
| `rationale_ref` | String | FK to LLMRationaleLog |
| `rationale_text` | Text | Short LLM explanation |
| `risk_tier` | String(32) | `conservative` \| `moderate` \| `aggressive` |
| `direction` | String(8) | `long` \| `short` |
| `confidence_score` | Integer 0–100 | Raw composite confidence |
| `risk_reward` | Float | e.g. 2.5 |
| `volatility_notes` | Text | ATR context, gap risk notes |
| `status` | String(16) | `pending` \| `taken` \| `skipped` |
| `suggested_risk_pct` | Float | % of capital for position sizing |
| `instrument_type` | String(16) | `cfd` \| `invest` \| `etf` \| `spot` |
| `min_confidence_gate` | Integer | Gate used at generation time |
| `shadow_b_threshold` | Integer | Alternate gate (A/B testing) |
| `shadow_b_pass` | Boolean | Would rec have passed shadow gate? |

**Indexes**: `(batch_id, status)`, `batch_id`, `created_at`

---

### 4.3 `active_trades` — Standard table (state machine)

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(36) PK | UUID |
| `recommendation_id` | String(36) FK | → recommendations.id |
| `user_taken_at` | DateTime(tz) | When user marked "took it" |
| `current_sl` | Float | Current stop-loss level |
| `current_tp` | Float | Current take-profit level |
| `suggested_sl` | Float | Suggested new SL (not yet accepted) |
| `suggested_tp` | Float | Suggested new TP |
| `suggested_at` | DateTime(tz) | When suggestion was made |
| `expiry` | Date | Trade expiry date |
| `last_updated` | DateTime(tz) | Last update timestamp |
| `trade_phase` | String(24) | `open` \| `partial_tp` \| `trailing` \| `closed` |
| `breakeven_hit` | Boolean | True after 1R gain, SL moved to entry |
| `highest_price` | Float | Track for longs (trailing) |
| `lowest_price` | Float | Track for shorts (trailing) |
| `signal_flip_count` | Integer | # of times signal reversed |
| `outcome` | String(24) | `hit_tp` \| `hit_sl` \| `expired` \| `manual` |
| `outcome_at` | DateTime(tz) | When outcome was recorded |
| `closing_price` | Float | Price at close |
| `strategy_leg` | String(32) | `vanilla` \| `scale_out_2tp` \| `scale_out_3tp` \| ... |
| `position_fraction` | Float | Fraction of position (1.0 = full) |

**Indexes**: `recommendation_id`, `(recommendation_id, strategy_leg)`

---

### 4.4 `active_trade_levels` — History per update

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(36) PK | UUID |
| `active_trade_id` | String(36) FK | → active_trades.id |
| `sl` | Float | Stop-loss at this point in time |
| `tp` | Float | Take-profit at this point in time |
| `created_at` | DateTime(tz) | Timestamp |
| `source` | String(32) | `initial` \| `scheduled_update` |

---

### 4.5 `market_analyses` — TimescaleDB hypertable (partitioned by `as_of`)

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(36) PK | UUID |
| `as_of` | DateTime(tz) | Analysis timestamp; partition key |
| `sources_used` | JSON | List of feed sources included |
| `summary` | Text | Human-readable summary |
| `payload` | JSON | `{by_asset_sentiment, active_events, ...}` |

`payload.by_asset_sentiment`: `{asset_id: {score, count, trend, divergence}}`
`payload.active_events`: `[{name, type, window_start, window_end}]`

---

### 4.6 `daily_briefings` — TimescaleDB hypertable (partitioned by `created_at`)

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(36) PK | UUID |
| `created_at` | DateTime(tz) | Partition key |
| `content_text` | Text | LLM-generated narrative |
| `payload` | JSON | Checklist evaluation results |

---

### 4.7 `backtest_runs`

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(36) PK | UUID |
| `created_at` | DateTime(tz) | |
| `start_date`, `end_date` | Date | Backtest range |
| `symbols_json` | Text | JSON array of symbols |
| `risk_tier` | String(32) | |
| `min_confidence` | Integer | Filter gate used |
| `status` | String(16) | `running` \| `completed` \| `failed` |
| `error_message` | Text | |
| `total_signals`, `total_trades` | Integer | |
| `win_rate`, `sharpe_ratio`, `max_drawdown` | Float | |
| `avg_rr_realized`, `avg_rr_projected` | Float | |
| `metrics_json` | Text | Full metrics JSON |

---

### 4.8 `backtest_trades`

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(36) PK | UUID |
| `run_id` | String(36) FK | → backtest_runs.id |
| `symbol`, `asset_category`, `direction`, `risk_tier` | String | |
| `entry_date`, `entry_price`, `stop_loss`, `take_profit` | Date/Float | |
| `confidence_score` | Integer | |
| `risk_reward` | Float | |
| `outcome` | String(16) | `hit_tp` \| `hit_sl` \| `expired` |
| `closing_price`, `outcome_date`, `bars_held`, `pnl_r` | Float/Date/Int | |
| `volatility_regime`, `trend_bias`, `atr_pct` | String/Float | |

---

### 4.9 `llm_rationale_logs`

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(36) PK | UUID |
| `created_at` | DateTime(tz) | |
| `recommendation_id` | String(36) FK | → recommendations.id |
| `active_trade_id` | String(36) FK | → active_trades.id |
| `asset_id`, `asset_category`, `direction` | String | |
| `horizon` | String(8) | `near` \| `far` |
| `horizon_days` | Integer | Default 14 |
| `provider_id`, `model`, `prompt_version` | String | LLM provenance |
| `context_snapshot` | Text (JSON) | Full context sent to LLM |
| `rationale_text` | Text | LLM output |
| `validation_flags` | Text (JSON list) | Detected issues |
| `retry_count` | Integer | |
| `news_verified` | Boolean | |
| `news_verification_detail` | Text (JSON) | |
| `metrics_at_entry`, `metrics_verification` | Text (JSON) | Technical data at entry/later |
| `metrics_accuracy_score` | Float 0–1 | |
| `outcome`, `outcome_at`, `closing_price`, `pnl_r` | Various | Filled after trade closes |
| `quality_score` | Integer 0–10 | |
| `hallucination_rate` | Float 0–1 | |

**Indexes**: `(asset_id, direction, outcome)`, `(asset_category, horizon, outcome)`, `(provider_id, model, outcome)`, `created_at`

---

## 5. API Endpoints

Base URL: `http://localhost:6001`
All endpoints are prefixed with `/api`.

### 5.1 Health

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Returns `{status: "ok"}` |

---

### 5.2 Analysis

| Method | Path | Params | Returns |
|--------|------|--------|---------|
| GET | `/api/analysis/latest` | — | `{id, as_of, sources_used, summary, payload}` — latest MarketAnalysis row |

---

### 5.3 Research

| Method | Path | Params | Returns |
|--------|------|--------|---------|
| GET | `/api/research/providers` | — | `{providers: [{id, display_name, default_model, requires_api_key, available}], current: {provider, model}}` |
| GET | `/api/research/resolve` | `q=<company name>` | `{query, matches: [{name, ticker}]}` |
| GET | `/api/research/summary` | `asset_id`, `category`, `generate=bool` | `{context: {...}, summary: "LLM text"}` — on-demand LLM research summary |

---

### 5.4 Briefing

| Method | Path | Params | Returns |
|--------|------|--------|---------|
| GET | `/api/briefing/latest` | — | `{id, created_at, content_text, payload}` |
| GET | `/api/briefing/sparklines` | — | `{spx, vix, wti, brent, gold, silver}` — 30-day close arrays |
| GET | `/api/briefing/checklist` | — | `{checklist: [...]}` — checklist item evaluations |
| POST | `/api/briefing/run` | — | Triggers briefing job; returns DailyBriefing |

---

### 5.5 Recommendations

| Method | Path | Params | Returns |
|--------|------|--------|---------|
| GET | `/api/recommendations` | `batch_id`, `status`, `asset_category`, `group_by=category` | Recommendation list or grouped by category |
| GET | `/api/recommendations/export` | `batch_id`, `status` | CSV download |
| GET | `/api/recommendations/item/{recommendation_id}` | — | `{item: {...}, current_price}` |
| POST | `/api/recommendations/{recommendation_id}/took-it` | Body: `{strategy: "vanilla"}` | Creates ActiveTrade(s), marks rec as `taken` |
| POST | `/api/recommendations/{recommendation_id}/skipped` | — | Marks rec as `skipped` |

**Recommendation item fields**: `id`, `created_at`, `batch_id`, `asset_category`, `asset_id`, `buy_in_price`, `stop_loss`, `take_profit`, `risk_tier`, `direction`, `confidence_score`, `confidence_calibrated`, `risk_reward`, `suggested_risk_pct`, `volatility_notes`, `status`, `rationale_text`, `engine_policy` (tuning snapshot).

---

### 5.6 Trade Builder

| Method | Path | Params | Returns |
|--------|------|--------|---------|
| GET | `/api/trade-builder/strategies` | — | `{strategies: [{name, display_name, description}]}` |
| GET | `/api/trade-builder/static-explanation` | `strategy`, `num_tps` | HTML explanation of the strategy levels |
| POST | `/api/trade-builder/explain-strategy` | Body: `{recommendation_id, strategy}` | `{explanation: "LLM text"}` |
| POST | `/api/trade-builder/from-asset` | Body: `{asset_id, category, run_full_pipeline}` | Creates ad-hoc Recommendation for any asset |

---

### 5.7 Active Trades

| Method | Path | Params | Returns |
|--------|------|--------|---------|
| GET | `/api/active-trades` | `symbol`, `status`, `sort_by` | `{trades: [...]}` — all active trades with lifecycle info |
| GET | `/api/active-trades/sparkline/{asset_id}` | `days=30` | `{prices: [...]}` — price array for charting |
| GET | `/api/active-trades/prices` | `asset_ids=id1,id2,...` | `{prices: {asset_id: current_price}}` |
| GET | `/api/active-trades/{trade_id}/gap-advice` | — | LLM-generated gap risk mitigation advice |
| POST | `/api/active-trades/{trade_id}/close-manually` | Body: `{closing_price, outcome}` | Marks outcome (`hit_tp` \| `hit_sl` \| `manual`) |

---

### 5.8 Calibration

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/calibration` | `{fitted, a, b, accuracy_metrics, by_bucket: {low, medium, high, very_high}}` |
| POST | `/api/calibration/fit` | Fits Platt curve from closed trades; returns metrics |
| POST | `/api/calibration/clear` | Clears calibration coefficients |

---

### 5.9 Market Context

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/regime/status` | `{regime, spx_trend, vix_level, dxy_macro, recent_trades_closed_count, ...}` |
| GET | `/api/market/context` | `{dxy, regime, regime_description, should_reduce_exposure, confidence_adjustment, min_confidence_threshold}` |
| GET | `/api/market/sectors` | `{sectors: {Energy: {regime, trend, volatility}, ...}}` |
| GET | `/api/market/correlations` | `{matrix: {...}, signal_strength, key_signals}` |
| GET | `/api/market/correlations/analysis` | LLM analysis of current correlation patterns |

---

### 5.10 Macro Calendar

| Method | Path | Params | Returns |
|--------|------|--------|---------|
| GET | `/api/macro-calendar` | — | `{is_high_impact_today, upcoming_events, dates_cache_age_hours}` |
| POST | `/api/macro-calendar/refresh` | — | Force refresh (rate-limited) |

---

### 5.11 Preferences

| Method | Path | Body | Returns |
|--------|------|------|---------|
| GET | `/api/preferences` | — | Full preferences object |
| PATCH | `/api/preferences` | Partial update fields | Updated preferences state |

**Preferences fields**: `preferred_risk`, `confidence_thresholds`, `shadow_mode_enabled`, `sentiment_half_life`, `risk_budget_1r`, `circuit_breaker_enabled`, `commodity_engine_gates`, `non_commodity_engine_gates`, `earnings_calendar_config`, `gap_risk_config`.

---

### 5.12 Notifications

| Method | Path | Body | Returns |
|--------|------|------|---------|
| GET | `/api/notifications/config` | — | `{webhook_url, event_types}` |
| PATCH | `/api/notifications/config` | `{webhook_url, event_types}` | Updated config |
| POST | `/api/notifications/test` | — | Sends test webhook payload |

**Event types**: `EVENT_BATCH_READY`, `EVENT_REGIME_CHANGE`, `EVENT_TRADE_ALERT`

---

### 5.13 Pipeline Control

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/pipeline/progress` | `{stage, progress_pct, logs}` — real-time pipeline state |
| POST | `/api/actions/run-pipeline-once` | Runs ingestion → analysis → batch in background |
| POST | `/api/actions/refresh-todays-batch` | Re-runs daily batch |
| POST | `/api/actions/clear-todays-batch` | Deletes today's pending recommendations |
| POST | `/api/actions/update-active-trades` | Forces immediate trade update |
| POST | `/api/actions/purge-all` | **Destructive**: deletes all recommendations and active trades |

---

### 5.14 Circuit Breaker

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/circuit-breaker` | `{state: "paused"|"defensive"|"normal", last_transition, reason, sizing_multiplier}` |

---

### 5.15 Backtesting (`app/api/backtest_routes.py`)

| Method | Path | Params/Body | Returns |
|--------|------|-------------|---------|
| POST | `/api/backtest/run` | `{start_date, end_date, symbols?, risk_tier, min_confidence, period}` | `{run_id, status: "running", message}` |
| GET | `/api/backtest/runs` | — | List of all runs with summary metrics |
| GET | `/api/backtest/runs/{run_id}` | — | Full run details + metrics |
| GET | `/api/backtest/runs/{run_id}/trades` | `offset`, `limit`, `outcome`, `symbol` | Paginated trade list |
| DELETE | `/api/backtest/runs/{run_id}` | — | Deletes run and its trades |
| GET | `/api/backtest/quick` | `symbol`, `days` | Single-symbol quick backtest (sync, ~15s) |

---

## 6. Scheduler Jobs

Defined in `app/jobs/scheduler.py`. Uses APScheduler with asyncio backend.

### `job_ingest_and_sentiment()` — Daily @ `daily_batch_hour` UTC

**Flow**:
1. Fetch all RSS feeds (`services/ingestion/feeds.py` → `services/ingestion/rss.py`)
2. Save new `FeedItem` rows (dedupe by `id`, skip existing)
3. Score sentiment: VADER + FinBERT + AFINN → composite score in \[-1, 1\]
4. Tag assets: NER + keyword dict + dynamic ticker detection → `asset_tags: [...]`
5. Mark items `is_processed = True`

**Output**: Populated `feed_items` table with sentiment + tags.

---

### `job_run_analysis()` — After ingestion (configurable interval)

**Flow**:
1. Load recent `FeedItem` rows where `is_processed = True`
2. Group by `asset_tag`
3. Weight each article: recency (exponential decay by `sentiment_half_life`), source reliability, entity importance
4. Compute weighted average sentiment per asset
5. Detect sentiment–price divergence (compare to previous close from yfinance)
6. Identify active macro events (FOMC / CPI / NFP windows from `macro_calendar.py`)
7. Save `MarketAnalysis` row with `payload = {by_asset_sentiment, active_events}`

**Output**: `market_analyses` table row.

---

### `job_daily_batch()` — Daily @ `daily_batch_hour` UTC (after analysis)

**Flow**:
1. Load latest `MarketAnalysis` payload
2. Load already-taken assets (exclude from batch)
3. Load closed trades (for circuit breaker evaluation)
4. Build candidates per category — calls `engine.build_daily_candidates()`
   - For each candidate: compute technicals, ATR SL/TP, direction, confidence, R:R
   - Apply gates: confidence threshold, R:R floor, indicator conflict, correlation, risk budget
   - Keep top `MAX_RECOMMENDATIONS_PER_CATEGORY` (5) per category
5. Generate LLM rationales async (non-blocking, fire-and-forget)
6. Save `Recommendation` rows (`status = "pending"`)
7. Send `EVENT_BATCH_READY` webhook notification

**Logs**:
- `[ENGINE_SKIP]` JSON per skipped candidate (reason, asset, category, batch_date)
- `[ENGINE_DIGEST]` JSON at end (total candidates, skips by reason, by category)

---

### `job_update_active_trades()` — Every `update_active_trades_interval_hours` (default 2h)

**Flow**:
1. Clear technicals cache (`clear_cache()`)
2. Detect regime transition → send `EVENT_REGIME_CHANGE` if changed
3. For each open `ActiveTrade`:
   - Fetch latest price + ATR
   - Check TP hit → `outcome = "hit_tp"`, close trade
   - Check SL hit → `outcome = "hit_sl"`, close trade
   - Check expiry → `outcome = "expired"`, close trade
   - If 1R profit reached and `breakeven_hit = False` → suggest breakeven SL move, set `trade_phase = "partial_tp"`
   - Otherwise trail SL behind structure (ATR-based)
   - Save `ActiveTradeLevel` row for history
   - Count signal flips (`signal_flip_count`)
4. Log all updates to file

---

### `job_daily_briefing()` — Daily (configurable cadence)

**Flow**:
1. Evaluate checklist (SPX close, VIX level, WTI/Brent, sentiment avg, recs count, regime)
2. Generate LLM narrative summarizing market state
3. Save `DailyBriefing` row

---

### `job_feed_cleanup()` — Weekly

**Flow**: Delete `FeedItem` rows older than `feed_max_age_days` (default 30).

---

## 7. Core Services — Functions

### 7.1 `app/core/config.py` — Settings

**Class**: `Settings` (Pydantic BaseSettings)

Loads `.env` file. Provides all runtime config. Instantiated once as module-level `settings`.

---

### 7.2 `app/core/preferences.py` — Persistent User Preferences

Stored in `data/preferences.json`. All functions read/write this file.

| Function | Signature | Description |
|----------|-----------|-------------|
| `get_preferred_risk()` | `() → str` | Returns `conservative`\|`moderate`\|`aggressive` |
| `set_preferred_risk(value)` | `(str) → None` | Validates and saves risk stance |
| `get_research_llm_config()` | `() → dict` | `{provider, model, ollama_base_url, thinking_budget}` |
| `set_research_llm_config(provider, model, ollama_base_url, thinking_budget)` | `(...) → None` | Saves LLM config |
| `get_risk_budget_1r()` | `() → float` | Max sum of `suggested_risk_pct` per batch (default 25%) |
| `set_risk_budget_1r(value)` | `(float) → None` | Max 50% |
| `get_sentiment_half_life()` | `() → int` | Decay rate in hours (default 24, range 1–168) |
| `set_sentiment_half_life(value)` | `(int) → None` | |
| `get_confidence_thresholds()` | `() → dict` | `{risk_on: 30, cautious: 35, risk_off: 40}` |
| `set_confidence_thresholds(thresholds)` | `(dict) → None` | |
| `get_shadow_mode_config()` | `() → dict` | `{enabled, delta}` — A/B testing |
| `set_shadow_mode_config(enabled, delta)` | `(bool, int) → None` | Delta range: -20 to +30 |
| `get_commodity_engine_gates()` | `() → dict` | `{confidence_offset, min_rr}` |
| `set_commodity_engine_gates(...)` | `(...) → None` | |
| `get_non_commodity_engine_gates()` | `() → dict` | `{confidence_offset, min_rr?}` |
| `set_non_commodity_engine_gates(...)` | `(...) → None` | |
| `get_earnings_calendar_config()` | `() → dict` | `{window_days: 2, penalty: 15, skip: false}` |
| `set_earnings_calendar_config(...)` | `(...) → None` | |
| `get_circuit_breaker_config()` | `() → dict` | `{enabled, consecutive_loss_limit: 5, daily_loss_r: 3.0, drawdown_gate: 8.0}` |
| `set_circuit_breaker_config(...)` | `(...) → None` | |
| `get_gap_risk_config()` | `() → dict` | `{enabled, max_penalty_multiplier: 0.50}` |
| `set_gap_risk_config(...)` | `(...) → None` | |

---

### 7.3 `app/core/database.py` — Async DB Setup

| Function | Description |
|----------|-------------|
| `init_db()` | Creates TimescaleDB extension if needed; creates all tables from ORM models |
| `get_db()` | Async generator yielding `AsyncSession`; used as FastAPI dependency |

**Pool**: `pool_size=10`, `max_overflow=20`, `pool_pre_ping=True`

---

### 7.4 `services/recommendations/engine.py` — Recommendation Engine

| Function | Signature | Description |
|----------|-----------|-------------|
| `build_daily_candidates` | `(by_asset_sentiment, preferred_risk, tie_seed, exclude_taken, active_events, risk_budget_1r, closed_trades) → List[Candidate]` | Top-level batch builder. Calls per-category builder, applies correlation filter + risk budget trim. |
| `build_daily_candidates_for_category` | `(category, by_asset_sentiment, ...) → List[Candidate]` | Generates sentiment-tagged ideas + discovery stocks + backfill from static universe. Filters to top 5. |
| `build_trade_builder_candidate` | `(asset_id, category, by_asset_sentiment, preferred_risk, active_events) → (Candidate|None, str)` | On-demand single-asset candidate (Trade Builder feature). |
| `_build_candidate` | `(asset_id, category, direction, market_ctx, ...) → (Candidate|None, skip_reason)` | Computes SL/TP, confidence, applies all gates. Returns candidate or skip reason string. |
| `compute_sl_tp_for_symbol` | `(symbol, direction, atr_multiplier) → (float, float)` | Fetches live ATR; returns (sl, tp). |
| `compute_atr_levels` | `(price, direction, atr_pct, risk_reward, ...) → (float, float)` | Pure ATR math; (sl, tp) from price + ATR%. |
| `determine_direction` | `(category, asset_id, market_ctx, ...) → str` | `"long"` or `"short"`. Regime-aware, sentiment-aware, multi-TF aligned. |
| `compute_confidence` | `(snap, sentiment_score, risk_tier, market_ctx, ...) → int` | Multi-factor 0–100 score. |
| `get_sentiment_score_and_count` | `(asset_id, by_asset_sentiment) → (float, int)` | Pulls asset sentiment from analysis payload. |

**Constants**:
- `MAX_POOL_PER_CATEGORY = 15` — candidate pool size before filtering
- `MAX_RECOMMENDATIONS_PER_CATEGORY = 5` — final recommendations per category
- `SENTIMENT_TAG_TO_ASSETS` — dict mapping sentiment tags to (category, asset_id)
- `ASSETS_BY_CATEGORY` — static universe per category with default risk tiers

**Global caches** (module-level, reset before each batch):
- `_vp_by_symbol` — volume profile data per symbol
- `_rs_by_symbol` — relative strength (20d, 60d, sector) per symbol
- `_gap_risk_by_symbol` — gap risk level + note per symbol

---

### 7.5 `services/recommendations/asset_class_policy.py` — Per-Class Tuning

| Function | Description |
|----------|-------------|
| `get_asset_class_policy(category, asset_id) → AssetClassPolicy` | Returns the tuning bundle for a given (category, asset_id) pair |

**`AssetClassPolicy` fields**:
- `min_confidence_offset` — added to base gate (can be negative)
- `equity_regime_adjustment_scale` — multiplier on regime confidence delta
- `correlation_divergence_penalty_scale` — multiplier on divergence penalty
- `mtf_aligned_confidence_bonus` — bonus when multi-TF trend aligns
- `conflict_skip_threshold` — indicator conflict score above which candidate is skipped
- `min_risk_reward` — floor R:R ratio (overrides preferences if set)
- `news_boost` — config dict for news-based confidence boost
- `direction_style` — `"standard"` \| `"macro_linked"` \| `"index_macro"`

**Policies**: `_POLICY_STOCKS`, `_POLICY_INDICES`, `_POLICY_VIX`, `_POLICY_COMMODITIES`, `_POLICY_FX`, `_POLICY_CRYPTO`

---

### 7.6 `services/technicals.py` — Multi-Timeframe Technical Analysis

| Function | Signature | Description |
|----------|-----------|-------------|
| `fetch_ohlcv` | `(symbol, period="90d", interval=None) → pd.DataFrame|None` | Downloads OHLCV from yfinance; cached per session |
| `build_snapshot` | `(symbol) → TechnicalSnapshot` | Full snapshot: all indicators, S/R, volume profile |
| `compute_volume_profile` | `(symbol, n_bins, hvn_pct_threshold, ...) → (vpoc, vah, val, hvn_levels, lvn_levels)` | Volume-at-price histogram |
| `determine_direction` | `(symbol, category, sentiment, market_regime, direction_style) → str` | Regime + sentiment + MTF → `"long"`\|`"short"` |
| `compute_confidence` | `(snap, sentiment, risk_tier, direction, market_ctx, ...) → int` | 0–100 composite confidence |
| `compute_technical_score` | `(snap, direction, risk_tier, category, ...) → float` | Raw indicator score (pre-adjustment) |
| `compute_atr_levels` | `(price, direction, atr_pct, min_rr, ...) → (float, float)` | (sl, tp) from ATR |
| `get_indicator_conflict_penalty` | `(snap, direction, category, ...) → int` | 0–25+; above threshold → skip |
| `check_trend_alignment` | `(symbol) → TrendAlignment` | 1D + 4H + 1H trends + consensus |
| `clear_cache` | `() → None` | Clears `_ohlcv_cache` and all derived caches |
| `set_backtest_mode` | `(cutoff_date) → None` | Limits all data fetches to ≤ cutoff_date |
| `clear_backtest_mode` | `() → None` | Restores live mode |

**`TechnicalSnapshot` dataclass fields**:
- `price`, `atr`, `atr_pct`
- `rsi`, `macd_line`, `macd_signal`
- `bb_upper`, `bb_middle`, `bb_lower`
- `adx`, `di_plus`, `di_minus`
- `stoch_k`, `stoch_d`
- `obv`, `obv_slope_5d`
- `vwap`
- `support_levels: List[float]`, `resistance_levels: List[float]`
- `vpoc`, `vah`, `val`, `hvn_levels`, `lvn_levels`, `vp_bias`, `vp_overextension`

**Computed properties**:
- `price_above_vwap`, `is_overbought`, `is_oversold`
- `macd_bullish`, `adx_trending`
- `stoch_overbought`, `near_upper_band`
- `nearest_support_below`, `in_value_area`

---

### 7.7 `services/market_context.py` — Regime Detection

| Function | Signature | Description |
|----------|-----------|-------------|
| `build_market_context` | `(include_sectors=True) → MarketContext` | Full context used once per batch |
| `detect_market_regime` | `() → MarketRegime` | SPX/VIX regime: `risk_on`\|`cautious`\|`risk_off` |
| `detect_dxy_macro` | `() → DxyMacro` | DXY spot vs MA-50d; trend + confidence delta |
| `detect_all_sector_regimes` | `() → Dict[str, SectorRegime]` | All 11 GICS sectors |
| `check_cross_asset_signals` | `() → List[CorrelationSignal]` | Divergence signals across asset classes |

**`MarketContext` methods**:
- `regime_direction_bias() → "bullish"|"bearish"|"neutral"`
- `regime_adjustment() → int` — confidence delta from SPX/VIX (risk_on: +5, cautious: 0, risk_off: -10)
- `confidence_adjustment() → int` — multi-factor: regime + divergence + macro + adaptive
- `min_confidence_threshold() → int` — per-regime gate with optional adaptive override
- `dxy_confidence_delta(category, asset_id, direction) → int` — DXY impact on specific direction
- `describe() → str` — human-readable summary

**Sectors tracked**: Energy, Materials, Industrials, Utilities, Healthcare, Financials, Consumer Discretionary, Consumer Staples, IT, Comm Services, Real Estate

---

### 7.8 `services/sentiment/ticker_detection.py`

| Function | Signature | Description |
|----------|-----------|-------------|
| `tag_assets` | `(text) → List[str]` | Returns asset names (e.g. `["oil", "gold", "AAPL"]`) |
| `detect_tickers` | `(text) → List[str]` | Dynamic ticker extraction from article text |
| `is_likely_ticker` | `(s) → bool` | Heuristic: 1–5 uppercase chars, not in blocklist |
| `get_blocklist` | `() → frozenset[str]` | Common false-positive words to filter |

---

### 7.9 `services/analysis/aggregate.py` — Sentiment Aggregation

| Function | Description |
|----------|-------------|
| `run_analysis(session) → MarketAnalysis` | Full aggregation pipeline: load FeedItems → weight → average → divergence → events → save |

**Weighting factors**:
- Recency: `exp(-hours_old / sentiment_half_life)`
- Source reliability: configurable per-source multiplier
- Entity importance: boosted for primary vs secondary mentions

---

### 7.10 `services/backtesting/`

| File | Function | Description |
|------|----------|-------------|
| `replay.py` | `run_backtest(symbols, start_date, end_date, risk_tier, min_confidence, ...) → List[dict]` | Historical signal replay |
| `simulator.py` | `simulate_outcome(entry_date, entry_price, sl, tp, direction, future_bars) → SimulatedOutcome` | Simulates trade outcome from future OHLCV bars |
| `metrics.py` | `compute_metrics(trades) → dict` | `{win_rate, sharpe_ratio, max_drawdown, avg_rr_realized, by_bucket}` |
| `data_provider.py` | `get_engine_symbols() → List[str]` | All symbols in engine universe |
| `data_provider.py` | `prefetch_symbols(symbols, period) → Dict[str, pd.DataFrame]` | Bulk OHLCV download |
| `data_provider.py` | `get_trading_days(start_date, end_date) → List[date]` | List of market open days |

---

### 7.11 `services/circuit_breaker.py`

| Function | Signature | Description |
|----------|-----------|-------------|
| `compute_state` | `(closed_trades, today_pnl_r) → CircuitBreakerState` | Returns `normal`\|`defensive`\|`paused` |

**Trigger conditions**:
- `paused`: consecutive losses ≥ `consecutive_loss_limit` OR daily loss ≥ `daily_loss_r`
- `defensive`: equity drawdown ≥ `drawdown_gate`
- In `defensive` mode: confidence penalty applied + `sizing_multiplier < 1.0`

---

### 7.12 `services/calibration.py` — Platt Scaling

| Function | Signature | Description |
|----------|-----------|-------------|
| `fit_platt_calibration` | `(confidences, outcomes) → (float, float)` | Fits logistic regression; returns (a, b) coefficients |
| `apply_platt_calibration` | `(raw_confidence) → float` | Maps raw int to calibrated probability 0–1 |
| `get_platt_params` | `() → (float, float)|None` | Loads from `data/platt.json` |
| `set_platt_params` | `(a, b) → None` | Saves to `data/platt.json` |

**Formula**: `P = 1 / (1 + exp(-(a * confidence + b)))`

---

### 7.13 `services/macro_calendar.py` / `macro_calendar_fetcher.py`

| Function | Signature | Description |
|----------|-----------|-------------|
| `get_high_impact_dates` | `(force_refresh=False) → Set[tuple]` | Returns set of (year, month, day) tuples |
| `is_high_impact_day` | `(d, window_days=2) → bool` | True if within window of a high-impact event |
| `get_macro_overlay_info` | `(d, window_days) → dict` | `{is_high_impact, upcoming_events, macro_day_confidence_penalty}` |

**Event sources**: Fed calendar (FOMC), CPI approximation, NFP (first Friday of month).
**Cache**: `data/macro_calendar_cache.json` (refreshed weekly).

---

### 7.14 `services/research/llm.py` — LLM Dispatch

| Function | Signature | Description |
|----------|-----------|-------------|
| `generate_rationale` | `(context: dict) → str` | Routes to configured provider; returns 2–4 sentence rationale |

**Providers**: `openai`, `anthropic` (with extended thinking up to 16k tokens), `ollama`, `openrouter`.
**Prompt variants**: near-horizon (≤7 days) vs far-horizon (8–14 days) system prompts.
**Post-processing**: hallucination detection, validation flags, retry on failure.

---

### 7.15 `services/notifications.py`

| Function | Signature | Description |
|----------|-----------|-------------|
| `send_notification` | `(event_type, payload) → None` | POSTs JSON to configured webhook URL |

**Event types**: `EVENT_BATCH_READY`, `EVENT_REGIME_CHANGE`, `EVENT_TRADE_ALERT`

---

### 7.16 `services/pipeline_progress.py`

| Function | Signature | Description |
|----------|-----------|-------------|
| `update` | `(pct, stage, message) → None` | Updates real-time progress for `/api/pipeline/progress` |
| `log_stage` | `(name, status, message, warnings) → None` | Logs stage completion with status |

---

### 7.17 `services/adaptive_thresholds.py`

| Function | Signature | Description |
|----------|-----------|-------------|
| `compute_adaptive_threshold` | `(by_bucket) → int` | Computes new min confidence gate from historical hit rates per bucket |
| `update_adaptive_thresholds` | `() → None` | Recomputes and saves to preferences |
| `get_adaptive_threshold` | `() → int|None` | Returns stored adaptive threshold or None |

---

### 7.18 `services/ticker_resolver.py`

| Function | Signature | Description |
|----------|-----------|-------------|
| `resolve_name` | `(name) → str` | Resolves company name or ticker to preferred Yahoo Finance symbol |

**Cache**: `data/ticker_cache.json`, TTL 90 days.
**Exchange preference order**: US (NMS, NYQ, NGM) > Europe > Asia.

---

### 7.19 `services/earnings_calendar.py`

| Function | Signature | Description |
|----------|-----------|-------------|
| `get_next_earnings` | `(symbol) → date|None` | Next earnings date from yfinance |
| `is_near_earnings` | `(symbol, window_days=2) → (bool, int)` | Returns (near_flag, days_to_earnings) |

**Cache**: 24 hours in-memory.

---

### 7.20 `services/ingestion/feeds.py`

| Function | Description |
|----------|-------------|
| `default_feeds() → List[FeedSpec]` | 20+ curated RSS specs: Bloomberg, Reuters, CNBC, FT, etc. |

**`FeedSpec` fields**: `name`, `category`, `url`, `fetch_method`

---

## 8. Data Models (ORM)

All models in `app/models/`, inherit from `app.core.base.Base`.

| Model | File | Key relationships |
|-------|------|-------------------|
| `FeedItem` | `feed_item.py` | Standalone |
| `Recommendation` | `recommendation.py` | Has many `ActiveTrade`, has `LLMRationaleLog` |
| `ActiveTrade` | `recommendation.py` | Belongs to `Recommendation`; has many `ActiveTradeLevel` |
| `ActiveTradeLevel` | `recommendation.py` | Belongs to `ActiveTrade` |
| `MarketAnalysis` | `market_analysis.py` | Standalone |
| `DailyBriefing` | `daily_briefing.py` | Standalone |
| `BacktestRun` | `backtest.py` | Has many `BacktestTrade` |
| `BacktestTrade` | `backtest.py` | Belongs to `BacktestRun` |
| `LLMRationaleLog` | `llm_rationale_log.py` | Belongs to `Recommendation` or `ActiveTrade` |

---

## 9. Static UI Pages

Located at `/app/static/`. Served under `/`.

| Route | File | Purpose |
|-------|------|---------|
| `/dashboard` | `dashboard.html` | Run pipeline, view progress, trigger actions |
| `/recommendations` | `recommendations.html` | View today's batch by category; mark took-it/skipped; export CSV |
| `/trade-builder` | `trade-builder.html` | Create ad-hoc trade plan; select strategy (vanilla, scale-out) |
| `/analysis` | `analysis.html` | Market analysis report (summary + full payload) |
| `/active-trades` | `active-trades.html` | Monitor open trades; update SL/TP; close manually |
| `/research` | `research.html` | Look up asset; technical + sentiment context; LLM summary |
| `/briefing` | `briefing.html` | Daily checklist (SPX, VIX, commodities, sentiment, regime) |
| `/journal` | `journal.html` | Closed trade outcomes; Brier score; calibration feedback |
| `/calendar` | `calendar.html` | Macro event calendar (FOMC, CPI, NFP dates) |
| `/backtest` | `backtest.html` | Run historical backtest; view Sharpe/win rate/drawdown |

**UX rule**: All new buttons, sections, and cards must include a hovering tooltip that appears after 3 seconds.

---

## 10. Full Data Workflows

### 10.1 Daily Recommendation Pipeline

```
[06:00 UTC] job_ingest_and_sentiment()
  └─ Fetch RSS → Save FeedItems → Sentiment score → Tag assets

[06:15 UTC] job_run_analysis()
  └─ Aggregate by asset → Weighted sentiment → Divergence → Macro events → Save MarketAnalysis

[06:30 UTC] job_daily_batch()
  └─ Load MarketAnalysis payload
  └─ For each category (stocks, commodities, other, indices):
       Generate ideas (sentiment tags + discovery + static universe)
       │
       ├─ For each candidate:
       │    build_snapshot()         → TechnicalSnapshot (RSI, MACD, ATR, VP, S/R, ...)
       │    determine_direction()    → long | short
       │    compute_atr_levels()     → (sl, tp)
       │    compute_confidence()     → 0–100
       │    apply_gates()            → confidence gate, R:R floor, conflict penalty, correlation, budget
       │    apply_shadow_mode()      → log shadow_b_pass
       │
       └─ Keep top 5 per category → Save Recommendation rows (status=pending)
       └─ Fire-and-forget: generate_rationale() for each rec (LLM async)
       └─ send_notification(EVENT_BATCH_READY)

[User] GET /api/recommendations?group_by=category
[User] POST /api/recommendations/{id}/took-it → Creates ActiveTrade(s)
```

### 10.2 Trade Lifecycle

```
Recommendation (status=pending)
  └─ User: POST /took-it → ActiveTrade (trade_phase=open)
       │
       [Every 2h] job_update_active_trades()
       │
       ├─ Price hit TP? → outcome=hit_tp, phase=closed
       ├─ Price hit SL? → outcome=hit_sl, phase=closed
       ├─ Expired?      → outcome=expired, phase=closed
       ├─ 1R profit?    → breakeven_hit=True, phase=partial_tp
       └─ Otherwise     → trail SL, save ActiveTradeLevel
       │
       [User] POST /active-trades/{id}/close-manually → outcome=manual
```

### 10.3 Confidence Calibration Loop

```
Closed ActiveTrades + LLMRationale outcomes
  └─ POST /api/calibration/fit
       └─ fit_platt_calibration(confidences, outcomes) → (a, b)
       └─ Save to data/platt.json
       └─ GET /api/recommendations → confidence_calibrated field uses apply_platt_calibration()
```

### 10.4 Backtesting Workflow

```
POST /api/backtest/run {start_date, end_date, symbols, risk_tier, min_confidence}
  └─ BacktestRun saved (status=running)
  └─ Background task:
       prefetch_symbols() → historical OHLCV
       For each trading day in range:
         set_backtest_mode(date)
         build_daily_candidates() → signals
         simulate_outcome()       → hit_tp | hit_sl | expired
       compute_metrics()          → win_rate, sharpe, drawdown, by_bucket
       BacktestRun updated (status=completed)

GET /api/backtest/runs/{run_id} → metrics
GET /api/backtest/runs/{run_id}/trades → individual trade outcomes
```

---

## 11. Configuration & Preferences

### Files

| File | Description |
|------|-------------|
| `data/preferences.json` | User preferences (auto-created, persisted across restarts) |
| `data/platt.json` | Platt calibration coefficients `{a, b}` |
| `data/macro_calendar_cache.json` | FOMC/CPI/NFP dates cache (weekly TTL) |
| `data/briefing_checklist.json` | Customizable daily checklist items |
| `data/ticker_cache.json` | Resolved ticker → Yahoo symbol (90-day TTL) |

### Migrations

Alembic migrations in `/migrations/versions/`. Run via `alembic upgrade head` (handled by `entrypoint.sh`).

---

## 12. Build & Ops

### Commands

```bash
make build start    # Rebuild images and start containers
make start          # docker compose up -d --remove-orphans
make stop           # docker compose down
make build          # docker compose build --no-cache
make pull           # docker compose pull
```

### Containers

| Service | Image | Port |
|---------|-------|------|
| `timescaledb` | `timescale/timescaledb:latest-pg16` | 5432 |
| `app` | Built from `Dockerfile` | 6001→8000 |

### Volumes

| Volume | Mountpoint | Contents |
|--------|------------|----------|
| `trading_data` | `/app/data` | SQLite DB, preferences, caches, platt params |
| `timescale_data` | `/var/lib/postgresql/data` | TimescaleDB data files |

### Scripts

| Script | Description |
|--------|-------------|
| `scripts/run_pipeline_once.py` | Run full ingestion → analysis → batch once |
| `scripts/refresh_todays_batch.py` | Regenerate today's batch |
| `scripts/migrate_sqlite_to_pg.py` | One-time SQLite → PostgreSQL migration |

---

## 13. Tests

Located in `/tests/unit/`.

| Module | Tests |
|--------|-------|
| `backtesting/test_metrics.py` | Win rate, Sharpe, drawdown calculations |
| `backtesting/test_simulator.py` | Trade outcome simulation |
| `sentiment/test_event_tagging.py` | Macro event detection |
| `sentiment/test_ticker_detection.py` | Asset tagging + ticker extraction |
| `sentiment/test_vader.py` | VADER sentiment scoring |
| `services/test_adaptive_thresholds.py` | Adaptive confidence gate |
| `services/test_calibration.py` | Platt scaling |
| `services/test_circuit_breaker.py` | State machine transitions |
| `services/test_macro_calendar.py` | FOMC/CPI/NFP window detection |
| `services/test_regime_monitor.py` | Market regime detection |

Run with: `pytest tests/`

---

## 14. Dependency Map

```
routes.py
  ├─ engine.py          ← technicals.py, market_context.py, asset_class_policy.py
  ├─ preferences.py
  ├─ calibration.py
  ├─ circuit_breaker.py
  ├─ macro_calendar.py
  ├─ notifications.py
  ├─ research/llm.py    ← research/providers.py, research/validator.py
  └─ pipeline_progress.py

scheduler.py (jobs)
  ├─ ingestion/rss.py   ← ingestion/feeds.py
  ├─ sentiment/         ← vader.py, finbert.py, ticker_detection.py, event_tagging.py
  ├─ analysis/aggregate.py
  ├─ engine.py          (full candidate pipeline)
  ├─ briefing.py
  └─ notifications.py

backtest_routes.py
  └─ backtesting/replay.py ← technicals.py (backtest_mode), simulator.py, metrics.py, data_provider.py
```

---

> **Note for agents**: This app does not install packages manually in containers. All new dependencies must be added to `requirements.txt`. To apply changes: `make build start`. Use `docker logs` for debugging.
