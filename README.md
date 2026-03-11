# local_trading_bot_v2

Trading insight app: **market analysis** (Phase 1) and **trade recommendations** (Phase 2, ≤5/day, volatility-aware) with user feedback (Took it / Skipped) and active-trade updates.

**Specs:** [TRADING_APP_PLAN.md](../TRADING_APP_PLAN.md) and [PLAN_MODE.md](../PLAN_MODE.md).  
**Stack:** [TECH_STACK_AND_INTEGRATION.md](TECH_STACK_AND_INTEGRATION.md).  
**Plan vs progress:** [PLAN_PROGRESS.md](PLAN_PROGRESS.md).

## Reuse from codeops-trading-app

| From codeops-trading-rss-ingester | Reused in local_trading_bot_v2 |
|-----------------------------------|---------------------------------|
| **Feed list** (`internal/feed/feed.go` DefaultFeeds) | **100%** — All 12 URLs and 4 categories copied into `app/services/ingestion/feeds.py`; we added 2 feeds for `commodities` (EIA, Investing.com energy). |
| **Feed item shape** (`internal/storage` FeedItem) | **100%** — Same fields (id, title, description, content, link, published, category, source, fetched_at, keywords, is_processed) in `app/models/feed_item.py`, plus `sentiment_score` and `asset_tags`. |
| **Categories** | Same 4 (finance, politics, trading, crypto) + `commodities`. |
| **Max age** | Same idea: 30 days retention (`feed_max_age_days` in config); Go had `CleanupOldItems`, we can add a DB cleanup job. |
| **Fetch → parse → save** | Same flow; we use `feedparser` (Python) instead of `gofeed` (Go), and save to SQLite instead of one JSON file per item. |
| **Filename sanitization** | Logic translated; we use a single-pass sanitize for item IDs (the Go version had a bug we avoided). |
| **File-based storage, status.json, category dirs** | Not reused — we use a single DB; no status.json or per-category directories. |
| **Cron (6h fetch, 02:00 cleanup)** | Different schedule — we use APScheduler (e.g. every 2h) and no separate cleanup job yet. |

**Shell:** Prefer **bash** for commands in this project (Git Bash or WSL on Windows). PowerShell works but uses different syntax (e.g. `;` instead of `&&`).

## Git and GitHub (push to rares221)

The project is already a local git repo (`.git` present). To push to your GitHub:

1. **Create a new private repo** at [github.com/rares221](https://github.com/rares221): click **New**, name it e.g. `local_trading_bot_v2`, set to **Private**, do **not** add a README or .gitignore (you already have them).
2. **Add the remote** (if not already added):
   ```bash
   cd local_trading_bot_v2
   git remote add origin https://github.com/rares221/local_trading_bot_v2.git
   ```
   If you used a different repo name, replace `local_trading_bot_v2` in the URL.
3. **Commit** (if you haven’t yet):
   ```bash
   git add .
   git commit -m "initial commit"
   ```
4. **Push**:
   ```bash
   git branch -M main
   git push -u origin main
   ```

## Setup

```bash
cd local_trading_bot_v2
python -m venv .venv
source .venv/bin/activate   # Unix / Git Bash
# .venv\Scripts\activate   # Windows PowerShell
pip install -r requirements.txt
```

Optional: copy `.env.example` to `.env` and set `DATABASE_URL`, `DEBUG`, etc.

## Run

```bash
# API only (no scheduler)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- **Health:** `GET http://localhost:8000/api/health`
- **Latest analysis:** `GET http://localhost:8000/api/analysis/latest`
- **Recommendations:** `GET http://localhost:8000/api/recommendations` (optional: `?asset_category=stocks`, `?group_by=category` for stocks/commodities/other sections)
- **Mark taken:** `POST http://localhost:8000/api/recommendations/{id}/took-it`
- **Mark skipped:** `POST http://localhost:8000/api/recommendations/{id}/skipped`
- **Active trades:** `GET http://localhost:8000/api/active-trades`

To run the scheduler (ingestion, analysis, daily batch, active-trade updates), call `start_scheduler()` from a worker or after app startup (see `app/jobs/scheduler.py`).

**Populate data once (no scheduler):**
```bash
python scripts/run_pipeline_once.py
```
Then open `/api/analysis/latest` and `/api/recommendations`; try `POST /api/recommendations/{id}/took-it` or `.../skipped`.

## What's next (proceed from here)

| Step | Status | Action |
|------|--------|--------|
| Pipeline runs once | Done | `python scripts/run_pipeline_once.py` |
| Scheduler on startup | Done | Scheduler starts with the API (ingestion + analysis every 2h, daily batch at 06:00, active-trade updates every 2h). |
| **Recommendation engine with real prices** | Done | `app/services/recommendations/engine.py` fetches prices via yfinance (CL=F, BZ=F, GC=F, XOM, NG=F), sets buy_in_price = last close, and computes stop_loss/take_profit by risk_tier (conservative: 1.5% SL / 3% TP; moderate: 2% / 4%; aggressive: 3% / 6%). Daily batch uses this; re-run pipeline (new day or after clearing today’s batch) to see real numbers. |
| Price/conditions in analysis | Later | Add oil/commodity prices and optional EIA conditions into the market analysis payload so the engine can use them. |
| Feed-item cleanup | Optional | Add a job to delete `feed_items` older than `feed_max_age_days` (e.g. daily). |

## Data

- SQLite DB: `./data/trading.db` (created on first run).
- Tables: `feed_items`, `market_analyses`, `recommendations`, `active_trades`.

## Disclaimer

All analysis and recommendations are for **educational and research purposes only**. Not financial advice. See API responses and [TRADING_APP_PLAN.md](../TRADING_APP_PLAN.md) §8.
