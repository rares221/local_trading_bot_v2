# Trading App — Single Plan (Purpose, Spec, Flows, Progress)

**Single source of truth** for the trading insight app: purpose, architecture, Phase 1 & 2 spec, flows, constraints, backlog, and progress. Replaces the need to track TRADING_APP_PLAN.md and PLAN_MODE.md separately.

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
- **Recommendation schema (reference):** Analysis has `as_of`, `sources_used`, `summary`, `payload` (by_category, by_asset_sentiment, price_snapshot).
- **Deliverables:** Ingestion, sentiment + tagging, market analysis output, optional MCP tools, Cursor rules.

---

## 4. Phase 2 — Trade recommendations (spec)

- **Goal:** At most **5 structured recommendations per day** from analysis; user marks Took it / Skipped; updates only for taken trades.
- **Recommendation schema:** `id`, `created_at`, `batch_id`, `asset_category`, `asset_id`, `buy_in_price`, `stop_loss`, `take_profit`, `expiry_date`, `rationale_ref`, `risk_tier`, `status` (pending | taken | skipped).
- **Asset categories:** stocks, commodities, other (forex/crypto); backlog: **indices** (see §9).
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

- See §5.4: filter out (category, asset_id) with existing **taken** recommendation when generating new batch; push new data only into Active Trades.

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
| Export (CSV/table) | Not done | Optional |
| Claude Equity Research | Not done | Optional |
| UI / notifications | Partially done | Dashboard, pages; optional Telegram/Slack |

### Constraints status

| Constraint | Status |
|------------|--------|
| C1–C6 | Done |
| Taken assets: no duplicate rec (C7) | Not done (see §5.4, §8.3) |

---

## 10. Suggested next steps (in order)

1. **Taken assets vs new batch** — Do not create a new recommendation for an asset that already has a taken trade; surface new data in Active Trades only (§5.4, §8.3).
2. **Export (CSV)** — `GET /api/recommendations/export?format=csv` for today’s batch or pending.
3. **Active-trade update logic** — In `job_update_active_trades`, refresh current_sl / current_tp from latest prices or analysis.
4. **Feed-item cleanup** — Daily job to delete feed_items older than feed_max_age_days.
5. **Blocklist from file** — Move ticker blocklist to data/blocklist.txt or script from logs (§8.1).
6. **Indices category** — Up to 5 indices recommendations; map FTSE/DJI/VIX etc. (§8.2).
7. **MCP tools** (optional) — get_latest_insight, get_sentiment_summary, get_oil_snapshot.
8. **Claude Equity Research** (optional) — For stocks.
9. **UI / notifications** (optional) — Telegram/Slack, etc.

---

## 11. One-line summary

Phase 1 and Phase 2 core are done (ingestion, sentiment, analysis, ≤5/day recommendations with dynamic tickers and risk preference, Took it/Skipped, active trades). Next: taken-asset filter so no duplicate rec for same asset, then CSV export, blocklist file, indices category, and optional items.
