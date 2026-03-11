# Tech Stack and Claude Equity Research Integration

**App location:** `local_trading_bot_v2` (this folder)  
**Specs:** [TRADING_APP_PLAN.md](../TRADING_APP_PLAN.md) (scope, phases, schema) and [PLAN_MODE.md](../PLAN_MODE.md) (behaviour, constraints).

This document recommends a concrete tech stack and how to integrate [claude-equity-research](https://github.com/quant-sentiment-ai/claude-equity-research) for stock analysis.

---

## 1. Recommended Tech Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| **Language** | **Python 3.11+** | Strong fit for data pipelines, NLP/sentiment (VADER, TextBlob), RSS (feedparser), market data (yfinance, httpx), and one-off scripts. Single language for Phase 1 (analysis) and Phase 2 (recommendations + API + jobs). |
| **API / app** | **FastAPI** | Async support, OpenAPI, minimal boilerplate. Serves market analysis, recommendations list, “Took it” / “Skipped”, active trades, and optional export (CSV). |
| **Database** | **SQLite** (dev) or **PostgreSQL** (production) | Recommendations, active_trades, and optional analysis cache. SQLite keeps the app runnable with zero infra; Postgres when you need concurrency or remote access. Use a single schema satisfying PLAN_MODE (recommendations with batch_id, status; active_trades linked to recommendation_id). |
| **Scheduled jobs** | **APScheduler** (in-process) or **Celery + Redis** | Daily batch (≤5 recommendations) and every-2h “update active trades only” job. APScheduler is enough for a single process; Celery if you later split workers. |
| **Phase 1: ingestion** | **feedparser** (RSS), **httpx** (APIs), **yfinance** / **pandas** | Reuse feed list from codeops-trading-app; add oil/commodity feeds. Pull price and conditions from free tiers (EIA, OilPriceAPI, Alpha Vantage, etc.). |
| **Phase 1: sentiment** | **VADER** or **TextBlob** (or small transformer) | Run on title/description; store sentiment_score and asset_tags. Lightweight and good enough for news headlines. |
| **Phase 2: recommendation engine** | **Python** (rules + scoring) | Input: last market analysis + asset universe + user risk preference. Output: up to 5 recommendations with risk_tier (conservative/moderate/aggressive). No heavy ML required initially. |

### Why not Node/Go for the main app?

- **Node:** Good for API and jobs, but the NLP/sentiment and data-pipeline ecosystem is weaker; you’d rely more on external services or subprocesses for sentiment and analysis.
- **Go:** Fits the existing RSS ingester (codeops-trading-app), but sentiment and ad-hoc data work are less convenient than in Python; mixing Go (ingestion) and Python (analysis/API) is possible but adds operational and mental overhead.

**Verdict:** Python + FastAPI keeps ingestion, sentiment, analysis, recommendation engine, API, and jobs in one stack and one repo under `local_trading_bot_v2`.

---

## 2. How Mandatory Is Claude Code? How Much Can You Progress Without It?

**Claude Code is not mandatory.** You can build and run the full app without it (and without the Anthropic API for equity research).

| What | Depends on Claude Code / Claude Equity Research? |
|------|---------------------------------------------------|
| **Phase 1: Market analysis** | **No.** RSS, price/conditions APIs, sentiment (VADER/TextBlob), aggregation, and the analysis report use only your stack (Python, feedparser, httpx, yfinance, etc.). |
| **Phase 2: Recommendations (≤5/day)** | **No.** The recommendation engine takes your market analysis + asset universe + risk preference and outputs up to 5 ideas. For **oil, commodities, forex** the plan already uses “your own analysis (sentiment + conditions + price)”. |
| **Phase 2: Stock recommendations** | **Optional.** For **stocks** (e.g. XOM, CVX, gold miners), you can either: (a) use the same pipeline (sentiment + price + conditions) and get usable stock ideas without any Claude, or (b) add richer “institutional-style” research later via Anthropic API or Claude Code. |
| **Took it / Skipped, active trades, jobs** | **No.** All behaviour in PLAN_MODE is independent of Claude. |

**Practical path:** Build Phase 1 and Phase 2 **without** Claude Code and without the Anthropic API. You get: ingestion, sentiment, market analysis, daily batch of ≤5 recommendations (including stocks via your own scoring), active trades, and updates. Add an “equity research” module (Anthropic API or Claude Code subprocess) later if you want fancier stock reports; until then, stock ideas come from your analysis pipeline like oil/commodities.

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
│   │   ├── equity_research/  # Optional: Anthropic API or Claude Code (Option B/D)
│   │   └── recommendations/  # Engine: analysis + universe → ≤5/day, risk_tier
│   └── jobs/              # APScheduler: daily batch, every-2h active-trade updates
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
| **Best tech stack** | **Python 3.11+**, **FastAPI**, **SQLite/PostgreSQL**, **APScheduler**; **feedparser**, **httpx**, **yfinance**, **VADER/TextBlob** for Phase 1; same stack for Phase 2 (recommendation engine + API + jobs). |
| **Where to build** | This folder: `local_trading_bot_v2`. |
| **Claude Code / Equity Research** | **Optional.** You can progress through Phase 1 and Phase 2 without it. If you add it later: use **Anthropic API + prompt reuse** (Option B) for stock ideas, or Option D (subprocess) for exact plugin output. |

Implementation should follow [TRADING_APP_PLAN.md](../TRADING_APP_PLAN.md) for scope and order and [PLAN_MODE.md](../PLAN_MODE.md) for behaviour (daily batch, ≤5, risk tier, Took it/Skipped, update job only for active trades).
