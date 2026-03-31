# Trading App — Plan & Progress

**Single source of truth** for the trading insight app: purpose, architecture, spec, flows, constraints, backlog, and progress.

**Quick links:** [1 Purpose](#1-purpose--context) · [2 Architecture](#2-architecture) · [3 Phase 1](#3-phase-1--market-analysis-spec) · [4 Phase 2](#4-phase-2--trade-recommendations-spec) · [5 Flows](#5-flows-and-constraints) · [6 Tech](#6-tech-stack-current) · [7 Legal](#7-legal--compliance) · [8 Backlog](#8-backlog--future-refinements) · [8.4a Dexter research](#84a-dexter-inner-workings-research-summary) · [8.4 Option B](#84-leveraging-dexter-we-proceed-with-option-b-emulate) · [9 Progress](#9-progress) · [10 Next steps](#10-suggested-next-steps-order-with-option-b-in-mind)

---

## 1. Purpose & context

- **Purpose:** Trading insight from news + sentiment + technicals + regime: (1) **Market analysis**, (2) **Structured trade recommendations** (stocks, commodities, forex, indices) with **Took it / Skipped**, **active trade** lifecycle, **outcomes**, **journal**, and **calibration**.
- **Principles:** Up to **5 recommendations per category per day** (~**20** total before correlation/risk-budget trims); one batch per calendar day; favor **conservative** assets when the user prefers less risk; updates only for **taken** trades until closed.
- **Context:** Build on codeops-trading-app ideas; reuse RSS feeds and data shapes. Optional: [claude-equity-research](https://github.com/quant-sentiment-ai/claude-equity-research) for equity research.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         TRADING INSIGHT APP                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│  PHASE 1: MARKET ANALYSIS                                                    │
│  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────────┐  │
│  │ News/RSS     │   │ Price &     │   │ Sentiment   │   │ Analysis        │  │
│  │ Ingestion    │──▶│ Order Flow  │──▶│ & Conditions│──▶│ Report /        │  │
│  │ (reuse/adapt)│   │ (APIs)      │   │ Enrichment  │   │ Summary Output  │  │
│  └─────────────┘   └─────────────┘   └─────────────┘   └─────────────────┘  │
├─────────────────────────────────────────────────────────────────────────────┤
│  PHASE 2: TRADE RECOMMENDATIONS (≤5 per category / day, volatility-aware)   │
│  Engine → daily batch → User: Took it / Skipped → Active trades → Journal   │
│  Took it → Active trade (updates SL/TP). Skipped → no updates.               │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Phase 1 — Market analysis (spec)

- **Goal:** One coherent **market analysis** (news + sentiment + price/conditions) as input for Phase 2.
- **Pipeline:** Ingest (RSS, price, conditions) → Enrich (sentiment, asset tags) → Aggregate → Output (report + “last analysis” artifact).
- **Analysis output:** `as_of`, `sources_used`, `summary`, `payload` (by_category, by_asset_sentiment, **price_snapshot** / trends / divergence — Yahoo enrichment only for **high-count** tags, capped; see `aggregate.py`).
- **Deliverables:** Ingestion, sentiment + tagging, market analysis output, optional MCP tools, Cursor rules.

---

## 4. Phase 2 — Trade recommendations (spec)

- **Goal:** Up to **5 structured recommendations per category per day** (stocks, commodities, other, indices), then **global** correlation and **risk-budget** filters; user marks Took it / Skipped; updates only for open active trades until closed.
- **Recommendation schema:** `id`, `created_at`, `batch_id`, `asset_category`, `asset_id`, `buy_in_price`, `stop_loss`, `take_profit`, `expiry_date`, `rationale_ref`, `rationale_text` (optional LLM), `risk_tier`, `direction`, `confidence_score`, `risk_reward`, `volatility_notes`, `suggested_risk_pct`, `status` (pending | taken | skipped).
- **Asset categories:** **stocks**, **commodities**, **other** (e.g. forex), **indices** (implemented; see §9).
- **Daily cap:** One batch per calendar day (`batch_id` = date); at most **5 per category** (~20 before trims); engine ranks within category then applies global gates.
- **User feedback:** Took it → create **ActiveTrade** (recommendation_id, user_taken_at, current_sl, current_tp, expiry, last_updated). Skipped → status = skipped; no active trade.
- **Scheduling:** Daily batch at configured time; update job (e.g. every 2h) only refreshes **active trades**.
- **Risk tier:** conservative | moderate | aggressive; user preference (exposed in UI) filters/ranks candidates.

---

## 5. Flows and constraints

### 5.1 Design principles

| Principle | Implication |
|-----------|-------------|
| Don’t assault the user | At most **5 per category** per day (~20 ceiling before filters). |
| Continue with the same set | Same batch until user responds; no second batch same day unless refresh action. |
| Respect risk preference | Tag risk_tier; favor conservative when user prefers. |
| Updates only for taken trades | Took it → active trade gets SL/TP updates. Skipped → no updates. |

### 5.2 When to generate vs update

| Condition | Action |
|-----------|--------|
| Same day, batch already exists | Do **not** create new batch. Run **update job** for active trades only. |
| New calendar day | Create **one new batch** (batch_id = today), up to **5 per category**. |
| User marks Took it | Create/update **active trade**; include in update job. |
| User marks Skipped | status = skipped; no active trade; no future updates. |
| Update job (e.g. every 2h) | Only **modifies active trades** (SL/TP, validity). Does not create new recommendations. |

### 5.3 Constraints (C1–C6)

| # | Constraint | Note |
|---|------------|------|
| C1 | Max **5 per category** per day | Engine targets up to 5 × 4 categories; global correlation + risk budget may reduce count. |
| C2 | One batch per day | batch_id = calendar day. |
| C3 | No new batch same day | Guard: if today has pending batch, skip generation. |
| C4 | Updates only for taken trades | Update job touches active_trades only. |
| C5 | Skipped = no updates | No active trade for skipped recs. |
| C6 | Volatility-aware selection | risk_tier + user preferred_risk filter/rank. |

### 5.4 Taken assets and new batches

- **Problem:** A new batch (or refresh) can create a **new pending recommendation** for an asset (e.g. Brent) that the user already **took** → duplicate rows (one taken, one pending).
- **Desired behaviour:**
  - When building the batch (or refresh), **exclude** any (asset_category, asset_id) that already has a recommendation with status = **taken** (has an active trade). Do not create a new pending line for that asset.
  - Surface new analysis for that asset in **Active Trades** only (e.g. refresh current_sl / current_tp, or “latest analysis” note). Update job can write last_updated so UI shows “Updated from latest pull.”

### 5.5 State model

- **Recommendation:** status ∈ { pending | taken | skipped }. taken → one ActiveTrade; skipped → no active trade.
- **ActiveTrade:** recommendation_id, user_taken_at, current_sl, current_tp, suggested_sl/tp, trade phases, expiry, last_updated, **outcome** / **outcome_at** / **closing_price** when closed; updated by the periodic job.

---

## 6. Tech stack (current)

- **App:** `local_trading_bot_v2` (Python **3.10+**, FastAPI, SQLite + aiosqlite, APScheduler).
- **Data:** RSS ingestion; sentiment (VADER + optional FinBERT); tagging + ticker detection + **bounded** Yahoo lookups for sentiment–price snapshot (mention count + cap; blocklist); recommendation engine (yfinance); outcomes & Platt calibration.
- **UI:** Static HTML/CSS — Dashboard, Recommendations (macro banner, calibration strip, context column), Research, Analysis, Active trades, **Journal**, Briefing; preferences include **risk budget** and LLM settings.
- **API:** Regime status hydrates from live market context; heavy batch work can run **off the asyncio loop** (`asyncio.to_thread`) so the UI stays responsive.

---

## 7. Legal / compliance

- All analysis and recommendations: **educational and research only**; not financial advice.
- Clear disclaimer on every user-facing output (API, UI).
- Data lineage: analysis and recommendations reference “as of” time and sources used.

---

## 8. Backlog / future refinements

### 8.1 Dynamic ticker blocklist & Yahoo noise

- **Done (hybrid):** In-code `_DEFAULT_BLOCKLIST` + optional **`data/blocklist.txt`**; `is_likely_ticker` / stricter extra-tag filter for price snapshots.
- **Done (analysis):** Sentiment–price Yahoo calls limited to tags with **≥2 mentions**, **top 120 by count** (`aggregate.py`) to avoid log spam and multi-minute analysis runs.
- **Ongoing:** Add symbols to `blocklist.txt` when new false positives appear in logs.

### 8.2 Indices as a recommendation category

- **Done:** **indices** category in `ASSETS_BY_CATEGORY` (e.g. SPX, DJI, NDX, FTSE, Nikkei, Kospi, … with `^` Yahoo symbols); up to **5** index ideas per batch alongside other categories. (Older note about “blocklist index symbols” referred to *headline* false positives, not the curated index universe.)

### 8.3 Taken trades vs new batch

- Same as §5.4: exclude (category, asset_id) with an existing **taken** rec when building a new batch; surface new data in Active Trades only.

### 8.4a Dexter inner workings (research summary)

**Dexter** (`../dexter`) is a TypeScript/Bun CLI agent. Understanding it clarifies what we emulate in Option B.

**Agent loop (simplified)**

1. **Input:** User query (natural language).
2. **System prompt:** SOUL.md (identity/philosophy) + tool descriptions + behavior + response format. Tools: financial_search, web_search, web_fetch, browser, skill, memory_*, heartbeat, read_file, write_file, edit_file.
3. **Iteration:** LLM is called with tools bound. If the model returns **tool_calls**, the executor runs them (with optional approval for write_file/edit_file), results are appended to a **scratchpad** (JSONL per query). Then a new prompt is built: original query + full tool results (Anthropic-style: no summarization). LLM is called again. Loop until the model responds **without** tool calls → that text is the final answer.
4. **Context management:** If token count exceeds a threshold, oldest tool results are dropped from the prompt (scratchpad stays on disk; only “active” results are sent). Optional memory flush writes a summary to `.dexter/memory/`.
5. **Limits:** Max iterations (e.g. 10), soft limits per tool (warnings to avoid retry loops).

**financial_search (meta-tool)**

- Single tool exposed to the main agent: “natural language query about financial data.”
- **Inside:** A dedicated LLM call with a **router** system prompt and a set of **finance tools** bound (get_stock_price, get_stock_prices, get_company_news, get_income_statements, get_key_ratios, get_cash_flow_statements, etc.). The router picks which tool(s) to call and with what args (ticker, dates, limit). Tools are executed (often in parallel), results combined, returned as one blob to the main agent.
- **Data source:** Financial Datasets API (`api.financialdatasets.ai`); each sub-tool calls an endpoint (e.g. `/prices/snapshot/`, `/news`, fundamentals). Responses are cached for immutable data.
- **Implication for emulation:** We don’t need the full meta-tool. We need: (1) **context** = our analysis (by_asset_sentiment, summary) + price (we have it) + optional extra (yfinance `.info` or one FD API call if we add a key). (2) **One LLM call** with a fixed prompt: “Given this context, write 2–4 sentences: why this idea might make sense and main risk.” No tool loop.

**Skills**

- **Skill** = SKILL.md file: YAML frontmatter (name, description) + markdown body (step-by-step instructions). Example: DCF skill says “Call financial_search with [TICKER] cash flow…”, “Extract FCF…”, “Calculate WACC…”.
- The **skill** tool just returns that markdown. The agent then “follows” it by calling other tools (e.g. financial_search). So skills are **prompt-like workflows** that the main loop executes.
- **Implication for emulation:** We can define “workflows” in Python (e.g. “rationale”: gather context → single LLM call → return text). No SKILL.md; same idea as a fixed pipeline.

**What we reuse conceptually (Option B)**

- **Single-shot “research”:** One prompt with rich context (analysis + sentiment + price ± fundamentals), one LLM call, short factual output. No iterative tool loop.
- **Context builder:** In the app we already have: analysis payload (by_asset_sentiment, summary), recommendation (asset_id, buy_in_price, risk_tier). We can add optional yfinance `.info` or one external API call for a bit more colour. That context is the input to the LLM.
- **System prompt:** Short, fixed: “You are a research assistant. Given the following context about a trade idea, write 2–4 sentences: why it might make sense and the main risk. Be concise and factual. Do not give generic advice.”

### 8.4 Leveraging Dexter: we proceed with Option B (emulate)

We **adopt Option B (emulate)** in the app: no dependency on running Dexter; one Python codebase; rationale and/or “Research this ticker” implemented with our own context + one LLM call.

**Implemented (Option B — shipped)**

1. **Rationale per recommendation (batch time)** — `rationale_text` on `Recommendation`; `app/services/research/` builds context + multi-provider LLM; batch job fills when provider configured.

2. **On-demand research** — `GET /api/research/summary?asset_id=...&category=...` with optional `generate=true`; **`GET /api/research/resolve?q=...`** for name→ticker; UI: Research page + per-row **Research** on Recommendations.

3. **Richer context** — `build_research_context` includes yfinance `.info` slices, technicals, regime notes where applicable.

**Option A (integrate Dexter)** remains in backlog: if we later want Dexter’s full depth (filings, DCF, multi-step planning), we can invoke Dexter as a service from the app instead of or in addition to the one-shot rationale.

---

## 9. Progress

### Phase 1 — Market analysis

| Item | Status | Notes |
|------|--------|--------|
| Ingestion (RSS + oil/commodity) | Done | `app/services/ingestion/` |
| Price snapshot in analysis | Done | `get_price_snapshot()` in engine; in analysis payload |
| Sentiment + asset tagging | Done | VADER + keywords + dynamic ticker detection |
| Market analysis output | Done | `MarketAnalysis` model; `GET /api/analysis/latest` |
| MCP tools (optional) | Not done | Optional |
| Cursor rules | Done | `.cursor/rules/` |

### Phase 2 — Trade recommendations

| Item | Status | Notes |
|------|--------|--------|
| Recommendation schema | Done | `Recommendation` model; full API schema |
| Engine (≤5 per category, sentiment + risk) | Done | `engine.py`; dynamic tickers + fixed universe; preferred_risk |
| Storage (recommendations + active trades) | Done | SQLite |
| One batch per day; update job for active only | Done | Guard in `job_daily_batch`; `job_update_active_trades` writes suggested_sl/suggested_tp + levels history; original SL/TP never overwritten |
| Took it / Skipped | Done | API + UI |
| Research / rationale (Option B) | Done | Context builder; multi-provider LLM; `rationale_text` on rec; API + UI: “Show research” modal per rec, “Research an asset” page (`/research`) |
| Indices category | Done | SPX, DJI, NDX, FTSE, VIX (^GSPC, ^DJI, ^IXIC, ^FTSE, ^VIX); ≤5 recs |
| Blocklist from file | Done | `data/blocklist.txt`; one symbol per line, # comments |
| **Practical Order: richer research context** | Done | yfinance `.info` (sector, industry, marketCap, shortName) in `build_research_context`; LLM prompt includes fundamentals |
| **Practical Order: company name → ticker** | Done | `resolve_company_to_ticker()` in ticker_detection; `GET /api/research/resolve?q=...`; Research page "Resolve" button |
| **Practical Order: optional daily briefing** | Done | Default checklist: SPX move, VIX, **WTI, Brent, gold, silver** futures, recs today; optional `data/briefing_checklist.json`; `GET/POST` briefing APIs; `/briefing` page |
| Export (CSV/table) | Not done | Optional |
| Claude Equity Research | Not done | Optional |
| UI / notifications | Partially done | Dashboard, pages; optional Telegram/Slack |

### Constraints status

| Constraint | Status |
|------------|--------|
| C1–C6 | Done |
| Taken assets: no duplicate rec (C7) | Done | Scheduler queries taken (category, asset_id); engine `exclude_taken` in ideas + backfill |

---

## 10. Suggested next steps (order with Option B in mind)

### 10.1 Next five priorities (backlog)

Consolidated from §10 below and `docs/FUTURE_RECOMMENDATION_IMPROVEMENTS.md` (quick wins + Sections 6–9). Order is **suggested**, not mandatory.

| # | Item | Scope / notes |
|---|------|----------------|
| 1 | ~~**Macro calendar forward-fill**~~ | **Done** — now **auto-fetched** from public sources (no API key): **FOMC** from Fed calendar HTML + press releases JSON; **CPI/NFP** pattern-based. Cached in `data/macro_calendar_cache.json` (7-day TTL). Static fallback if fetch fails. **API:** `GET /api/macro-calendar`, `POST /api/macro-calendar/refresh`. **UI:** `/calendar` page with status, upcoming events, refresh button. |
| 2 | **Export (CSV)** | `GET /api/recommendations/export?format=csv` for today’s batch or pending; optional download control on Recommendations UI. |
| 3 | **Calibrated confidence in API** | When Platt calibration is fitted, expose optional **`confidence_calibrated`** (or similar) on recommendation list/detail payloads so UI/API consumers see raw vs calibrated side by side. |
| 4 | **MCP tools** (optional) | Thin wrappers: e.g. `get_latest_insight`, `get_sentiment_summary`, `get_oil_snapshot` over existing HTTP/DB paths. |
| 5 | **Notifications** (optional) | Telegram / Slack (or webhook): e.g. daily batch ready, regime transition alerts, optional macro-day reminder. |

Further ideas (larger than a single sprint): **A/B or shadow engine** (log alternate thresholds without changing live picks — Section 9 in future doc); **historical replay / backtest** (Section 6); **higher-frequency bars** (Section 9).

---

1. ~~**Taken assets vs new batch**~~ — Done: scheduler passes `exclude_taken` (from DB); engine skips in ideas + backfill (§5.4).
2. ~~**Research / rationale (Option B — emulate Dexter)**~~ — Done: `app/services/research` (context + multi-provider LLM: OpenAI, Anthropic, Ollama, OpenRouter; config: `research_llm_provider`, `research_llm_model`, `*_api_key`, `ollama_base_url`), `rationale_text` on Recommendation, batch stores rationale when provider available, `GET /api/research/summary`, `GET /api/research/providers`.
3. ~~**Export (CSV)**~~ — Done: `GET /api/recommendations/export` returns CSV for today's batch. UI: "Export CSV" button on Recommendations page.
4. ~~**Active-trade update logic**~~ — Done: original SL/TP preserved; suggested_* + `active_trade_levels` history; API and UI show original, suggested, and per-run table with timestamps.
5. ~~**Blocklist from file**~~ — Done: `data/blocklist.txt` loaded at runtime; in-code defaults as fallback (§8.1).
6. ~~**Indices category**~~ — Done: SPX/DJI/NDX/FTSE/VIX in ASSETS_BY_CATEGORY; recommendations + Research an asset (§8.2).
7. ~~**Feed-item cleanup**~~ — Done: daily job at 04:00 deletes feed_items where fetched_at older than feed_max_age_days.
8. ~~**R:R Minimum Filter**~~ — Done: `MIN_RISK_REWARD = 1.5` in engine; candidates with R:R < 1.5 are skipped.
9. ~~**Webhook Notifications**~~ — Done: `app/services/notifications.py` sends alerts via Discord/Slack/generic webhook. Events: batch_ready, regime_change. API + UI on Dashboard.
10. ~~**Dynamic indicator weights**~~ — Done: `_regime_direction_weights()` and `_regime_tech_weights()` in `technicals.py`.
11. ~~**Regime-specific thresholds**~~ — Done: `_regime_rsi_thresholds()` and `_regime_stoch_thresholds()` use stricter OB/OS in `risk_off`.
12. ~~**Sector-specific regimes**~~ — Done: `detect_sector_regime()` and `detect_all_sector_regimes()` in `market_context.py`. Tracks tech, energy, financials, healthcare, consumer, industrials, materials, commodities via sector ETFs. API: `GET /api/market/sectors`. UI: Sector Regimes panel on Dashboard. **Engine integration:** `_sector_regime_adjustment()` adjusts confidence ±5-8 based on sector regime alignment with trade direction; relative strength vs SPX adds ±3.
13. ~~**Sentiment decay tuning**~~ — Done: `sentiment_half_life_hours` configurable via preferences (1-168h, default 24h). `_get_sentiment_half_life()` in `aggregate.py`. UI: input on Dashboard.
14. ~~**Correlation matrix UI**~~ — Done: `GET /api/market/correlations` returns 20-day rolling correlations for SPX, VIX, Gold, Oil, USD, 10Y Yield + divergence signals. UI: Correlation Matrix panel on Dashboard with color-coded table.
15. **MCP tools** (optional) — get_latest_insight, get_sentiment_summary, get_oil_snapshot.

---

### Engine v2 — ATR-based logic upgrade

| Item | Status | Notes |
|------|--------|--------|
| **ATR-based SL/TP** | Done | `app/services/technicals.py`: ATR multipliers by risk tier replace fixed %; falls back to fixed % when no price history |
| **Long/short direction** | Done | `determine_direction()` combines sentiment (30%) + technicals (70%: RSI, MACD, BB, ADX, Stochastic, OBV); `direction` field on Recommendation |
| **Expiry date estimation** | Done | Based on avg daily % move vs TP distance; clamped 1–30 days |
| **Technical indicators (core)** | Done | Pure pandas/numpy: ATR(14), RSI(14), MACD(12/26/9), Bollinger(20,2σ) |
| **Combined ranking score** | Done | `compute_technical_score()`: sentiment magnitude + mention count + RSI extremes + MACD + Bollinger + ADX strength + Stochastic + OBV |
| **Time-decay sentiment** | Done | `aggregate.py`: exponential decay (24h half-life) so recent headlines weigh more |
| **Price caching** | Done | `technicals.py`: OHLCV 90d cache per batch run; `clear_cache()` at batch start |
| **ATR-based trailing SL/TP** | Done | Active-trade update uses fresh `TechnicalSnapshot`; SL only moves favorably |
| **Volatility notes** | Done | Each rec stores ATR%, RSI, MACD, ADX, Stochastic, OBV, BB, S/R, trend bias |
| **Direction in API & research** | Done | All responses include `direction`; LLM prompt references direction + technicals |

### Engine v2.1 — Advanced indicators, S/R, confidence

| Item | Status | Notes |
|------|--------|--------|
| **ADX (Average Directional Index)** | Done | Trend strength (weak/moderate/strong); +DI vs -DI for direction. ADX > 25 = trending, > 40 = strong |
| **Stochastic Oscillator (%K/%D)** | Done | Overbought/oversold confirmation alongside RSI; 14-period K, 3-period D |
| **OBV (On-Balance Volume)** | Done | Volume confirms price direction; 5-day slope for momentum |
| **Support/Resistance detection** | Done | Swing high/low method (window=5); deduplication within 0.5%; sorted by proximity |
| **Fibonacci retracement levels** | Done | 50-bar lookback; 0%, 23.6%, 38.2%, 50%, 61.8%, 78.6%, 100% levels |
| **S/R-aware SL/TP placement** | Done | Long SL snaps below nearest support (with buffer); TP considers nearest resistance; vice versa for shorts |
| **Confidence score (0–100%)** | Done | Weighted: ADX trend 15%, RSI 15%, MACD 15%, Sentiment 15%, Stochastic 10%, OBV 10%, Bollinger 10%, Mentions 10% |
| **Risk:Reward ratio** | Done | Explicit R:R on each rec (e.g. 2.5 = reward is 2.5× the risk) |
| **Confidence + R:R in model** | Done | `confidence_score` (Integer), `risk_reward` (Float) columns on Recommendation |
| **Enhanced API responses** | Done | `_rec_to_item` includes confidence_score, risk_reward, direction, volatility_notes |

### Engine v3 — Professional trading methodology

| Item | Status | Notes |
|------|--------|--------|
| **Market regime detection** | Done | `app/services/market_context.py`: SPX SMA50/SMA200 trend + VIX level → risk_on / cautious / risk_off. Adjusts max recs, confidence threshold, direction bias |
| **Multi-timeframe trend alignment** | Done | `check_trend_alignment()`: daily EMA 20/50 trend vs weekly SMA 200 slope → aligned / conflicting. Aligned trades get +8 confidence and ranking priority |
| **EMA stack filter** | Done | EMA 20 > 50 > SMA 200 = bullish stack; reverse = bearish stack. Added to volatility_notes |
| **Confidence gating** | Done | Regime-dependent threshold: risk_on=35, cautious=45, risk_off=55. Low-conviction candidates dropped before ranking |
| **Regime-dependent rec count** | Done | risk_off caps at 3/category (vs 5). Fewer recommendations = fewer opportunities to get wrong |
| **Cross-asset correlation** | Done | Gold/USD, SPX/VIX, Gold/SPX 5-day return divergence detection. Warnings reduce confidence by -5 |
| **Direction override (risk_off)** | Done | In risk_off regime, stock/index longs are flipped to short to match market conditions |
| **Trade lifecycle phases** | Done | open → partial_tp (at 1R profit) → trailing (SL to breakeven) → closed_sl/closed_tp |
| **Breakeven stop logic** | Done | At 1R profit, SL moves to entry + 5% buffer. Eliminates downside on remaining position |
| **Extreme price tracking** | Done | `highest_price` / `lowest_price` on ActiveTrade for trailing reference |
| **Market context API** | Done | `GET /api/market/context`: regime, VIX, SPX trend, correlations, direction bias, thresholds |
| **LLM prompt upgrade** | Done | Rationale now references market regime and multi-timeframe alignment |

### Engine v3.1 — Ops, UI transparency, calibration

| Item | Status | Notes |
|------|--------|--------|
| **Non-blocking batch build** | Done | `build_daily_candidates` runs in `asyncio.to_thread` so HTTP stays live during heavy work |
| **Regime API hydration** | Done | `GET /api/regime/status` refreshes from `build_market_context` + `detect_regime_transition`; avoids stuck “unknown” before active-trade job |
| **Recommendations API** | Done | Grouped list uses per-row price snapshot in a thread; `macro_overlay` on `GET /api/recommendations` |
| **Survivor conflict note** | Done | `volatility_notes` can include `indicator conflict penalty: N (skip if ≥threshold)` |
| **Calibration & journal** | Done | Dashboard Platt/Brier/ECE/buckets; `GET /api/calibration`; Journal page for closed trades |

---

## 11. One-line summary

Phase 1–2 and **Engine v3** (+ sizing, risk budget, outcomes, calibration, journal, briefing commodities, macro overlay, log/Yahoo spam limits) are **done**. **Next:** see **§10.1** (CSV export, calibrated confidence field, MCP, notifications; macro calendar maintained through 2027).
