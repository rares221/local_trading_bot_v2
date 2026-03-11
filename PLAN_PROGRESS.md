# Plan progress — TRADING_APP_PLAN vs local_trading_bot_v2

Maps [TRADING_APP_PLAN.md](../TRADING_APP_PLAN.md) and [PLAN_MODE.md](../PLAN_MODE.md) to what’s done and what’s next.

---

## Phase 1 — Market analysis

| Plan item | Status | Notes |
|-----------|--------|--------|
| Ingestion for news (RSS + oil/commodity) | Done | `app/services/ingestion/`: feeds from codeops + commodities (EIA, Investing.com energy). |
| Price and conditions in ingestion | Done | Prices in **market analysis** payload via `get_price_snapshot()` (WTI, Brent, XAUUSD, XOM, Henry Hub). Also used in recommendation engine. |
| Sentiment enrichment and asset tagging | Done | VADER + asset keywords in `app/services/sentiment/`; run in `job_ingest_and_sentiment`. |
| Market analysis output (schema + last artifact) | Done | `MarketAnalysis` model; `run_analysis` aggregates feed items; `GET /api/analysis/latest`. |
| MCP tools (optional) | Not done | `get_latest_insight`, `get_sentiment_summary`, `get_oil_snapshot` for Cursor — optional. |
| Cursor rules | Done | `.cursor/rules/`: disclaimer, data lineage, architecture. |

---

## Phase 2 — Trade recommendations

| Plan item | Status | Notes |
|-----------|--------|--------|
| Recommendation schema (date, hour, buy, SL, TP, expiry, risk_tier) | Done | `Recommendation` model; API returns full schema. |
| Recommendation engine: 5 stocks + 5 commodities + 5 other/day, volatility-aware | Done | `engine.py`: ASSETS_BY_CATEGORY; up to 5 per category. API: `?asset_category=`, `?group_by=category` for recommendation page. |
| Storage for recommendations and active trades | Done | SQLite; `recommendations`, `active_trades` tables. |
| One batch per day; update job only for active trades | Done | Guard in `job_daily_batch`; `job_update_active_trades` only touches active trades. |
| User feedback: Took it → active trade; Skipped → no updates | Done | `POST /api/recommendations/{id}/took-it` and `.../skipped`; active trades list. |
| Export format (CSV/table) | Not done | API returns JSON; CSV/table export for “date, hour, buy, SL, TP, expiry” — optional. |
| Integrate claude-equity-research for stocks | Not done | Optional; see TECH_STACK_AND_INTEGRATION.md. |
| UI / notifications | Not done | Optional; dashboard, Telegram/Slack. |

---

## PLAN_MODE behaviour

| Constraint | Status |
|------------|--------|
| Max 5 recommendations per day | Done |
| One batch per day; no new batch same day | Done |
| Updates only for active trades (taken) | Done |
| Skipped = no updates | Done |
| Risk tier (conservative/moderate/aggressive) | Done |
| User can mark Took it / Skipped | Done |

---

## Suggested next steps (in order)

1. ~~**Price/conditions in market analysis**~~ — Add current oil/commodity (and optional stock) prices into the analysis report payload so the “last analysis” includes price context and the engine can use it later (e.g. for volatility or filtering).  
2. **Export (CSV)** — Add `GET /api/recommendations/export?format=csv` (or similar) returning `date,hour,buy_in_price,stop_loss,take_profit,expiry_date` for today’s batch (or all pending).  
3. **Active-trade update logic** — In `job_update_active_trades`, optionally refresh `current_sl` / `current_tp` from latest prices (e.g. re-fetch and re-apply the same % rules, or leave as-is for now).  
4. **Feed-item cleanup** — Add a daily job to delete `feed_items` older than `feed_max_age_days`.  
5. **MCP tools** (optional) — Small MCP server exposing `get_latest_insight`, `get_sentiment_summary`, `get_oil_snapshot` for Cursor.  
6. **Claude Equity Research** (optional) — For stocks only; Anthropic API or subprocess.  
7. **UI or notifications** (optional) — Simple dashboard or Telegram/Slack for new recommendations and active-trade updates.

---

## One-line summary

Phase 1 and Phase 2 core are done (ingestion, sentiment, analysis with price snapshot, ≤5/day recommendations with real prices, Took it/Skipped, active trades, scheduler). Next: CSV export, then optional items (update logic, cleanup, MCP, Claude, UI).
