# Trading App — Plan & Progress

**Single source of truth** for the trading insight app: purpose, architecture, spec, flows, constraints, backlog, and progress.

**Quick links:** [1 Purpose](#1-purpose--context) · [2 Architecture](#2-architecture) · [3 Phase 1](#3-phase-1--market-analysis-spec) · [4 Phase 2](#4-phase-2--trade-recommendations-spec) · [5 Flows](#5-flows-and-constraints) · [6 Tech](#6-tech-stack-current) · [7 Legal](#7-legal--compliance) · [8 Backlog](#8-backlog--future-refinements) · [8.4a Dexter research](#84a-dexter-inner-workings-research-summary) · [8.4 Option B](#84-leveraging-dexter-we-proceed-with-option-b-emulate) · [9 Progress](#9-progress) · [10 Next steps](#10-suggested-next-steps-order-with-option-b-in-mind)

---

## 1. Purpose & context

- **Purpose:** Active trading support for oil/gold/gas (and later stocks, commodities, forex, indices) using market news, sentiment, and market data. Two deliverables: (1) **Market analysis**, (2) **Structured trade recommendations** with a **user feedback loop** (taken vs skipped → updates only for taken trades).
- **Principles:** At most **5 recommendations per day**; don’t overwhelm the user; favor **conservative / lower-volatility** when the user prefers less risk.
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
│  PHASE 2: TRADE RECOMMENDATIONS (≤5 per day, volatility-aware)               │
│  Recommendation Engine → daily batch (≤5) → User: Took it / Skipped         │
│  Took it → Active trade (updates SL/TP). Skipped → no updates.               │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Phase 1 — Market analysis (spec)

- **Goal:** One coherent **market analysis** (news + sentiment + price/conditions) as input for Phase 2.
- **Pipeline:** Ingest (RSS, price, conditions) → Enrich (sentiment, asset tags) → Aggregate → Output (report + “last analysis” artifact).
- **Analysis output:** `as_of`, `sources_used`, `summary`, `payload` (by_category, by_asset_sentiment, price_snapshot).
- **Deliverables:** Ingestion, sentiment + tagging, market analysis output, optional MCP tools, Cursor rules.

---

## 4. Phase 2 — Trade recommendations (spec)

- **Goal:** At most **5 structured recommendations per day** from analysis; user marks Took it / Skipped; updates only for taken trades.
- **Recommendation schema:** `id`, `created_at`, `batch_id`, `asset_category`, `asset_id`, `buy_in_price`, `stop_loss`, `take_profit`, `expiry_date`, `rationale_ref`, `risk_tier`, `status` (pending | taken | skipped). Optional (Option B): `rationale_text` (LLM-generated short “why and risk”).
- **Asset categories:** stocks, commodities, other (forex/crypto). Backlog: **indices** (§8.2).
- **Daily cap:** One batch per day (batch_id = calendar day); at most 5 recs per day; engine ranks by sentiment + risk preference.
- **User feedback:** Took it → create **ActiveTrade** (recommendation_id, user_taken_at, current_sl, current_tp, expiry, last_updated). Skipped → status = skipped; no active trade.
- **Scheduling:** Daily batch at configured time; update job (e.g. every 2h) only refreshes **active trades**.
- **Risk tier:** conservative | moderate | aggressive; user preference (exposed in UI) filters/ranks candidates.

---

## 5. Flows and constraints

### 5.1 Design principles

| Principle | Implication |
|-----------|-------------|
| Don’t assault the user | At most 5 recommendations per day. |
| Continue with the same set | Same batch until user responds; no new batch same day. |
| Respect risk preference | Tag risk_tier; favor conservative when user prefers. |
| Updates only for taken trades | Took it → active trade gets SL/TP updates. Skipped → no updates. |

### 5.2 When to generate vs update

| Condition | Action |
|-----------|--------|
| Same day, batch already exists | Do **not** create new batch. Run **update job** for active trades only. |
| New calendar day | Create **one new batch** of ≤5 (batch_id = today). |
| User marks Took it | Create/update **active trade**; include in update job. |
| User marks Skipped | status = skipped; no active trade; no future updates. |
| Update job (e.g. every 2h) | Only **modifies active trades** (SL/TP, validity). Does not create new recommendations. |

### 5.3 Constraints (C1–C6)

| # | Constraint | Note |
|---|------------|------|
| C1 | Max 5 recommendations per day | Engine returns at most 5 per batch. |
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
- **ActiveTrade:** recommendation_id, user_taken_at, current_sl, current_tp, expiry, last_updated; updated by the periodic update job.

---

## 6. Tech stack (current)

- **App:** `local_trading_bot_v2` (Python, FastAPI, SQLite, APScheduler).
- **Data:** RSS ingestion, sentiment (VADER), asset tagging + dynamic ticker detection, market analysis aggregation, recommendation engine (yfinance for prices).
- **UI:** Static HTML/CSS, dashboard, Recommendations / Analysis / Active trades pages, preferences (risk stance).

---

## 7. Legal / compliance

- All analysis and recommendations: **educational and research only**; not financial advice.
- Clear disclaimer on every user-facing output (API, UI).
- Data lineage: analysis and recommendations reference “as of” time and sources used.

---

## 8. Backlog / future refinements

### 8.1 Dynamic ticker blocklist maintenance

- **Problem:** Dynamic ticker detection produces false positives; we edit code (BLOCKLIST in ticker_detection.py) when new “possibly delisted” symbols appear in logs.
- **Backlog:** Move blocklist to `data/blocklist.txt` (or script that suggests entries from logs). Load at runtime with in-code defaults as fallback.

### 8.2 Indices as a recommendation category

- **Context:** Users can trade indices (S&P 500, Dow, FTSE, VIX, etc.). We currently blocklist index symbols to avoid yfinance noise.
- **Backlog:** Add **indices** category; support **up to 5 indices recommendations** per day. Map tags (SPX, DJI, FTSE, VIX) to yfinance symbols (e.g. ^GSPC, ^DJI, ^FTSE); add to tradeable universe; rank by sentiment; cap at 5.

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

**Planned work (Option B)**

1. **Rationale per recommendation (optional at batch time)**  
   When building the daily batch, for each recommended asset we can call an LLM with context (analysis summary, by_asset_sentiment for that asset, buy_in_price, risk_tier). Prompt: one short paragraph “why this idea and main risk.” Store in a new field (e.g. `rationale_text`) or in a separate table keyed by recommendation_id. Show in Recommendations UI. Requires: LLM API key in app config; context builder; one-shot prompt; schema migration if we store on Recommendation.

2. **“Research this ticker” endpoint**  
   `GET /api/research/{asset_id}` (and optional category): gather same context (latest analysis, sentiment for that asset, current price; optional: yfinance `.info` or Financial Datasets snapshot if we add a key). Single LLM call with the same style of prompt. Return `{ "summary": "…" }`. No storage; on-demand. Enables a “Research” button per row in the UI.

3. **Richer context (optional later)**  
   Add a small **research context** module: given (asset_id, category), return a dict: sentiment, price, optional fundamentals (yfinance `ticker.info` or one FD API call). Keeps the LLM prompt factual without reimplementing Dexter’s tool loop.

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
| One batch per day; update job for active only | Done | Guard in `job_daily_batch`; `job_update_active_trades` |
| Took it / Skipped | Done | API + UI |
| Research / rationale (Option B) | Done | Context builder; multi-provider LLM (OpenAI, Anthropic, Ollama, OpenRouter); `rationale_text` on rec; `GET /api/research/summary`, `GET /api/research/providers` |
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

1. ~~**Taken assets vs new batch**~~ — Done: scheduler passes `exclude_taken` (from DB); engine skips in ideas + backfill (§5.4).
2. ~~**Research / rationale (Option B — emulate Dexter)**~~ — Done: `app/services/research` (context + multi-provider LLM: OpenAI, Anthropic, Ollama, OpenRouter; config: `research_llm_provider`, `research_llm_model`, `*_api_key`, `ollama_base_url`), `rationale_text` on Recommendation, batch stores rationale when provider available, `GET /api/research/summary`, `GET /api/research/providers`.
3. **Export (CSV)** — `GET /api/recommendations/export?format=csv` for today’s batch or pending.
4. **Active-trade update logic** — In `job_update_active_trades`, refresh current_sl / current_tp from latest prices or analysis.
5. **Blocklist from file** — `data/blocklist.txt` or log-parsing script (§8.1).
6. **Indices category** — Up to 5 indices recs; map SPX/DJI/FTSE/VIX to yfinance (§8.2).
7. **Feed-item cleanup** — Daily job to delete feed_items older than feed_max_age_days.
8. **MCP tools** (optional) — get_latest_insight, get_sentiment_summary, get_oil_snapshot.
9. **UI / notifications** (optional) — Telegram/Slack, etc.

---

## 11. One-line summary

Phase 1 and Phase 2 core are done; **taken-asset filter** and **Research/rationale (Option B)** are done. Next: CSV export, active-trade update logic, blocklist file, indices, and optional items.
