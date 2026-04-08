# Live Trading — Implementation Plan

> **Status**: Draft — awaiting approval before any code changes.
> **Scope**: New independent feature. Shares the same Docker container and nav bar. Does NOT modify existing code.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture Diagram](#2-architecture-diagram)
3. [Component Breakdown](#3-component-breakdown)
4. [File Structure](#4-file-structure)
5. [Database Schema — New Tables](#5-database-schema--new-tables)
6. [API Endpoints — New Routes](#6-api-endpoints--new-routes)
7. [Data Stream Engine](#7-data-stream-engine)
8. [Data Stream Processing (LLM)](#8-data-stream-processing-llm)
9. [Selected Trade Tracker](#9-selected-trade-tracker)
10. [UI Page — Live Trading](#10-ui-page--live-trading)
11. [Integration Points (touches existing files)](#11-integration-points-touches-existing-files)
12. [Technical Decisions & Recommendations](#12-technical-decisions--recommendations)
13. [Implementation Phases](#13-implementation-phases)
14. [Risks & Constraints](#14-risks--constraints)

---

## 1. Overview

**Live Trading** is a real-time market monitoring and AI-powered recommendation page. It differs from the existing daily batch system in that:

- Data ingestion is **continuous** (news polled every 30–60s, prices every 5s)
- LLM analysis is triggered **by button press** (manual full batch) AND **automatically on every new news event** (re-runs analysis with fresh context)
- A selected trade is **pinned** and re-analyzed automatically on every new news event
- Market data is delivered to the browser via **WebSocket / SSE**
- Trading API integration is a **placeholder** (wired but disabled)

The feature is **fully independent** of the existing recommendation engine, scheduler, and batch pipeline. It shares only:
- The Docker container and port
- The FastAPI `app` instance (new router registered)
- The existing LLM config from `preferences.py` (provider, model, API key)
- The existing `common.css` + `icons.js` for UI theming
- The same `DATABASE_URL` (new tables only)

---

## 2. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         LIVE TRADING FEATURE                            │
│                                                                         │
│  ┌──────────────┐   ┌──────────────┐                                   │
│  │  RSS Feeds   │──▶│              │                                   │
│  │  (30s poll)  │   │  Data Stream │   ┌──────────────────────────┐   │
│  └──────────────┘   │   Engine     │──▶│  Data Stream Processing  │   │
│                     │              │   │                          │   │
│  ┌──────────────┐   │ • news poll  │   │ • LLM analysis (button)  │   │
│  │Market Tickers│──▶│ • price poll │   │ • Quant/Trader prompts   │   │
│  │  (5s poll)   │   │ • in-memory  │   │ • 6 recs per category    │   │
│  └──────────────┘   │   bus        │   │ • confidence > 50%       │   │
│                     └──────────────┘   │ • selected trade watcher │   │
│                                        └──────────┬───────────────┘   │
│                      ┌─────────────────┐          │                    │
│                      │  Quant & Trader  │──────────┘                   │
│                      │  Skills (prompts)│                              │
│                      └─────────────────┘                              │
│                                                                         │
│                              ┌────────────────────────────────────┐   │
│                              │        Data UI Display             │   │
│                              │                                    │   │
│                              │  [Analyze button]                  │   │
│                              │  Category tabs: stocks, commodities│   │
│                              │  options, FX, crypto, indices      │   │
│                              │  Recommendation cards (6 per cat)  │   │
│                              │  Selected trade panel (pinned)     │   │
│                              │  Stream: price, volume, SL/TP      │   │
│                              └────────────────┬───────────────────┘   │
│                                               │                        │
│                              If trade         │ Yes                    │
│                              selected? ───────┘                        │
│                                               │                        │
│                              ┌────────────────▼───────────────────┐   │
│                              │  Get Data Stream Processing for    │   │
│                              │  selected ticker (auto on news)    │   │
│                              │  + Trading API placeholder         │   │
│                              └────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Component Breakdown

### 3.1 Data Stream Engine (`app/live_trading/stream_engine.py`)

Runs as a background asyncio task started at app startup (alongside APScheduler).

**Responsibilities**:
- Poll RSS feeds every 30s (reuse `services/ingestion/feeds.py` feed list)
- Detect **new** news articles by comparing against `live_news_items` table IDs
- On new article → broadcast to in-memory event bus → trigger selected trade re-analysis
- Poll market prices via yfinance every 5s for a configurable ticker watchlist
- Maintain in-memory state: `latest_prices`, `latest_news_buffer` (last 50 per asset)
- Broadcast price updates to all connected WebSocket clients

**In-memory bus** (asyncio.Queue per subscriber):
- `news_events_queue` — new articles; consumed by Stream Processor
- `price_events_queue` — price ticks; broadcast directly to WS clients

**Not responsible for**:
- LLM calls (that's Stream Processor)
- Persisting market prices (only news goes to DB)

---

### 3.2 Data Stream Processing (`app/live_trading/stream_processor.py`)

**Responsibilities**:
- Triggered by **button press** (POST endpoint) for full batch analysis
- Triggered **automatically on every new news event** — re-runs full analysis with fresh context (debounced: max once per 3 minutes to avoid LLM flooding)
- Triggered **automatically on news for selected ticker** — runs narrow ticker re-analysis regardless of the 3-minute debounce
- Builds context snapshot: latest prices + technicals + recent news sentiment
- Constructs LLM prompt with professional trader + quant skills
- Parses LLM response → 6 recommendations per category with confidence ≥ 50%
- For selected trade re-analysis: returns updated confidence, flip signal, risk changes

**LLM call modes**:

| Mode | Trigger | Debounce | Output |
|------|---------|----------|--------|
| `full_batch` | Analyze button (manual) | None | 6 per category × N categories |
| `news_refresh` | Any new news article detected | 3 min | 6 per category × N categories (fresh context) |
| `ticker_watch` | New news matching selected ticker | 5 min per ticker | Updated analysis for 1 ticker |

**System prompt includes**:
- Role: "Senior quantitative trader and financial analyst"
- Quant skills: ATR, RSI, Bollinger, momentum, mean-reversion, Kelly criterion, R:R math
- Trading skills: trend-following, breakout, reversion, news-driven momentum, options flow
- Risk management: position sizing, max drawdown, volatility regime
- Output format: strict JSON schema (validated before returning)

**Uses** (reused from existing app):
- `services/technicals.build_snapshot()` for technical context
- `app/core/preferences.get_research_llm_config()` for LLM provider/model
- `services/research/llm.py` pattern for provider dispatch

---

### 3.3 Selected Trade Tracker (`app/live_trading/trade_watcher.py`)

**Responsibilities**:
- Persist selected trade to `data/live_selected_trade.json`
- On startup: reload selected trade if file exists
- Expose `get_selected_trade()` / `set_selected_trade(ticker, category, entry_data)` / `clear_selected_trade()`
- When stream engine fires `news_event` for any asset that matches selected ticker → calls `stream_processor.analyze_ticker(ticker)` → pushes result to WebSocket
- Track SL/TP suggestions as they update, history stored in `live_trade_updates` table

**State file** `data/live_selected_trade.json`:
```json
{
  "ticker": "AAPL",
  "category": "stocks",
  "selected_at": "2026-04-08T10:00:00Z",
  "entry_price": 185.20,
  "direction": "long",
  "stop_loss": 180.00,
  "take_profit": 195.00,
  "confidence": 72,
  "last_analysis_at": "2026-04-08T10:05:00Z"
}
```

Selected trade **persists across**:
- Browser page refreshes
- Analysis batch refreshes
- App restarts (file-based)

Selected trade is **cleared only when**:
- User explicitly clicks "Deselect / Close Watch"

---

### 3.4 Trading API Placeholder (`app/live_trading/trading_api.py`)

Simple stub module. All functions return `{"status": "placeholder", "message": "Trading API not connected"}`.

Defines the interface that a real broker integration will implement:
- `place_order(ticker, direction, qty, order_type, price)` → placeholder
- `cancel_order(order_id)` → placeholder
- `get_open_orders()` → placeholder
- `get_account_balance()` → placeholder

UI shows a "Connect Broker" section with greyed-out order controls.

---

### 3.5 WebSocket / SSE Transport (`app/live_trading/live_routes.py`)

**WebSocket endpoint** `/api/live/ws`:
- Client connects → receives price stream for selected tickers
- Server pushes: `{type: "price", ticker, price, volume, change_pct}`
- Server pushes: `{type: "news", ticker, headline, sentiment, source}`
- Server pushes: `{type: "analysis_update", ticker, confidence, direction, rationale, sl, tp}`
- On disconnect: removed from active connections set

**Fallback**: If client doesn't support WebSocket → SSE endpoint `/api/live/stream` using `EventSourceResponse` (via `sse-starlette` package).

---

## 4. File Structure

```
app/
├── live_trading/                        ← NEW MODULE (independent)
│   ├── __init__.py
│   ├── stream_engine.py                 ← RSS + price polling, event bus
│   ├── stream_processor.py             ← LLM analysis, quant skills
│   ├── trade_watcher.py                ← selected trade persistence + auto-refresh
│   ├── trading_api.py                  ← placeholder broker integration
│   └── live_routes.py                  ← FastAPI router + WebSocket
│
├── static/
│   └── live-trading.html               ← NEW UI page
│
└── main.py                             ← ADD: include live_router, add /live-trading route
```

```
docs/
└── LIVE_TRADING_PLAN.md                ← THIS FILE
```

---

## 5. Database Schema — New Tables

### 5.1 `live_news_items` — TimescaleDB hypertable (partitioned by `fetched_at`)

Separate from existing `feed_items` to keep live stream independent.

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(512) PK | Article ID (dedup key) |
| `title` | Text | |
| `link` | String(2048) | |
| `published` | DateTime(tz) | |
| `fetched_at` | DateTime(tz) | Partition key |
| `source` | String(256) | |
| `category` | String(64) | |
| `asset_tags` | JSON | e.g. `["AAPL", "tech"]` |
| `sentiment_score` | Float | VADER score |
| `is_new` | Boolean | True on first insert |

**TTL**: Auto-purge rows older than 7 days (separate from existing 30-day rule).

---

### 5.2 `live_analysis_runs`

Stores each time the "Analyze" button is pressed.

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(36) PK | UUID |
| `created_at` | DateTime(tz) | |
| `trigger` | String(32) | `manual_button` \| `news_refresh` \| `ticker_watch` |
| `ticker_context` | String(32) | Null for full batch; ticker for auto |
| `llm_prompt_tokens` | Integer | |
| `llm_response_tokens` | Integer | |
| `provider` | String(32) | |
| `model` | String(64) | |
| `raw_response` | Text | Full LLM JSON response |
| `parsed_ok` | Boolean | Did JSON parse cleanly? |

---

### 5.3 `live_recommendations`

One row per recommended item per analysis run.

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(36) PK | UUID |
| `run_id` | String(36) FK | → live_analysis_runs.id |
| `created_at` | DateTime(tz) | |
| `asset_category` | String(32) | `stocks` \| `commodities` \| `options` \| `fx` \| `crypto` \| `indices` |
| `ticker` | String(32) | |
| `direction` | String(8) | `long` \| `short` |
| `entry_price` | Float | |
| `stop_loss` | Float | |
| `take_profit` | Float | |
| `confidence` | Integer | 0–100 (only ≥50 returned) |
| `risk_reward` | Float | |
| `rationale` | Text | 2–3 sentence LLM rationale |
| `timeframe` | String(16) | `scalp` \| `intraday` \| `swing` |
| `catalysts` | JSON | List of news/technical catalysts |

---

### 5.4 `live_trade_updates`

History of auto-refresh updates for selected trade.

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(36) PK | UUID |
| `created_at` | DateTime(tz) | |
| `ticker` | String(32) | |
| `trigger_headline` | Text | News headline that triggered this update |
| `confidence_before` | Integer | |
| `confidence_after` | Integer | |
| `direction_flip` | Boolean | True if direction changed |
| `suggested_sl` | Float | |
| `suggested_tp` | Float | |
| `assessment` | Text | LLM short assessment |
| `action_advised` | String(32) | `hold` \| `tighten_sl` \| `exit` \| `add` |

---

## 6. API Endpoints — New Routes

All prefixed `/api/live`. Defined in `app/live_trading/live_routes.py`.

### Stream Engine Control

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/live/status` | `{engine_running, connected_clients, tickers_watched, last_news_at, selected_trade}` |
| POST | `/api/live/watchlist` | Body: `{tickers: ["AAPL", "GOLD"]}` — set price watchlist |
| GET | `/api/live/watchlist` | Returns current watchlist |

### Analysis

| Method | Path | Body | Description |
|--------|------|------|-------------|
| POST | `/api/live/analyze` | `{categories?: [...]}` | Triggers full LLM batch analysis; returns run_id |
| GET | `/api/live/analysis/latest` | — | Latest analysis run with all recommendations |
| GET | `/api/live/analysis/history` | `?limit=10` | Last N analysis runs |

### Recommendations

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/live/recommendations` | `?run_id=<id>&category=<cat>` — paginated, filtered |

### Selected Trade

| Method | Path | Body | Description |
|--------|------|------|-------------|
| GET | `/api/live/selected-trade` | — | Current selected trade + latest update |
| POST | `/api/live/selected-trade` | `{ticker, category, entry_price, direction, stop_loss, take_profit, confidence}` | Pin a trade for watching |
| DELETE | `/api/live/selected-trade` | — | Deselect / stop watching |
| GET | `/api/live/selected-trade/history` | — | All `live_trade_updates` for selected ticker |

### Prices

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/live/prices` | `?tickers=AAPL,GOLD` — current snapshot prices |

### Trading API (Placeholder)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/live/trading-api/status` | `{connected: false, broker: null, message: "placeholder"}` |
| POST | `/api/live/trading-api/order` | Always returns placeholder response |

### WebSocket

| Method | Path | Description |
|--------|------|-------------|
| WS | `/api/live/ws` | Live price + news + analysis update stream |

**WebSocket message types**:
```json
{ "type": "price",    "ticker": "AAPL",   "price": 185.20, "volume": 1234567, "change_pct": 0.42 }
{ "type": "news",     "ticker": "AAPL",   "headline": "...", "sentiment": 0.6, "source": "Reuters" }
{ "type": "analysis_update", "ticker": "AAPL", "confidence": 74, "direction": "long",
  "rationale": "...", "suggested_sl": 180.0, "suggested_tp": 196.0, "action_advised": "hold" }
{ "type": "batch_ready", "run_id": "...", "categories": ["stocks", "commodities", ...] }
```

---

## 7. Data Stream Engine

### Implementation: `app/live_trading/stream_engine.py`

```python
# Pseudocode — not final code
class DataStreamEngine:
    def __init__(self):
        self.news_subscribers: list[asyncio.Queue] = []
        self.price_subscribers: list[asyncio.Queue] = []
        self.latest_prices: dict[str, dict] = {}          # ticker → {price, volume, change_pct}
        self.seen_news_ids: set[str] = set()               # loaded from DB on startup
        self.watchlist: list[str] = []                     # configurable

    async def start(self):
        await asyncio.gather(
            self._news_poll_loop(),       # every 30s
            self._price_poll_loop(),      # every 5s
        )

    async def _news_poll_loop(self):
        while True:
            items = await fetch_all_feeds()                 # existing ingestion RSS
            new_items = [i for i in items if i["id"] not in self.seen_news_ids]
            for item in new_items:
                await self._save_live_news(item)            # save to live_news_items
                self.seen_news_ids.add(item["id"])
                await self._broadcast_news(item)            # push to news_subscribers
            await asyncio.sleep(30)

    async def _price_poll_loop(self):
        while True:
            prices = await self._fetch_prices(self.watchlist)   # yfinance batch download
            for ticker, data in prices.items():
                self.latest_prices[ticker] = data
                await self._broadcast_price(ticker, data)
            await asyncio.sleep(5)
```

**RSS source**: Reuse `services/ingestion/feeds.py:default_feeds()` — same 20+ feeds.

**Price fetching**: `yfinance.download(tickers, period="1d", interval="1m", auto_adjust=True)` — last row = latest price. For true real-time (sub-second), a paid provider (Polygon.io, Alpaca) would be needed. For now, 5s polling via yfinance is sufficient for swing/intraday.

**Startup**: Engine starts in `app/main.py` lifespan, after APScheduler.

---

## 8. Data Stream Processing (LLM)

### Implementation: `app/live_trading/stream_processor.py`

#### Full Batch Analysis (button press)

**Context snapshot built**:
1. Latest prices for all watchlist tickers (from engine `latest_prices`)
2. Recent news last 2 hours (from `live_news_items` table, last 50 items)
3. Market regime from `services/market_context.detect_market_regime()` (reused)
4. DXY, VIX, SPX current values (from engine latest_prices)
5. Technical snapshots for top candidates (ATR, RSI, trend)

**LLM System Prompt** (quant/trader skills):
```
You are a senior quantitative trader and financial analyst with expertise in:
- Technical analysis: ATR-based stop placement, multi-timeframe trend alignment,
  RSI divergence, MACD momentum, Bollinger Band breakouts, volume profile analysis
- Quantitative methods: Kelly Criterion position sizing, Sharpe ratio estimation,
  maximum adverse excursion, R-multiples, volatility regime classification
- Trading strategies: trend-following, mean-reversion, news-momentum, gap-and-go,
  breakout confirmation, options flow analysis
- Risk management: position sizing per conviction, portfolio heat, correlated risk

Given the market context provided, identify the BEST 6 trading opportunities per
category (stocks, commodities, options, fx, crypto, indices). For each opportunity:
- Only include if estimated win probability > 50%
- Calculate ATR-based stop loss and R:R ≥ 1.5
- State the primary catalyst (technical or fundamental)
- Classify timeframe: scalp (<4h), intraday (1d), swing (2–5d)

Return ONLY valid JSON matching this schema: { "recommendations": { "stocks": [...], ... } }
```

**Output schema per item**:
```json
{
  "ticker": "AAPL",
  "direction": "long",
  "entry_price": 185.20,
  "stop_loss": 181.40,
  "take_profit": 194.60,
  "confidence": 68,
  "risk_reward": 2.5,
  "timeframe": "swing",
  "rationale": "Bullish hammer on daily + earnings beat catalyst. Volume surge above VWAP.",
  "catalysts": ["earnings_beat", "volume_breakout"]
}
```

#### Ticker Watch Analysis (auto on new news)

Triggered when: new news item has `asset_tags` matching selected ticker.

**Narrower prompt**:
```
A new development has emerged for {ticker}. Based on the updated context,
re-assess the trade:
- Current position: {direction} at {entry_price}, SL {sl}, TP {tp}
- Previous confidence: {confidence}%

Determine:
1. Updated confidence (0-100)
2. Has the directional thesis changed? (yes/no)
3. Recommended action: hold | tighten_sl | exit | add
4. Suggested new SL/TP if changed
5. One-sentence assessment

Return JSON: { "confidence": N, "direction_flip": bool, "action": "...", "suggested_sl": N, "suggested_tp": N, "assessment": "..." }
```

---

## 9. Selected Trade Tracker

### Implementation: `app/live_trading/trade_watcher.py`

**Key behaviors**:

1. **Persistence**: `data/live_selected_trade.json` — survives restarts, page refreshes, analysis refreshes.

2. **Auto-analysis on news**: When `DataStreamEngine` fires a news event, `TradeWatcher` checks if `item.asset_tags` contains the selected ticker. If yes → calls `stream_processor.analyze_ticker()` → result saved to `live_trade_updates` → pushed to WebSocket clients.

3. **WS push**: All connected WebSocket clients receive `type: "analysis_update"` message with the new confidence, action, and SL/TP suggestions.

4. **UI behavior**:
   - Selected trade panel is always visible at the top of the page regardless of which category tab is shown
   - Analysis refreshes (button press) also re-analyze the selected ticker as part of batch
   - "Deselect" button clears state; panel collapses

5. **Trade watcher does NOT**:
   - Execute orders (Trading API is placeholder)
   - Modify existing `active_trades` table (fully independent)

---

## 10. UI Page — Live Trading

### Layout

```
┌─────────────────────────── NAV BAR ─────────────────────────────────┐
│ Configuration | Recommendations | ... | [Live Trading] | Backtest | ...│
└─────────────────────────────────────────────────────────────────────┘

┌──── SELECTED TRADE PANEL (always visible if trade selected) ─────────┐
│ ● AAPL  LONG  $185.20  ▲ +0.42%   Confidence: 74%  Action: HOLD     │
│ SL: $181.40   TP: $194.60   R:R 2.5x   Last update: 2m ago           │
│ "Bullish thesis intact. No directional flip from latest Reuters item." │
│ [History ▾]              [Deselect]        [🔌 Connect Broker ···]   │
└─────────────────────────────────────────────────────────────────────┘

┌── CONTROLS ──────────────────────────────────────────────────────────┐
│ [⚡ Analyze Market]   Status: ● Engine running   Last analysis: 5m ago│
│ Watchlist: AAPL TSLA GOLD WTI EUR/USD BTC ...  [Edit watchlist]      │
└─────────────────────────────────────────────────────────────────────┘

┌── CATEGORY TABS ─────────────────────────────────────────────────────┐
│ [Stocks] [Commodities] [Options] [FX] [Crypto] [Indices]             │
└─────────────────────────────────────────────────────────────────────┘

┌── RECOMMENDATIONS GRID (6 cards per tab) ────────────────────────────┐
│ ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                │
│ │ AAPL  LONG   │  │ TSLA  SHORT  │  │ NVDA  LONG   │                │
│ │ $185.20      │  │ $245.00      │  │ $880.00      │                │
│ │ SL: $181     │  │ SL: $252     │  │ SL: $860     │                │
│ │ TP: $195     │  │ TP: $232     │  │ TP: $920     │                │
│ │ Conf: 72%    │  │ Conf: 65%    │  │ Conf: 81%    │                │
│ │ RR: 2.5x     │  │ RR: 2.0x     │  │ RR: 2.0x     │                │
│ │ swing        │  │ intraday     │  │ swing        │                │
│ │ [Watch Trade]│  │ [Watch Trade]│  │ [Watch Trade]│                │
│ └──────────────┘  └──────────────┘  └──────────────┘                │
└─────────────────────────────────────────────────────────────────────┘

┌── LIVE NEWS FEED (bottom panel) ─────────────────────────────────────┐
│ ● 2m  Reuters: Apple raises iPhone prices in Europe — sentiment +0.6  │
│ ● 5m  Bloomberg: Oil slips as demand outlook weakens — sentiment -0.4 │
│ ● 8m  CNBC: Fed holds rates steady — sentiment +0.1                   │
└─────────────────────────────────────────────────────────────────────┘
```

### WebSocket Connection

Page opens WebSocket to `/api/live/ws` on load. Reconnects on disconnect with exponential backoff (1s → 2s → 4s → max 30s).

Price updates: displayed inline on recommendation cards (price badge updates in-place).
News updates: prepended to news feed panel (max 50 visible).
Analysis updates: if selected trade, update the selected trade panel without re-rendering the page.

### Tooltips

As per project rule: every button and card has a hovering tooltip appearing after 3 seconds.

---

## 11. Integration Points (touches existing files)

Only **3 existing files** are modified. All changes are additive (no existing code altered):

### `app/main.py`

```python
# ADD: import
from app.live_trading.live_routes import live_router
from app.live_trading.stream_engine import DataStreamEngine

# ADD: inside lifespan startup (after scheduler.start())
engine = DataStreamEngine()
asyncio.create_task(engine.start())

# ADD: router registration (after backtest_router)
app.include_router(live_router, prefix="/api/live", tags=["live-trading"])

# ADD: page route (after /backtest route)
@app.get("/live-trading", response_class=HTMLResponse)
async def live_trading_page() -> HTMLResponse:
    return HTMLResponse(content=_read_static("live-trading.html"))
```

### All 10 existing HTML files (nav bar only)

In each file's `<nav class="nav">` block, add one line after the active-trades link:
```html
<a href="/live-trading"><span data-icon="zap"></span> Live Trading</a>
```

Files: `dashboard.html`, `recommendations.html`, `trade-builder.html`, `research.html`, `analysis.html`, `active-trades.html`, `backtest.html`, `journal.html`, `briefing.html`, `calendar.html`

### `requirements.txt`

```
sse-starlette>=1.6.5        # SSE fallback for WebSocket
websockets>=12.0             # WebSocket support (already pulled by uvicorn, but explicit)
```

> `yfinance`, `feedparser`, `anthropic`, `openai` are already present. No other new packages needed.

---

## 12. Technical Decisions & Recommendations

### Decision 1: Price polling vs true WebSocket market data

**Problem**: yfinance has no WebSocket API. True real-time (sub-100ms) requires a paid broker/data provider.

**Recommendation**: Poll yfinance every 5 seconds. This is acceptable for intraday/swing trading. The architecture is designed so the `DataStreamEngine._price_poll_loop()` is the only place to swap this out — a real WebSocket provider can be added later with zero changes to routing or UI.

### Decision 2: SSE vs WebSocket

**Recommendation**: Implement **WebSocket** as primary (better for bidirectional, reconnection). Add SSE `/api/live/stream` as fallback. SSE is simpler to implement as fallback using `sse-starlette` (1 line in requirements.txt already referenced).

### Decision 3: LLM analysis — synchronous vs streaming

**Recommendation**: Use **streaming LLM response** (Anthropic/OpenAI both support `stream=True`). Push partial text to the browser via the WebSocket. This gives the user visual feedback that analysis is running and prevents a blank "loading" state for 10–30 seconds.

### Decision 4: News detection — new table vs reuse `feed_items`

**Recommendation**: Use a **separate `live_news_items` table** with 7-day TTL. Reasons:
- `feed_items` grows large and is a TimescaleDB hypertable for a different purpose
- Live trading needs fast dedup on `id` only (no sentiment scoring pipeline)
- Clean separation means we can purge live news aggressively without affecting the daily batch

### Decision 5: Selected trade persistence — file vs DB

**Recommendation**: **JSON file** (`data/live_selected_trade.json`). Reasons:
- Exactly one selected trade at a time → no query needed
- Consistent with how existing app handles `preferences.json` and `platt.json`
- Instant read on startup

### Decision 6: LLM prompt — output validation

**Recommendation**: Validate LLM JSON output with a strict Pydantic model before accepting. If JSON parse fails → retry once with "Return ONLY valid JSON, no markdown" injected. If retry fails → return last known good analysis with a warning flag in the UI.

### Decision 7: Categories for analysis

**Recommendation**: `stocks`, `commodities`, `options`, `fx`, `crypto`, `indices` — 6 categories × 6 items = 36 max recommendations per analysis run. Options analysis will focus on directional options plays (not complex spreads) given LLM limitations.

---

## 13. Implementation Phases

### Phase 1 — Foundation & Routing (no LLM yet)

- [ ] Create `app/live_trading/` module
- [ ] Create `app/live_trading/live_routes.py` with stub endpoints
- [ ] Create `app/live_trading/trading_api.py` (placeholder)
- [ ] Create `app/static/live-trading.html` with nav bar + empty layout
- [ ] Register router + page route in `app/main.py`
- [ ] Add "Live Trading" link to all 10 existing nav bars
- [ ] Add new DB models (`live_news_items`, `live_analysis_runs`, `live_recommendations`, `live_trade_updates`)
- [ ] Add DB tables to `init_db()`
- [ ] Verify: page loads, nav works, `/api/live/status` returns 200
- [ ] `make build start`

### Phase 2 — Data Stream Engine

- [ ] Implement `stream_engine.py`: RSS news polling (30s), dedup, save to `live_news_items`
- [ ] Implement price polling (5s) via yfinance for watchlist
- [ ] WebSocket endpoint `/api/live/ws`: connect, broadcast price ticks
- [ ] `/api/live/watchlist` GET + POST
- [ ] UI: live news feed panel (WebSocket-driven)
- [ ] UI: price badge on cards updates in real time
- [ ] `make build start`

### Phase 3 — LLM Analysis (Stream Processing)

- [ ] Implement `stream_processor.py`:
  - Build context snapshot
  - Full batch analysis (quant/trader prompt)
  - Parse + validate JSON output
  - Save to `live_analysis_runs` + `live_recommendations`
- [ ] POST `/api/live/analyze` endpoint
- [ ] GET `/api/live/analysis/latest` endpoint
- [ ] GET `/api/live/recommendations` endpoint
- [ ] UI: "Analyze Market" button + loading state
- [ ] UI: Recommendation cards rendered per category tab
- [ ] Streaming LLM response pushed via WebSocket
- [ ] `make build start`

### Phase 4 — Selected Trade + Auto Watch

- [ ] Implement `trade_watcher.py`: persist, reload on startup, news trigger
- [ ] Ticker watch LLM prompt (narrow re-analysis on news)
- [ ] Save updates to `live_trade_updates`
- [ ] POST/GET/DELETE `/api/live/selected-trade` endpoints
- [ ] WebSocket `analysis_update` message type
- [ ] UI: Selected trade panel (pinned, persists refresh)
- [ ] UI: Trade history panel
- [ ] UI: "Watch Trade" button on cards; "Deselect" button on panel
- [ ] `make build start`

### Phase 5 — Polish & Trading API Placeholder

- [ ] Trading API placeholder UI section ("Connect Broker" greyed out)
- [ ] Tooltips (3s hover) on all new buttons + cards
- [ ] Error states: LLM failure banner, WebSocket disconnect indicator
- [ ] Analysis history page/modal
- [ ] `make build start`

---

## 14. Risks & Constraints

| Risk | Severity | Mitigation |
|------|----------|------------|
| yfinance rate limiting at 5s poll | Medium | Back off to 15s if 429 received; implement exponential backoff |
| LLM analysis takes 15–30s | Medium | Stream partial response via WebSocket; show spinner with elapsed time |
| LLM returns malformed JSON | Medium | Pydantic validation with retry + fallback to last known good |
| Too many WebSocket clients (memory) | Low | Cap at 20 concurrent connections; queue-based fan-out |
| News-triggered full batch floods LLM | Medium | Debounce: max 1 `news_refresh` per 3 minutes; `ticker_watch` debounced separately per ticker (5 min) |
| yfinance doesn't have real-time options data | High | Options analysis uses equity price + implied volatility estimate only; clearly noted in UI as "estimated" |
| App startup latency increases | Low | Stream engine starts async; doesn't block FastAPI startup |
| DB hypertable creation fails on SQLite | Medium | `live_news_items` has a `CREATE TABLE IF NOT EXISTS` fallback without TimescaleDB extension |

---

## 15. Architectural Solutions

Soluții concrete pentru toate concerns-urile identificate în analiza arhitecturală.

---

### S1 — feedparser + yfinance blochează event loop-ul

**Problemă**: Ambele sunt biblioteci sincrone. Apelate direct într-o corutină asyncio, blochează întregul event loop FastAPI.

**Soluție**: `asyncio.to_thread()` (Python 3.11, confirmat în Dockerfile). Wrapper async pentru fiecare apel blocant:

```python
# stream_engine.py
import asyncio
from app.services.ingestion.rss import fetch_all_feeds

async def _fetch_feeds_async() -> list[dict]:
    return await asyncio.to_thread(fetch_all_feeds)

async def _fetch_prices_async(tickers: list[str]) -> dict[str, dict]:
    def _sync_fetch():
        import yfinance as yf
        result = {}
        for sym in tickers:
            try:
                fi = yf.Ticker(sym).fast_info
                prev_close = fi.get("previous_close") or 1
                last = fi.get("last_price") or 0
                result[sym] = {
                    "price": last,
                    "volume": fi.get("last_volume"),
                    "change_pct": round((last - prev_close) / prev_close * 100, 2),
                }
            except Exception:
                pass
        return result
    return await asyncio.to_thread(_sync_fetch)
```

`yf.Ticker.fast_info` este mai rapid și mai curent decât `yf.download()` cu `interval="1m"`. Aceeași abordare pe care `technicals.py` o folosea deja cu `ThreadPoolExecutor` — noi o facem mai curat cu `to_thread`.

---

### S2 — `seen_news_ids` memory leak

**Problemă**: Set-ul crește nelimitat pe toată durata de viață a procesului.

**Soluție**: `BoundedNewsCache` — dict cu TTL. Pruning automat la fiecare ciclu de poll:

```python
# stream_engine.py
from datetime import datetime, timedelta, timezone

class BoundedNewsCache:
    def __init__(self, ttl_hours: int = 24):
        self._store: dict[str, datetime] = {}
        self._ttl = timedelta(hours=ttl_hours)

    def __contains__(self, item_id: str) -> bool:
        return item_id in self._store

    def add(self, item_id: str) -> None:
        self._store[item_id] = datetime.now(timezone.utc)

    def prune(self) -> int:
        cutoff = datetime.now(timezone.utc) - self._ttl
        before = len(self._store)
        self._store = {k: v for k, v in self._store.items() if v > cutoff}
        return before - len(self._store)  # nr. de intrări eliminate
```

La startup, cache-ul se populează din DB cu ultimele 24h de ID-uri. Memory usage devine O(articole_per_24h) — stabil, nu crește necontrolat.

---

### S3 — Engine task moare silențios fără restart

**Problemă**: `asyncio.create_task(engine.start())` — dacă coroutine-ul aruncă orice excepție, task-ul dispare. Niciun log vizibil, nicio repornire.

**Soluție**: Supervisor pattern cu exponential backoff:

```python
# stream_engine.py
async def _supervised(coro_factory, name: str) -> None:
    """Restarts a coroutine on failure with exponential backoff. Never exits."""
    delay = 1.0
    while True:
        try:
            await coro_factory()
            delay = 1.0  # reset la ieșire curată
        except asyncio.CancelledError:
            raise  # shutdown explicit — nu restart
        except Exception as exc:
            logger.error("[LIVE_ENGINE] %s crashed: %s — restart in %.0fs", name, exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)  # backoff până la 5 minute

# În DataStreamEngine.start():
async def start(self) -> None:
    await asyncio.gather(
        _supervised(self._news_poll_loop, "news_poll"),
        _supervised(self._price_poll_loop, "price_poll"),
    )
```

Fiecare loop rulează independent. Dacă price poll cade (yfinance timeout), news poll continuă și invers.

---

### S4 — yfinance nu oferă date live (delay ~15 minute)

**Problemă**: Yahoo Finance free tier returnează date cu delay de 15 minute. Nu putem numi asta "live".

**Soluție în 2 pași**:

**4a. Schimbăm sursa de preț**: `yf.Ticker.fast_info["last_price"]` returnează prețul de la ultimul tick disponibil, de obicei mult mai curent decât `download(interval="1m")`. Nu e garantat real-time dar e mai bun.

**4b. UI complet transparent**: Niciunde în UI nu apare cuvântul "live" pentru prețuri. Înlocuit cu:
- Badge permanent: `~ Delayed (Yahoo Finance)`
- Tooltip pe prețuri: "Price data sourced from Yahoo Finance. May be delayed up to 15 minutes. Not suitable for scalping."
- Când Trading API va fi conectat, badge-ul se schimbă automat în `● Live`

Redenumim pagina internal: nu "Live Prices" ci "Market Monitor". Titlul paginii "Live Trading" rămâne (se referă la workflow-ul de trading live, nu la prețuri live).

---

### S5 — Costul LLM la auto-trigger este necontrolat

**Problemă**: 15,000–30,000 tokens/call × multiple calls/oră = zeci de $ pe zi.

**Soluție în 3 niveluri**:

**5a. Comprimăm contextul înainte de LLM**:

Înlocuim "last 50 news items full text" cu un summary pre-procesat:
```python
def _compress_news_context(news_items: list[dict]) -> str:
    # Top 10 by |sentiment_score| — cele mai extreme, nu primele 50
    top = sorted(news_items, key=lambda x: abs(x.get("sentiment_score", 0)), reverse=True)[:10]
    lines = []
    for item in top:
        sign = "+" if item["sentiment_score"] > 0 else ""
        lines.append(f"[{item['source']}] {item['title']} (sentiment: {sign}{item['sentiment_score']:.2f})")
    return "\n".join(lines)
```

Tehnic: `price + ATR% + RSI + trend_direction` per ticker, NU `TechnicalSnapshot` complet. Estimare: 2,000–4,000 tokens input în loc de 15,000–30,000. Cost reduction: 7x.

**5b. Cost tracker în `live_analysis_runs`**:
- Salvăm `llm_prompt_tokens` + `llm_response_tokens` (deja în schema)
- Adăugăm `estimated_cost_usd` (Float) calculat din rate-urile cunoscute per model
- `GET /api/live/status` returnează `{daily_llm_cost_usd, monthly_llm_cost_usd}`
- UI afișează costul acumulat în bara de control

**5c. Budget cap în preferences**:
```json
// data/preferences.json — nou câmp
"live_trading": {
  "daily_llm_budget_usd": 5.0,
  "news_refresh_debounce_minutes": 3,
  "price_poll_interval_seconds": 10
}
```
Când `daily_llm_cost_usd` > `daily_llm_budget_usd` → auto-trigger dezactivat, butonul manual rămâne activ. Banner în UI: "Daily LLM budget reached ($5.00). Manual analysis still available."

---

### S6 — `build_snapshot()` este sincron și lent (36 apeluri seriale)

**Problemă**: 36 tickers × 1–3s per snapshot = 36–108 secunde blocat înainte de LLM.

**Soluție**: Execuție paralelă pe ThreadPoolExecutor + cache cu TTL 60s:

```python
# stream_processor.py
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

_snap_cache: dict[str, tuple] = {}  # ticker → (snapshot, fetched_at)
_snap_executor = ThreadPoolExecutor(max_workers=8)

async def _get_snapshot_cached(ticker: str):
    cached = _snap_cache.get(ticker)
    if cached:
        snap, ts = cached
        if (datetime.now(timezone.utc) - ts).total_seconds() < 60:
            return snap  # cache hit
    
    loop = asyncio.get_event_loop()
    snap = await loop.run_in_executor(_snap_executor, build_snapshot, ticker)
    if snap:
        _snap_cache[ticker] = (snap, datetime.now(timezone.utc))
    return snap

async def _build_context(tickers: list[str]) -> dict:
    # Toate snapshot-urile în paralel
    snapshots = await asyncio.gather(
        *[_get_snapshot_cached(t) for t in tickers],
        return_exceptions=True
    )
    return {t: s for t, s in zip(tickers, snapshots) if s and not isinstance(s, Exception)}
```

Reducere timp: de la 36–108s la 3–10s (8 threads paralel, cache hit = 0ms).

---

### S7 — Race condition: LLM calls concurente

**Problemă**: Buton apăsat în timp ce `news_refresh` rulează = 2 LLM calls concurente, răspunsuri out-of-order.

**Soluție**: `asyncio.Lock` + status broadcast:

```python
# stream_processor.py
class StreamProcessor:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._active_run_id: str | None = None

    async def run_analysis(self, trigger: str, ticker_context: str | None = None) -> str | None:
        if self._lock.locked():
            logger.info("[PROCESSOR] Analysis already running (run_id=%s), skipping %s", 
                        self._active_run_id, trigger)
            return self._active_run_id  # returnăm run-ul curent, nu pornim altul

        async with self._lock:
            run_id = str(uuid.uuid4())
            self._active_run_id = run_id
            await _broadcast({"type": "analysis_started", "run_id": run_id, "trigger": trigger})
            try:
                # ... logică analiză
                return run_id
            finally:
                self._active_run_id = None
                await _broadcast({"type": "analysis_done", "run_id": run_id})
```

UI: butonul "Analyze Market" devine disabled + "Analysis in progress..." cât timp lock-ul e activ. Primit via WebSocket `analysis_started` / `analysis_done`.

---

### S8 — Options fără date reale

**Problemă**: Nu avem options flow, IV real-time, sau Greeks. LLM va hallucina.

**Soluție**: Înlocuim categoria `options` cu `etfs`.

**De ce ETFs sunt mai buni acum**:
- Date complete via yfinance (preț, volum, ATR)
- Acoperă aceeași expunere: TQQQ (3x NASDAQ), GLD (gold), USO (oil), TLT (bonds), UVXY (volatility)
- Nu necesită cont de opțiuni sau cunoștințe avansate de Greeks
- Când Trading API va fi conectat și vom avea date de opțiuni, adăugăm categoria înapoi

Categories finale: `stocks`, `commodities`, `etfs`, `fx`, `crypto`, `indices`

În UI, în tab-ul ETFs: tooltip "Exchange-Traded Funds — directional exposure without options complexity. Options trading will be added when broker integration is live."

---

### S9 — Event loop contention cu APScheduler + engine

**Problemă**: Uvicorn cu `--reload` = single worker. APScheduler + Engine + WebSocket broadcast + LLM calls = totul pe același event loop Python.

**Soluție**: Nu schimbăm workers (incompatibil cu `--reload` și in-memory state). În schimb, ne asigurăm că nimic nu blochează:

**9a. Toate I/O blocante → `asyncio.to_thread()`** (soluțiile S1, S6 rezolvă asta)

**9b. WebSocket fan-out cu timeout per client**:
```python
async def _broadcast(message: dict) -> None:
    if not _ws_clients:
        return
    data = json.dumps(message)
    # Trimite la toți în paralel, cu timeout per client
    await asyncio.gather(
        *[_safe_send(ws, data) for ws in list(_ws_clients)],
        return_exceptions=True
    )

async def _safe_send(ws: WebSocket, data: str) -> None:
    try:
        await asyncio.wait_for(ws.send_text(data), timeout=2.0)
    except (asyncio.TimeoutError, Exception):
        _ws_clients.discard(ws)  # client lent/mort = eliminat
```

**9c. Price poll interval → 10s în loc de 5s**: Reduce cu 50% numărul de apeluri yfinance și broadcasts, fără impact semnificativ pe UX pentru intraday/swing.

**9d. Guard pentru conflict cu `job_daily_batch`**: Dacă APScheduler raportează că `job_daily_batch` rulează, `news_refresh` LLM call este amânat cu 2 minute. Ambele folosesc aceeași resursă LLM și același yfinance.

---

### S10 — Watchlist non-persistent

**Problemă**: `self.watchlist = []` se pierde la restart.

**Soluție**: `data/live_watchlist.json` — același pattern cu `preferences.json`:

```python
# trade_watcher.py (sau un nou live_preferences.py)
DEFAULT_WATCHLIST = ["AAPL", "TSLA", "NVDA", "MSFT", "GC=F", "CL=F", "BTC-USD", "EUR=X", "SPY", "QQQ"]

def get_watchlist() -> list[str]:
    path = Path("data/live_watchlist.json")
    if path.exists():
        return json.loads(path.read_text()).get("tickers", DEFAULT_WATCHLIST)
    return DEFAULT_WATCHLIST

def set_watchlist(tickers: list[str]) -> None:
    Path("data/live_watchlist.json").write_text(json.dumps({"tickers": tickers}))
```

La startup: `engine.watchlist = get_watchlist()`. La `POST /api/live/watchlist`: `set_watchlist(tickers)` + `engine.watchlist = tickers`.

---

### S11 — Price polling 24/7 inutil

**Problemă**: Polling yfinance la 10s la 3 AM sâmbătă pentru acțiuni NYSE este inutilă.

**Soluție**: Market hours aware interval — fără biblioteci externe (`zoneinfo` e built-in Python 3.9+):

```python
# stream_engine.py
from zoneinfo import ZoneInfo
from datetime import datetime, time as dtime

_ET = ZoneInfo("America/New_York")

def _market_state(tickers: list[str]) -> str:
    """Returns current market state for the watchlist."""
    has_crypto = any(sym.endswith("-USD") or sym.endswith("=X") for sym in tickers)
    has_equity = any(not (sym.endswith("-USD") or sym.endswith("=X")) for sym in tickers)

    now = datetime.now(_ET)
    weekday = now.weekday()
    t = now.time()

    # Crypto/FX = 24/7, nu le oprim niciodata
    if has_crypto and not has_equity:
        return "open_24h"

    if weekday >= 5:   # weekend
        return "closed"
    if dtime(9, 30) <= t <= dtime(16, 0):
        return "regular"
    if dtime(8, 30) <= t < dtime(9, 30):
        return "pre_market_warmup"   # fereastra cu 1h înainte de deschidere
    if dtime(4, 0) <= t < dtime(8, 30):
        return "pre_market_early"
    if dtime(16, 0) < t <= dtime(20, 0):
        return "after_hours"
    return "closed"  # 20:00-04:00

def _price_poll_interval(state: str) -> int:
    return {
        "open_24h":           10,
        "regular":            10,
        "pre_market_warmup":  15,
        "after_hours":        30,
        "pre_market_early":  120,
        "closed":            300,   # 5 min — suficient pentru a detecta redeschiderea
    }.get(state, 120)
```

Folosit în `_price_poll_loop`:
```python
state = _market_state(self.watchlist)
await asyncio.sleep(_price_poll_interval(state))
```

---

### S12 — Bloc analiză când piața e închisă + auto-start cu 1h înainte de deschidere

**Decizie**: Analiza LLM (atât `news_refresh` cât și `ticker_watch`) **nu rulează când piața e închisă**. Nu are sens să generăm recomandări pentru NYSE la 3 AM — contextul se schimbă complet până la deschidere.

**Excepție**: Butonul manual `Analyze Market` funcționează **oricând**, indiferent de starea pieței.

**Fereastra de analiză activă** (Mon-Fri, ET):
```
08:30 → 20:00  — analiză activă (include 1h pre-market + sesiune regulară + after-hours)
20:00 → 08:30  — analiză blocată (noapte + weekend)
```

**Implementare — market gate în `stream_processor.py`**:

```python
def _analysis_allowed() -> bool:
    """Returns True if automated analysis is allowed to run."""
    now = datetime.now(_ET)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(8, 30) <= t <= dtime(20, 0)
```

Aplicat în ambele trigger-uri auto:
```python
# news_refresh trigger (în stream_engine._news_poll_loop)
if new_items and not _analysis_lock.locked():
    if _analysis_allowed():
        asyncio.create_task(processor.run_analysis(trigger="news_refresh"))
    # dacă piața e închisă: știrile sunt salvate în DB dar analiza NU pornește

# ticker_watch trigger (în trade_watcher)
if matching_news and _analysis_allowed():
    asyncio.create_task(processor.run_analysis(trigger="ticker_watch", ticker_context=ticker))
```

---

**Auto-trigger la 08:30 ET — Pre-Market Warmup**

La tranziția `closed → pre_market_warmup`, engine-ul detectează că fereastra s-a deschis și declanșează automat o analiză completă, chiar dacă nu a sosit nicio știre nouă:

```python
# stream_engine.py — în _price_poll_loop, după fiecare sleep
prev_state = self._last_market_state
curr_state = _market_state(self.watchlist)

if prev_state == "closed" and curr_state == "pre_market_warmup":
    logger.info("[LIVE_ENGINE] Market window opened (08:30 ET) — triggering pre-market warmup analysis")
    asyncio.create_task(
        processor.run_analysis(trigger="pre_market_warmup")
    )

self._last_market_state = curr_state
```

Trigger nou adăugat în tabelul LLM call modes:

| Mode | Trigger | Debounce | Output |
|------|---------|----------|--------|
| `full_batch` | Analyze button (manual) | Niciunul — merge oricând | 6 per categorie × N categorii |
| `pre_market_warmup` | Tranziție `closed → pre_market_warmup` la 08:30 ET | O singură dată per zi | 6 per categorie × N categorii |
| `news_refresh` | Știre nouă detectată (doar în fereastra 08:30–20:00) | 3 min | 6 per categorie × N categorii |
| `ticker_watch` | Știre matching ticker selectat (doar în fereastra 08:30–20:00) | 5 min per ticker | Update pentru 1 ticker |

**UI — Market Status indicator**:

```
● OPEN   Regular Session (closes in 2h 15m)
● WARMUP Pre-Market (opens in 45m) — analysis active
◌ CLOSED Market closed — analysis paused until 08:30 ET
```

Când piața e `closed`: butonul "Analyze Market" rămâne activ (cu tooltip "Market is closed. Manual analysis still available."), dar auto-trigger-urile sunt suspendate vizibil în UI cu un banner:

```
⏸ Auto-analysis paused — market closed. Resumes at 08:30 ET (in 6h 12m).
```

**Crypto/FX exception**: Dacă watchlist-ul conține **doar** crypto sau FX (`BTC-USD`, `ETH-USD`, `EUR=X`), `_analysis_allowed()` returnează `True` mereu — aceste piețe nu au market hours.

---

### S13 — Context snapshot nesalvat (debugging imposibil)

**Problemă**: Nu știm ce context a primit LLM-ul când a generat o recomandare. Nu putem reproduce sau debug.

**Soluție**: Adăugăm `context_snapshot_json` (Text, nullable) la `live_analysis_runs`:

```python
# La construirea run-ului
context = {
    "news_summary": _compress_news_context(news_items),
    "prices": {t: engine.latest_prices.get(t) for t in watchlist},
    "technicals": {t: _snap_to_dict(snap) for t, snap in snapshots.items()},
    "regime": regime_summary,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "market_session": _get_market_session(),
}
run.context_snapshot_json = json.dumps(context)
```

Câmpul nu e expus în UI principal, dar `GET /api/live/analysis/history` îl include opțional cu `?include_context=true`. Util pentru debugging și viitor fine-tuning al prompturilor.

---

### Rezumat soluții și impact în plan

| # | Concern | Soluție | Fișier afectat |
|---|---------|---------|----------------|
| S1 | Sync blocking | `asyncio.to_thread()` wrappers | `stream_engine.py` |
| S2 | Memory leak | `BoundedNewsCache` cu TTL 24h | `stream_engine.py` |
| S3 | Task moare silențios | Supervisor cu exponential backoff | `stream_engine.py` |
| S4 | Prețuri delay 15min | `fast_info` + UI disclaimer explicit | `stream_engine.py` + UI |
| S5 | Cost LLM necontrolat | Context comprimat + budget cap + cost tracker | `stream_processor.py` + preferences |
| S6 | Snapshots lente seriale | Paralel ThreadPoolExecutor + cache 60s | `stream_processor.py` |
| S7 | Race condition LLM | `asyncio.Lock` + status broadcast WS | `stream_processor.py` |
| S8 | Options fără date | Înlocuit cu `etfs` | schema + UI |
| S9 | Event loop contention | `to_thread` + WS timeout + poll 10s + APScheduler guard | `stream_engine.py` + `live_routes.py` |
| S10 | Watchlist nepersistent | `data/live_watchlist.json` | `trade_watcher.py` |
| S11 | Polling 24/7 | Interval adaptat per stare de piață (10s–300s) | `stream_engine.py` |
| S12 | Analiză când piața e închisă | Gate 08:30–20:00 ET + auto-warmup la 08:30 + crypto excepție | `stream_processor.py` + `stream_engine.py` + UI |
| S13 | Context nesalvat | `context_snapshot_json` în DB | schema + `stream_processor.py` |

---

> **Next step**: Approve this plan, then implementation begins with Phase 1.
> Command to rebuild after changes: `make build start`
