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

### Docker (recommended)

Same pattern as codeops-trading-app: start/stop via Makefile and Docker Compose.

```bash
make build   # first time or after dependency/code changes
make start   # start app in background
```

- **Dashboard:** http://localhost:8000/dashboard (buttons: run pipeline, refresh today’s batch, clear today’s batch, link to recommendations)
- **Recommendations:** http://localhost:8000/recommendations

```bash
make stop    # stop and remove the container
```

**Docker required:** Install [Docker Desktop for Windows](https://docs.docker.com/desktop/install/windows-install/) and have it running.

**Make `docker` work in the terminal (Git Bash, Cursor, PowerShell):**  
Add Docker’s folder to your **user** PATH so every new terminal finds it:

1. Press **Win**, type **environment**, open **Edit the system environment variables**.
2. Click **Environment Variables**.
3. Under **User variables**, select **Path** → **Edit**.
4. Click **New** and add:  
   `C:\Program Files\Docker\Docker\resources\bin`
5. Click **OK** on all dialogs.
6. **Close and reopen** your terminal (and Cursor if you use its integrated terminal).  
   Then run: `docker --version` and `docker compose version`.

If Docker was installed to a different drive or folder, use that path instead (the folder must contain `docker.exe`).

**If you don’t want to change PATH:**  
Double‑click **start.bat** / **stop.bat** in the project folder; they call Docker by full path.

**From a terminal (once `docker` is on PATH):**  
`docker compose build` and `docker compose up -d --remove-orphans` to start, `docker compose down` to stop (or `make build` / `make start` / `make stop` if you have make).

Data (SQLite DB) is stored in a Docker volume `trading_data` and persists across stop/start.

### Local (venv)

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- **Health:** `GET http://localhost:8000/api/health`
- **Latest analysis:** `GET http://localhost:8000/api/analysis/latest`
- **Recommendations:** `GET http://localhost:8000/api/recommendations` (optional: `?asset_category=stocks`, `?group_by=category`)
- **Mark taken:** `POST http://localhost:8000/api/recommendations/{id}/took-it`
- **Mark skipped:** `POST http://localhost:8000/api/recommendations/{id}/skipped`
- **Active trades:** `GET http://localhost:8000/api/active-trades`

The scheduler runs with the API (ingestion + analysis every 2h, daily batch at 06:00, active-trade updates every 2h).

**Populate data once (no scheduler):** run from the dashboard (“Run pipeline once”) or:

```bash
python scripts/run_pipeline_once.py
```

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
