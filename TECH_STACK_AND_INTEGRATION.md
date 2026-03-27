# Tech Stack and Claude Equity Research Integration

**App location:** `local_trading_bot_v2` (this folder)  
**Implemented behaviour:** [PLAN_PROGRESS.md](PLAN_PROGRESS.md) is the in-repo source of truth. Original scope docs (`TRADING_APP_PLAN.md`, `PLAN_MODE.md`) may live elsewhere in a monorepo; they are **not** required paths inside this folder.

This document describes the **chosen stack** (as implemented) and how to optionally integrate [claude-equity-research](https://github.com/quant-sentiment-ai/claude-equity-research) for richer stock narratives.

---

## 1. Recommended Tech Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| **Language** | **Python 3.10+** (see `README.md`) | Data pipelines, NLP/sentiment, RSS (feedparser), market data (yfinance), FastAPI, jobs—single language end-to-end. |
| **API / app** | **FastAPI** | Async support, OpenAPI, minimal boilerplate. Serves market analysis, recommendations list, “Took it” / “Skipped”, active trades, and optional export (CSV). |
| **Database** | **SQLite** (dev) or **PostgreSQL** (production) | Recommendations, active_trades, and optional analysis cache. SQLite keeps the app runnable with zero infra; Postgres when you need concurrency or remote access. Use a single schema satisfying PLAN_MODE (recommendations with batch_id, status; active_trades linked to recommendation_id). |
| **Scheduled jobs** | **APScheduler** (in-process, `app/jobs/scheduler.py`) | Configurable intervals: ingestion + analysis, daily recommendation batch, active-trade updates, daily briefing, feed cleanup. Celery + Redis only if you split workers later. |
| **Phase 1: ingestion** | **feedparser** (RSS), **yfinance** / **pandas** | RSS feeds configured in-app; prices/technicals primarily from **yfinance**. |
| **Phase 1: sentiment** | **VADER** (+ optional **FinBERT** blend if `requirements-optional.txt` installed) | `sentiment_score` + `asset_tags` on feed items; keyword tags + dynamic ticker detection. |
| **Phase 2: recommendation engine** | **Python** (rules + scoring + regime context) | Input: latest `MarketAnalysis` payload + tradeable universe + preferences. Output: up to **5 recommendations per category** (stocks, commodities, other, indices) → **up to ~20** before correlation/risk-budget filters; ATR SL/TP, direction, confidence, optional LLM `rationale_text`. |

### Why not Node/Go for the main app?

- **Node:** Good for API and jobs, but the NLP/sentiment and data-pipeline ecosystem is weaker; you’d rely more on external services or subprocesses for sentiment and analysis.
- **Go:** Fits the existing RSS ingester (codeops-trading-app), but sentiment and ad-hoc data work are less convenient than in Python; mixing Go (ingestion) and Python (analysis/API) is possible but adds operational and mental overhead.

**Verdict:** Python + FastAPI keeps ingestion, sentiment, analysis, recommendation engine, API, and jobs in one stack and one repo under `local_trading_bot_v2`.

---

## 2. How Mandatory Is Claude Code? How Much Can You Progress Without It?

**Claude Code is not mandatory.** You can build and run the full app without it (and without the Anthropic API for equity research).

| What | Depends on Claude Code / Claude Equity Research? |
|------|---------------------------------------------------|
| **Phase 1: Market analysis** | **No.** RSS, yfinance-backed prices, sentiment (VADER + optional FinBERT), aggregation into `MarketAnalysis`. |
| **Phase 2: Recommendations (daily batch)** | **No.** Engine uses analysis + universe + regime + preferences; up to **5 per category** (~20 before filters). Same stack for stocks, commodities, forex, indices. |
| **Phase 2: Stock “research” prose** | **Optional.** In-app **research** service (Anthropic/OpenAI/Ollama/OpenRouter) adds rationales and on-demand summaries—**not** the same as the claude-equity-research plugin output format. |
| **Took it / Skipped, active trades, journal, calibration** | **No.** Independent of Claude Code. |

**Practical path:** The shipped app already runs **without** Claude Code. You get: ingestion, sentiment, analysis, daily batch, active trades (ATR updates, outcomes, journal), calibration (Platt/Brier/ECE), briefing, and **optional** multi-provider research (`app/services/research/`: OpenAI, Anthropic, Ollama, OpenRouter). claude-equity-research remains optional for institutional-style *reports*, not for core signals.

**Short-term trading:** For short-term trading (entry, SL, TP, expiry, sentiment- and conditions-driven), your pipeline alone is enough to reach the goal; you do not need Claude or the institutional report.

**What you cannot do without it:** You cannot produce the **institutional-style equity research report** that claude-equity-research delivers: the full structured output (executive summary, fundamental + catalyst + valuation with bull/base/bear, risk with position sizing, technical + options flow, insider signals, and the formal recommendation table with conviction level and upside/downside). Your own pipeline can still output stock ideas (buy/sell, SL/TP, risk_tier) from sentiment + price + conditions, but those will not have that report format, that depth of narrative, or those sections (e.g. analyst citations, options intelligence, insider activity) unless you add Claude (API or Claude Code) for stocks.

---

## 3. Claude Equity Research Integration (Optional)

claude-equity-research is a **Claude Code plugin**: it exposes a command (e.g. `/trading-ideas TICKER`) that runs inside **Claude Code** (Anthropic’s separate CLI app) with WebSearch/WebFetch and outputs institutional-style equity research (BUY/SELL/HOLD, price target, risk, etc.). There is **no HTTP API**; the plugin is CLI-driven.

**Cursor vs Claude Code:** Cursor uses Claude (and other) **models** for editing and chat inside the IDE. It does **not** run or include the **Claude Code CLI**. So the plugin’s `/trading-ideas` command is not available inside Cursor; it only works when you run the standalone Claude Code application. Your trading app (this repo) cannot “use Cursor” to call that command—it would need either the Claude Code CLI installed and invoked as a subprocess, or the Anthropic API (see below).

### Option A: Subprocess (run the plugin from your app)

- **How:** Install Claude Code and the plugin on the same machine; from your app, run e.g. `claude trading-ideas XOM` (or equivalent) as a subprocess; capture stdout; parse the markdown output (e.g. “RECOMMENDATION SUMMARY”, “Price Target”, “Conviction”).
- **Pros:** Uses the exact plugin output; no prompt maintenance.
- **Cons:** Requires Claude Code + auth where the app runs; subprocess and parsing are brittle if the plugin’s output format changes; harder to run in serverless or restricted environments.

### Option B: Anthropic API + same methodology (recommended)

- **How:** Reuse the **methodology and output structure** of claude-equity-research in your own service. The plugin’s [commands/trading-ideas.md](https://github.com/quant-sentiment-ai/claude-equity-research/blob/main/commands/trading-ideas.md) defines the research framework (financial performance, catalysts, valuation, risk, technical context, etc.). Turn that into a **system + user prompt** and call the **Anthropic API** (e.g. Claude 3.5 Sonnet) with:
  - Pre-fetched context (e.g. your market analysis, price, and a short summary of the ticker from your DB or a search), or
  - Tool use (e.g. “web search”) if you expose it in your backend.
- **Pros:** No CLI dependency; runs inside your process; you control input (e.g. your analysis + ticker) and parsing; works in any deployment (VM, Docker, serverless); you can still cite “methodology aligned with claude-equity-research” and keep disclaimers.
- **Cons:** You own the prompt and any updates if the plugin’s structure evolves.

**Recommendation:** Use **Option B** for production: a small “equity research” module in `local_trading_bot_v2` that calls the Anthropic API with a prompt derived from the plugin’s template. Use it for **stocks** (e.g. oil/gold/gas names) as one input to your recommendation engine. Store the structured result (rating, price target, conviction, risk snippet) in your DB and in `rationale_ref` for each stock recommendation.

### Option C: Omit entirely (valid)

- Do not integrate Claude Code or the Anthropic API. Use only your market analysis (sentiment + price + conditions) for all asset types, including stocks. Add equity research later if you want.

### Option D: Hybrid (optional)

- Use **Option B** by default. For users who already run Claude Code and want the **exact** plugin output, expose an optional “run plugin” path (e.g. admin script or one-off job) that shells out to `claude trading-ideas TICKER` and ingests the result. Keep the main pipeline on Option B.

---

## 4. Suggested Layout for `local_trading_bot_v2`

```
local_trading_bot_v2/
├── app/
│   ├── api/              # FastAPI routes: analysis, recommendations, feedback, active_trades
│   ├── core/              # Config, DB session, dependencies
│   ├── models/            # SQLAlchemy or similar: Recommendation, ActiveTrade, AnalysisCache
│   ├── services/
│   │   ├── ingestion/     # RSS, price, conditions
│   │   ├── sentiment/     # VADER/TextBlob, asset tagging
│   │   ├── analysis/      # Aggregate → market analysis report
│   │   ├── research/       # Optional LLM: rationales + on-demand summary (multi-provider)
│   │   └── recommendations/  # Engine: analysis + universe → batch, risk_tier, sizing
│   └── jobs/              # APScheduler wiring (see scheduler.py; intervals from settings)
├── data/                  # Optional: RSS cache, analysis output (if not only in DB)
├── tests/
├── .cursor/               # Optional: rules, MCP config
├── requirements.txt
├── README.md
└── TECH_STACK_AND_INTEGRATION.md  # This file
```

---

## 5. Summary

| Question | Answer |
|----------|--------|
| **Best tech stack** | **Python 3.10+**, **FastAPI**, **SQLite** (default) / Postgres-ready URL, **APScheduler**; **feedparser**, **yfinance**, **pandas**, **VADER** (+ optional FinBERT); recommendation engine + static HTML UI. |
| **Where to build** | This folder: `local_trading_bot_v2`. |
| **Claude Code / Equity Research** | **Optional.** You can progress through Phase 1 and Phase 2 without it. If you add it later: use **Anthropic API + prompt reuse** (Option B) for stock ideas, or Option D (subprocess) for exact plugin output. |

For **what is implemented today**, use [PLAN_PROGRESS.md](PLAN_PROGRESS.md) and [README.md](README.md). Original TRADING_APP_PLAN / PLAN_MODE documents are optional external references if your team maintains them.
