# local_trading_bot_v2

Trading insight app: **market analysis** (Phase 1) and **trade recommendations** (Phase 2, ≤5/day, volatility-aware) with user feedback (Took it / Skipped), active-trade updates, research rationales (LLM), and an optional daily briefing.

**Specs:** [TRADING_APP_PLAN.md](../TRADING_APP_PLAN.md) and [PLAN_MODE.md](../PLAN_MODE.md).  
**Stack:** [TECH_STACK_AND_INTEGRATION.md](TECH_STACK_AND_INTEGRATION.md).  
**Plan vs progress:** [PLAN_PROGRESS.md](PLAN_PROGRESS.md).

---

## Prerequisites

- **Python 3.10+** (for type hints and async)
- **pip** and a **virtual environment** (recommended)
- **Network access** for RSS feeds and yfinance; optional API keys for cloud LLM providers
- **Optional:** [Docker](https://docs.docker.com/get-docker/) and Docker Compose for running in a container
- **Optional (local LLM):** [Ollama](https://ollama.ai/) running (e.g. `ollama run llama3.1`) if you use `RESEARCH_LLM_PROVIDER=ollama`

---

## Setup

```bash
cd local_trading_bot_v2
python -m venv .venv
# Activate the venv:
#   Windows (PowerShell):  .venv\Scripts\Activate.ps1
#   Windows (cmd):        .venv\Scripts\activate.bat
#   Unix / Git Bash:      source .venv/bin/activate
pip install -r requirements.txt
```

**Environment (optional)**  
Create a `.env` file in the project root to override defaults. The app loads it via `pydantic-settings`.

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | SQLite path (or other async URL) | `sqlite+aiosqlite:///./data/trading.db` |
| `DEBUG` | Enable SQL echo / debug | `false` |
| `DATA_DIR` | Directory for DB, preferences, blocklist, briefing checklist | `./data` |
| `PREFERRED_RISK` | Risk filter for recommendations | `moderate` |
| `DAILY_BATCH_HOUR` | Hour (0–23) for daily recommendation batch | `6` |
| `ANALYSIS_RUN_INTERVAL_HOURS` | Ingestion + analysis interval | `2` |
| `UPDATE_ACTIVE_TRADES_INTERVAL_HOURS` | Active-trade SL/TP refresh interval | `2` |
| `FEED_MAX_AGE_DAYS` | Delete feed items older than this | `30` |
| Research LLM (see below) | `RESEARCH_LLM_PROVIDER`, `RESEARCH_LLM_MODEL`, `*_API_KEY`, `OLLAMA_BASE_URL` | — |

On first run, the app creates the `data` directory (if missing), the SQLite database, and tables.

---

## Run

### Option A: Local (venv)

```bash
# From project root, with venv activated
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- **Root:** http://localhost:8000/ (links to dashboard, pages, API docs)
- **Dashboard:** http://localhost:8000/dashboard (preferences, actions, LLM settings, links)
- **Recommendations:** http://localhost:8000/recommendations
- **Research an asset:** http://localhost:8000/research (asset/category + “Resolve” company name → ticker)
- **Market analysis:** http://localhost:8000/analysis
- **Active trades:** http://localhost:8000/active-trades
- **Daily briefing:** http://localhost:8000/briefing
- **API docs:** http://localhost:8000/docs

The **scheduler** runs with the app: ingestion + analysis every 2h, daily batch at 06:00, active-trade updates every 2h, daily briefing once per day (after batch hour), feed cleanup at 04:00.

### Option B: Docker

```bash
make build   # first time or after dependency/code changes
make start   # start app in background
```

- Same URLs as above; ensure port 8000 is mapped (see `docker-compose.yml` or Makefile).
- Data (SQLite, `data/`) is typically in a volume so it persists across stop/start.

```bash
make stop    # stop and remove the container
```

**Docker on Windows:** Add Docker’s `bin` folder to your user PATH (e.g. `C:\Program Files\Docker\Docker\resources\bin`) so `docker` and `docker compose` work in the terminal. Alternatively use the project’s `start.bat` / `stop.bat` if they call Docker by full path.

---

## API overview

| Endpoint | Description |
|----------|-------------|
| `GET /api/health` | Health check |
| `GET /api/analysis/latest` | Latest market analysis (summary + payload) |
| `GET /api/recommendations` | List recommendations (optional: `?batch_id`, `?status`, `?asset_category`, `?group_by=category`) |
| `POST /api/recommendations/{id}/took-it` | Mark recommendation as taken (creates active trade) |
| `POST /api/recommendations/{id}/skipped` | Mark recommendation as skipped |
| `GET /api/active-trades` | List active trades (with original/suggested SL/TP and levels history) |
| `GET /api/research/providers` | List LLM providers and current research config |
| `GET /api/research/resolve?q=...` | Resolve company name to ticker(s) (e.g. `?q=Apple` → AAPL) |
| `GET /api/research/summary?asset_id=...&category=...&generate=true` | Research context + optional LLM rationale |
| `GET /api/briefing/latest` | Most recent daily briefing (404 if none) |
| `GET /api/briefing/checklist` | Current briefing checklist config |
| `POST /api/briefing/run` | Run briefing checklist now and return new briefing |
| `GET /api/preferences` | User preferences (risk, LLM provider/model) |
| `PUT /api/preferences` | Update preferences (JSON body) |
| `POST /api/actions/run-pipeline-once` | Run ingest + analysis + daily batch once |
| `POST /api/actions/refresh-todays-batch` | Rebuild today’s recommendation batch |
| `POST /api/actions/clear-todays-batch` | Clear today’s pending recommendations |
| `POST /api/actions/purge-all` | Delete all recommendations and active trades (testing) |

All API responses include a `disclaimer` field where applicable.

---

## Research rationale (multi-provider LLM)

Short “why this idea and main risk” rationales use a configurable LLM. **Providers:** OpenAI, Anthropic, Ollama (local), OpenRouter.

| Env / config | Description |
|--------------|-------------|
| `RESEARCH_LLM_PROVIDER` | `openai` (default), `anthropic`, `ollama`, or `openrouter` |
| `RESEARCH_LLM_MODEL` | Model name; empty = provider default (e.g. `gpt-4o-mini`, `llama3.1`) |
| `OPENAI_API_KEY` | Required when provider is `openai` |
| `ANTHROPIC_API_KEY` | Required when provider is `anthropic` |
| `OPENROUTER_API_KEY` | Required when provider is `openrouter`; use model IDs like `openai/gpt-4o-mini` |
| `OLLAMA_BASE_URL` | Optional; default `http://localhost:11434` for Ollama |

- **Ollama (local):** Set `RESEARCH_LLM_PROVIDER=ollama`, optionally `RESEARCH_LLM_MODEL=llama3.1`. No API key; run Ollama and pull the model.
- **OpenAI / Anthropic / OpenRouter:** Set the matching `*_API_KEY` and `RESEARCH_LLM_PROVIDER`.

Provider and model (and Ollama URL) can also be changed from the **Dashboard** (“Research / LLM provider”); values are stored in `data/preferences.json` and override env.

If no provider is configured or the key is missing, rationale generation is skipped; context and recommendations still work, and `rationale_text` / summary will be null.

---

## Daily briefing

Optional “heartbeat-style” checklist runs **once per day** (scheduled after the daily batch) and can be run manually from the **Briefing** page or via `POST /api/briefing/run`.

**Default checklist:** S&P 500 move (highlight if |change| ≥ 1.5%), VIX level (highlight if above 20), WTI oil price, recommendations count for today.

To customize, create `data/briefing_checklist.json` with a JSON array of items (see `app/services/briefing.py` for `type`: `index_move`, `level`, `price`, `recs_count` and their fields). Results are stored in the `daily_briefings` table and returned by `GET /api/briefing/latest`.

---

## Populate data (without waiting for scheduler)

From the Dashboard click **“Run pipeline once”**, or from the project root:

```bash
python scripts/run_pipeline_once.py
```

This runs: ingest RSS + sentiment → market analysis → daily recommendation batch (if none for today). Then check `/api/analysis/latest` and `/api/recommendations`.

Other scripts (from project root):

- `python scripts/refresh_todays_batch.py` — Rebuild today’s batch (e.g. after changing risk preference).

---

## Testing / sanity check

- **Health:** `curl http://localhost:8000/api/health`
- **Run pipeline once:** use Dashboard or `python scripts/run_pipeline_once.py`, then open Dashboard → Recommendations and Analysis.
- **Research:** open http://localhost:8000/research, type e.g. `Apple`, click **Resolve**, then **Get research** (LLM summary requires a configured provider).
- **Briefing:** open http://localhost:8000/briefing and click **Run briefing now** (uses live yfinance data and today’s rec count).

There are no automated test suites in the repo yet; the above manual checks are the current validation.

---

## Data and files

| Path / location | Description |
|-----------------|-------------|
| `data/trading.db` | SQLite database (created on first run) |
| `data/preferences.json` | User preferences (risk, LLM provider/model, Ollama URL); created when saved from UI |
| `data/blocklist.txt` | Optional; one symbol per line (2–5 letters), `#` = comment; used to exclude tokens from ticker detection |
| `data/briefing_checklist.json` | Optional; custom daily briefing checklist (JSON array); see `app/services/briefing.py` |

**Main tables:** `feed_items`, `market_analyses`, `recommendations`, `active_trades`, `active_trade_levels`, `daily_briefings`.

---

## Reuse from codeops-trading-app

| From codeops-trading-rss-ingester | Reused in local_trading_bot_v2 |
|-----------------------------------|---------------------------------|
| Feed list, categories, item shape | Feeds in `app/services/ingestion/feeds.py`; FeedItem in `app/models/feed_item.py` (plus sentiment, asset_tags) |
| Max age / cleanup | `feed_max_age_days`; daily cleanup job at 04:00 |
| Fetch → parse → save | Same flow with `feedparser` and SQLite |

**Shell:** Bash (Git Bash / WSL) or PowerShell; use `;` instead of `&&` in PowerShell where needed.

---

## What’s next

See [PLAN_PROGRESS.md](PLAN_PROGRESS.md) §10 for the full backlog. Current status:

| Step | Status |
|------|--------|
| Pipeline, scheduler, recommendation engine, taken-asset filter | Done |
| Research / rationale (multi-provider LLM), richer context (yfinance .info), company→ticker resolve | Done |
| Active-trade update (original + suggested + history), indices, blocklist, feed cleanup | Done |
| Daily briefing (checklist, job, API, UI) | Done |
| Export (CSV) | Later |
| MCP tools / Telegram (optional) | Later |

---

## Disclaimer

All analysis and recommendations are for **educational and research purposes only**. Not financial advice. See API responses and [TRADING_APP_PLAN.md](../TRADING_APP_PLAN.md) §8.
