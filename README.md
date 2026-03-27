# local_trading_bot_v2

Trading insight app: **market analysis** (RSS + sentiment + technicals), **regime-aware trade recommendations**, user feedback (**Took it / Skipped**), **active trade management** (ATR-based suggested SL/TP, signal alignment), **outcome logging & calibration**, **position sizing & risk budget**, **trade journal**, research rationales (multi-provider LLM), and an optional **daily briefing**.

**Living spec & progress:** [PLAN_PROGRESS.md](PLAN_PROGRESS.md) (architecture, constraints, what’s implemented).  
**Stack & optional Claude integration:** [TECH_STACK_AND_INTEGRATION.md](TECH_STACK_AND_INTEGRATION.md).  
**Future ideas:** [docs/FUTURE_RECOMMENDATION_IMPROVEMENTS.md](docs/FUTURE_RECOMMENDATION_IMPROVEMENTS.md).  
*If your monorepo keeps original scope docs (`TRADING_APP_PLAN.md`, `PLAN_MODE.md`) **above** this folder, use those paths locally; they are not bundled in this repo snapshot.*

---

## Prerequisites

- **Python 3.10+** (async, type hints)
- **pip** (or use `python -m pip` if `pip` is not on PATH)
- **Virtual environment** (strongly recommended)
- **Network** for RSS feeds and **yfinance**
- **Optional:** API keys for cloud LLM providers (research rationales)
- **Optional:** [Docker](https://docs.docker.com/get-docker/) / Docker Compose
- **Optional (local LLM):** [Ollama](https://ollama.ai/) if you use provider `ollama`
- **Optional (FinBERT sentiment blend):** `pip install -r requirements-optional.txt` (PyTorch + transformers; see file for notes)

---

## Setup

From the **project root** (`local_trading_bot_v2`):

```bash
cd local_trading_bot_v2
python -m venv .venv
```

### Activate the virtual environment (pick your shell)

| Shell | Command |
|--------|---------|
| **Windows — PowerShell** | `.venv\Scripts\Activate.ps1` |
| **Windows — Command Prompt (cmd)** | `.venv\Scripts\activate.bat` |
| **Windows — Git Bash** | `source .venv/Scripts/activate` |
| **macOS / Linux / WSL** | `source .venv/bin/activate` |

Then install dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

**Note:** On Windows Git Bash, always use **forward slashes** in paths (e.g. `.venv/Scripts/activate`), not backslashes.

**Optional FinBERT:** after the base install:

```bash
python -m pip install -r requirements-optional.txt
```

### Environment (optional)

Create a `.env` file in the project root. The app loads it via `pydantic-settings`.

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | Async SQLAlchemy URL | `sqlite+aiosqlite:///./data/trading.db` |
| `DEBUG` | SQL echo / debug | `false` |
| `DATA_DIR` | DB, preferences, blocklist, briefing | `./data` |
| `PREFERRED_RISK` | Risk filter (`conservative` / `moderate` / `aggressive`) | `moderate` |
| `DAILY_BATCH_HOUR` | Hour (0–23) for daily recommendation batch | `6` |
| `ANALYSIS_RUN_INTERVAL_HOURS` | Ingestion + analysis interval | `2` |
| `UPDATE_ACTIVE_TRADES_INTERVAL_HOURS` | Active-trade SL/TP refresh | `2` |
| `FEED_MAX_AGE_DAYS` | Delete feed items older than this | `30` |
| Research LLM | `RESEARCH_LLM_PROVIDER`, `RESEARCH_LLM_MODEL`, `*_API_KEY`, `OLLAMA_BASE_URL` | — |

On first run, the app creates `data/`, the SQLite database, and tables (with lightweight column migrations).

---

## Run the app

**Always:** project root + **venv activated**, then:

### Start Uvicorn (pick your shell)

The command is the same everywhere; only the way you chain commands differs.

**PowerShell** (use `;` if you need two steps in one line):

```powershell
cd C:\path\to\local_trading_bot_v2
.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Command Prompt (cmd):**

```bat
cd C:\path\to\local_trading_bot_v2
.venv\Scripts\activate.bat
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Git Bash** (one line after `cd`):

```bash
cd /c/path/to/local_trading_bot_v2 && source .venv/Scripts/activate && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**macOS / Linux / WSL:**

```bash
cd /path/to/local_trading_bot_v2 && source .venv/bin/activate && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- **Root:** http://localhost:8000/ → redirects to dashboard  
- **API docs:** http://localhost:8000/docs  

The **scheduler** starts with the app: ingestion + analysis (interval), daily batch (default 06:00), active-trade updates, daily briefing, feed cleanup.

---

## Web UI (current)

| Page | URL | What it’s for |
|------|-----|----------------|
| **Dashboard** | `/dashboard` | Pipeline actions, risk stance, **risk budget per batch**, LLM settings, **calibration & regime status**, glossary / key concepts |
| **Recommendations** | `/recommendations` | Today’s ideas; **Took it / Skip**; confidence, **suggested size %**, R:R; **Context** column (technicals + regime notes); **macro** banner + **calibration** strip when APIs return them |
| **Research** | `/research` | LLM research for any asset; company → ticker resolve |
| **Market analysis** | `/analysis` | Latest report, sentiment, news by category |
| **Active trades** | `/active-trades` | **Open** trades only (taken, not yet closed); update SL/TP; **Close manually** |
| **Journal** | `/journal` | **Closed** trades (outcomes), filters, stats, **closing price** when recorded |
| **Briefing** | `/briefing` | Daily checklist (SPX, VIX, WTI, Brent, gold, silver, rec count, …) |

Help text in the UI explains **market regime** (risk-on / cautious / risk-off), confidence buckets, and sizing.

---

## Behaviour highlights (engine & data)

- **Regime:** SPX + VIX → affects thresholds, direction bias, and correlation / macro overlays. Transitions can surface alerts on Dashboard / Active trades.
- **Daily batch:** Up to **5 recommendations per category** (stocks, commodities, other, indices) → **up to ~20** total. Each category is ranked separately; the engine then keeps rows in order while applying **global** correlation and **global** risk-budget (sum of Size %) across the whole batch. Default risk budget **25** is sized for ~20 small positions; increase it if logs show budget skips.
- **Active trades API:** `GET /api/active-trades` returns **non-closed** trades by default. Use `?include_closed=true` for all rows (e.g. Journal).
- **Outcomes:** `hit_tp`, `hit_sl`, `expired`, `manual` — used for calibration and journal. **Closing price** is stored when the outcome is set (scheduler or manual close with live quote).
- **Preferences:** `risk_budget_1r` caps the **sum** of **all** rows’ **Size** values (% of capital). Default **25.0** fits a full batch (~20 recommendations); **1.0** is far too small. Stored in `data/preferences.json`.

---

## Docker (optional)

```bash
make build   # first time or after dependency/code changes
make start   # background
make stop    # stop container
```

Same URLs; ensure port **8000** is exposed. On Windows, add Docker’s `bin` folder to PATH if `docker` is not found.

**Podman:** you can point the Docker CLI at Podman (`docker context create/use podman` with your machine’s pipe/socket). Then `make build` / `make start` work the same.

---

## API overview (selected)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Health check |
| `GET` | `/api/analysis/latest` | Latest market analysis |
| `GET` | `/api/recommendations` | List recommendations (`?group_by=category`, filters). Response includes `macro_overlay` when in a macro window (FOMC/CPI/NFP ±1 day). |
| `POST` | `/api/recommendations/{id}/took-it` | Taken → creates active trade |
| `POST` | `/api/recommendations/{id}/skipped` | Skipped |
| `GET` | `/api/active-trades` | Open trades (add `?include_closed=true` for all) |
| `POST` | `/api/active-trades/{id}/close-manually` | Manual close + capture price |
| `GET` | `/api/calibration` | Buckets, Brier, ECE, Platt/adaptive metadata |
| `POST` | `/api/calibration/fit` | Fit Platt scaling (needs enough closed trades) |
| `GET` | `/api/regime/status` | Regime snapshot / flags (for UI) |
| `GET` | `/api/research/providers` | LLM providers + config |
| `GET` | `/api/research/resolve?q=...` | Company / phrase → ticker (cached + yfinance.Search) |
| `GET` | `/api/research/summary?asset_id=...&category=...` | On-demand research + optional `generate=true` |
| `GET` | `/api/market/context` | Regime, VIX/SPX context, correlations, thresholds |
| `GET` | `/api/briefing/latest` | Latest briefing |
| `POST` | `/api/briefing/run` | Run briefing now |
| `GET` | `/api/preferences` | Risk, LLM, **risk_budget_1r** |
| `PATCH` | `/api/preferences` | Update preferences (JSON body) |
| `POST` | `/api/actions/run-pipeline-once` | Ingest + analysis + batch |
| `POST` | `/api/actions/refresh-todays-batch` | Rebuild today’s batch |
| `POST` | `/api/actions/clear-todays-batch` | Clear pending for today |
| `POST` | `/api/actions/purge-all` | Wipe recs + trades (testing) |
| `POST` | `/api/actions/update-active-trades` | Recalc active-trade levels (same as UI refresh) |

Responses include a `disclaimer` where applicable.

---

## Research rationale (multi-provider LLM)

Providers: **OpenAI**, **Anthropic**, **Ollama**, **OpenRouter**. Env vars: `RESEARCH_LLM_PROVIDER`, `RESEARCH_LLM_MODEL`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `OLLAMA_BASE_URL`.

Dashboard can override and persist to `data/preferences.json`. If no key / provider, rationales are skipped; recommendations still work.

---

## Daily briefing

Runs on schedule and manually from **Briefing** or `POST /api/briefing/run`. Default checklist includes WTI (`CL=F`), Brent (`BZ=F`), gold (`GC=F`), and silver (`SI=F`). Optional override: `data/briefing_checklist.json` (see `app/services/briefing.py`).

---

## Populate data without waiting for the scheduler

- Dashboard → **Run full pipeline**, or from project root with venv active:

```bash
python scripts/run_pipeline_once.py
```

- `python scripts/refresh_todays_batch.py` — rebuild today’s batch (e.g. after changing preferences).

---

## Testing / sanity check

- **Health:** `curl http://localhost:8000/api/health` (or open in browser)
- **Pipeline:** run once, then **Analysis** + **Recommendations**
- **Taken trade:** **Took it** → **Active trades**; after close → **Journal**
- **Research:** `/research` with a configured LLM

Automated test suites are minimal; the above is the main manual validation.

---

## Data and files

| Path | Description |
|------|-------------|
| `data/trading.db` | SQLite DB |
| `data/preferences.json` | Risk, LLM, **risk_budget_1r** |
| `data/blocklist.txt` | Optional ticker blocklist |
| `data/briefing_checklist.json` | Optional briefing config |

**Main tables:** `feed_items`, `market_analyses`, `recommendations`, `active_trades` (incl. `outcome`, `outcome_at`, `closing_price`), `active_trade_levels`, `daily_briefings`.

---

## Troubleshooting: very few recommendations

1. **Risk budget (most common):** The batch targets **~20** rows (5 per category). Set **Risk budget per batch** to **25** (default) or higher and **Refresh today’s batch**. A value like **1.0** caps total Size at 1 percentage point and will drop most ideas.
2. **Regime / confidence:** In **risk-off**, thresholds are stricter; check logs for `min_confidence` and skip reasons.
3. **Correlation:** Pairs with very high return correlation to an already-selected idea are skipped (see engine logs).

---

## Shell tips

- **PowerShell:** use `;` between commands; execution policy may block scripts — you can run `python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000` without activating if you use `.venv\Scripts\python.exe` explicitly.
- **Git Bash on Windows:** `source .venv/Scripts/activate` (forward slashes).
- **WSL:** treat paths as Linux; venv is `.venv/bin/activate`.

---

## Disclaimer

All analysis and recommendations are for **educational and research purposes only**. Not financial advice. See API `disclaimer` fields and [PLAN_PROGRESS.md](PLAN_PROGRESS.md) §7.
