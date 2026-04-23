# local_trading_bot_v2

Market analysis app for **14-day trade predictions**: RSS + sentiment + technicals → regime-aware recommendations → active trade management → outcome logging → strategy backtesting.

---

## Stack

| Layer | Technology |
|---|---|
| Backend | Python / FastAPI (async) |
| Database | **TimescaleDB** (PostgreSQL 16 via TimescaleDB image) |
| Container runtime | **Podman** (Windows) / Docker |
| Orchestration | `docker-compose` via `Makefile` |
| Frontend | Vanilla JS / HTML (served as static files) |
| Market data | yfinance |
| Sentiment | VADER + FinBERT blend, RSS feeds |
| LLM (optional) | OpenAI / Anthropic / Ollama / OpenRouter |

---

## Prerequisites

- **Podman** (Windows) or **Docker Desktop** with `docker-compose`
- `.env` file in the project root (see below)
- Port **6001** free on the host

---

## Quick start

```bash
make build start
```

App is available at **http://localhost:6001**

```bash
make stop          # stop containers
make build start   # rebuild after code or dependency changes
```

### View logs

```bash
podman logs -f local-trading-bot
podman logs -f trading-timescaledb
```

---

## Environment (`.env`)

Create `.env` in the project root. Minimum required:

```env
POSTGRES_USER=trading
POSTGRES_PASSWORD=trading
POSTGRES_DB=trading
```

Full reference:

| Variable | Description | Default |
|---|---|---|
| `POSTGRES_USER` | DB user | `trading` |
| `POSTGRES_PASSWORD` | DB password | `trading` |
| `POSTGRES_DB` | DB name | `trading` |
| `DEBUG` | SQL echo / debug logging | `false` |
| `DATA_DIR` | Preferences, blocklist, briefing config | `./data` |
| `PREFERRED_RISK` | `conservative` / `moderate` / `aggressive` | `moderate` |
| `DAILY_BATCH_HOUR` | Hour (0–23) for daily recommendation batch | `6` |
| `ANALYSIS_RUN_INTERVAL_HOURS` | Ingestion + analysis interval | `2` |
| `UPDATE_ACTIVE_TRADES_INTERVAL_HOURS` | Active-trade SL/TP refresh | `2` |
| `FEED_MAX_AGE_DAYS` | Prune feed items older than this | `30` |
| `RESEARCH_LLM_PROVIDER` | `openai` / `anthropic` / `ollama` / `openrouter` | — |
| `RESEARCH_LLM_MODEL` | Model name for the chosen provider | — |
| `OPENAI_API_KEY` | OpenAI key | — |
| `ANTHROPIC_API_KEY` | Anthropic key | — |
| `OPENROUTER_API_KEY` | OpenRouter key | — |
| `OLLAMA_BASE_URL` | Ollama base URL | `http://localhost:11434` |

LLM keys are optional — recommendations and backtesting work without them; only research rationales require a configured provider.

---

## Web UI

| Page | URL | What it's for |
|---|---|---|
| **Dashboard** | `/dashboard` | Pipeline actions, risk stance, risk budget, LLM settings, calibration & regime status |
| **Recommendations** | `/recommendations` | Today's ideas — Took it / Skip; confidence, suggested size %, R:R; regime + technicals context |
| **Active Trades** | `/active-trades` | Open trades only; update SL/TP; signal alignment; close manually |
| **Journal** | `/journal` | Closed trades, filters, stats, closing prices |
| **Backtest** | `/backtest` | Historical replay of the signal engine; by-confidence, by-direction, by-regime, by-category breakdowns; walk-forward IS/OOS validation |
| **Live Trading** | `/live-trading` | Real-time signal stream and live trade monitoring |
| **Trade Builder** | `/trade-builder` | Manual trade construction with engine-aligned sizing |
| **Research** | `/research` | LLM research for any asset; company → ticker resolve |
| **Market Analysis** | `/analysis` | Latest report, sentiment, news by category |
| **Calendar** | `/calendar` | Macro calendar — upcoming events with market impact |
| **Briefing** | `/briefing` | Daily checklist (SPX, VIX, WTI, Brent, gold, silver, …) |

---

## Engine behaviour

### Recommendation categories
The engine produces up to **5 recommendations per category**, up to ~20 total per batch:

| Category | Symbols |
|---|---|
| `stocks_large` | Large-cap equities |
| `stocks_mid` | Mid-cap equities |
| `stocks_small` | Small-cap equities |
| `indices` | ETFs (SPY, QQQ, etc.) |
| `commodities` | Futures (oil, gold, silver, …) |

### Signal scoring & filters
- **Regime-aware:** SPX + VIX determine risk-on / cautious / risk-off; affects confidence thresholds, direction bias, and category limits.
- **Soft penalty pool** (capped at 25 pts): ADX weak, OBV against, MACD decay, RSI slope, MACD alignment, volume confirmation, cap-tier penalties — pooled so no combination of weak signals can stack into an invisible hard gate.
- **Category-tier penalties:** mid-cap -4 pts, small-cap -6 pts (backtest-validated; these categories underperform breakeven without the offset).
- **Hard gates kept:** RSI exhaustion, R:R minimum, EMA200 short gate, crisis/high-vol regime block.
- **Correlation & budget trim:** after per-category ranking, highly correlated pairs are dropped and global risk budget (sum of Size %) is enforced.

### Backtesting
The backtest replays the exact signal engine on historical OHLCV data:
- Outcomes: `hit_tp`, `hit_sl`, `expired`
- Metrics: win rate, Sharpe ratio, avg P&L (R), max drawdown, breakeven WR, by-confidence bucket, by-direction, by-regime, by-category
- **Walk-forward analysis:** rolling IS/OOS windows (default 252/63 trading days) with degradation ratio — ≥ 0.85 = robust, < 0.70 = likely overfit
- Current validated performance: **Sharpe 2.486, WR 31.2%, Avg P&L +0.119R** (3-year backtest, walk-forward degradation ratio 0.964)

---

## API overview

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/health` | Health check |
| `GET` | `/api/recommendations` | List recommendations (`?group_by=category`) |
| `POST` | `/api/recommendations/{id}/took-it` | Mark taken → creates active trade |
| `POST` | `/api/recommendations/{id}/skipped` | Mark skipped |
| `GET` | `/api/active-trades` | Open trades (`?include_closed=true` for all) |
| `POST` | `/api/active-trades/{id}/close-manually` | Manual close + capture price |
| `GET` | `/api/analysis/latest` | Latest market analysis |
| `GET` | `/api/market/context` | Regime, VIX/SPX context, correlations |
| `GET` | `/api/regime/status` | Regime snapshot / flags |
| `GET` | `/api/calibration` | Buckets, Brier, ECE, Platt metadata |
| `POST` | `/api/calibration/fit` | Fit Platt scaling |
| `POST` | `/api/backtest/run` | Start backtest (async, returns `run_id`) |
| `GET` | `/api/backtest/runs` | List backtest runs |
| `GET` | `/api/backtest/runs/{id}` | Run details + full metrics |
| `GET` | `/api/backtest/runs/{id}/trades` | Paginated trade list |
| `DELETE` | `/api/backtest/runs/{id}` | Delete run + trades |
| `POST` | `/api/backtest/walkforward` | Walk-forward analysis (blocking, ~1-2h for 3yr range) |
| `GET` | `/api/briefing/latest` | Latest briefing |
| `POST` | `/api/briefing/run` | Run briefing now |
| `GET` | `/api/preferences` | Risk, LLM, risk budget |
| `PATCH` | `/api/preferences` | Update preferences |
| `GET` | `/api/research/resolve?q=...` | Company / phrase → ticker |
| `GET` | `/api/research/summary?asset_id=...` | On-demand research |
| `POST` | `/api/actions/run-pipeline-once` | Ingest + analysis + batch |
| `POST` | `/api/actions/refresh-todays-batch` | Rebuild today's batch |
| `POST` | `/api/actions/purge-all` | Wipe recs + trades (testing) |

Full interactive docs: **http://localhost:6001/docs**

---

## Data files

| Path | Description |
|---|---|
| `data/preferences.json` | Risk tier, LLM config, risk budget |
| `data/blocklist.txt` | Optional extra ticker blocklist (one per line) |
| `data/briefing_checklist.json` | Optional briefing config |

Database volume (`timescale_data`) is managed by Docker/Podman and persists across restarts.

---

## Troubleshooting

**Very few recommendations:**
1. Check risk budget — default is **25** (sum of all Size % values). A value of 1.0 will drop nearly everything. Set via Dashboard → Risk budget per batch, then Refresh today's batch.
2. Check regime — in risk-off, confidence thresholds are stricter. See logs for `min_confidence` and skip reasons.
3. Check logs: `podman logs -f local-trading-bot`

**Containers not starting:**
- Ensure port 6001 is free
- Check `.env` exists and has correct credentials
- Run `make build` if code or `requirements.txt` changed

---

## Disclaimer

All analysis and recommendations are for **educational and research purposes only**. Past backtest performance does not guarantee future results. Not financial advice.
