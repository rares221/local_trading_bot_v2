"""
Data Stream Engine — Live Trading.

Runs two supervised background loops:
  _news_poll_loop  — polls RSS feeds every 30s, detects new articles, broadcasts to WS clients
  _price_poll_loop — polls yfinance prices at market-aware intervals, broadcasts to WS clients

Key design decisions:
  - All blocking I/O (feedparser, yfinance) runs via asyncio.to_thread() — never blocks the event loop
  - BoundedNewsCache keeps seen IDs bounded to last 24h to prevent memory growth
  - Market-aware poll intervals: 10s during regular session, up to 300s when closed
  - Pre-market warmup trigger fires once at 08:30 ET to kick off analysis before open
  - Supervised loops restart automatically with exponential backoff on failure
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone, time as dtime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_vader = SentimentIntensityAnalyzer()

# ── WebSocket connection manager ──────────────────────────────────────────────

class ConnectionManager:
    """Thread-safe WebSocket fan-out with per-client send timeout."""

    def __init__(self) -> None:
        self._clients: set = set()

    def connect(self, ws) -> None:
        self._clients.add(ws)

    def disconnect(self, ws) -> None:
        self._clients.discard(ws)

    @property
    def count(self) -> int:
        return len(self._clients)

    async def broadcast(self, message: dict) -> None:
        if not self._clients:
            return
        data = json.dumps(message, default=str)
        await asyncio.gather(
            *[self._safe_send(ws, data) for ws in list(self._clients)],
            return_exceptions=True,
        )

    async def _safe_send(self, ws, data: str) -> None:
        try:
            await asyncio.wait_for(ws.send_text(data), timeout=2.0)
        except Exception:
            self._clients.discard(ws)


ws_manager = ConnectionManager()


# ── Bounded news ID cache ─────────────────────────────────────────────────────

class BoundedNewsCache:
    """Keeps article IDs seen in the last `ttl_hours` hours. Prevents unbounded growth."""

    def __init__(self, ttl_hours: int = 24) -> None:
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
        return before - len(self._store)

    def load_from_ids(self, ids: list[str]) -> None:
        now = datetime.now(timezone.utc)
        for id_ in ids:
            self._store[id_] = now


# ── Market session helpers ────────────────────────────────────────────────────

def get_market_state(tickers: list[str]) -> str:
    """
    Returns current market state string based on ET time and watchlist composition.

    States:
      open_24h           — crypto/FX only watchlist, always open
      regular            — NYSE/NASDAQ regular session 09:30-16:00 ET Mon-Fri
      pre_market_warmup  — 08:30-09:30 ET Mon-Fri (analysis window opens 1h before open)
      pre_market_early   — 04:00-08:30 ET Mon-Fri
      after_hours        — 16:00-20:00 ET Mon-Fri
      closed             — all other times
    """
    has_crypto_fx = any(
        sym.endswith("-USD") or sym.endswith("-USDT") or sym.endswith("=X")
        for sym in tickers
    )
    has_equity = any(
        not (sym.endswith("-USD") or sym.endswith("-USDT") or sym.endswith("=X"))
        for sym in tickers
    )

    if has_crypto_fx and not has_equity:
        return "open_24h"

    now = datetime.now(_ET)
    weekday = now.weekday()
    t = now.time()

    if weekday >= 5:
        return "closed"
    if dtime(9, 30) <= t <= dtime(16, 0):
        return "regular"
    if dtime(8, 30) <= t < dtime(9, 30):
        return "pre_market_warmup"
    if dtime(4, 0) <= t < dtime(8, 30):
        return "pre_market_early"
    if dtime(16, 0) < t <= dtime(20, 0):
        return "after_hours"
    return "closed"


def analysis_allowed(tickers: list[str] | None = None) -> bool:
    """
    Returns True if automated LLM analysis is allowed to run.
    Window: Mon-Fri 08:30-20:00 ET. Crypto/FX-only watchlists: always True.
    Manual button always bypasses this gate.
    """
    tickers = tickers or []
    has_crypto_fx = any(
        sym.endswith("-USD") or sym.endswith("-USDT") or sym.endswith("=X")
        for sym in tickers
    )
    has_equity = any(
        not (sym.endswith("-USD") or sym.endswith("-USDT") or sym.endswith("=X"))
        for sym in tickers
    )
    if has_crypto_fx and not has_equity:
        return True

    now = datetime.now(_ET)
    if now.weekday() >= 5:
        return False
    return dtime(8, 30) <= now.time() <= dtime(20, 0)


_POLL_INTERVAL: dict[str, int] = {
    "open_24h": 10,
    "regular": 10,
    "pre_market_warmup": 15,
    "after_hours": 30,
    "pre_market_early": 120,
    "closed": 300,
}


# ── Supervised task runner ────────────────────────────────────────────────────

async def _supervised(coro_factory, name: str) -> None:
    """Restarts a coroutine on failure with exponential backoff. Never exits."""
    delay = 1.0
    while True:
        try:
            await coro_factory()
            delay = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[LIVE_ENGINE] %s crashed: %s — restart in %.0fs", name, exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


# ── Main engine ───────────────────────────────────────────────────────────────

class DataStreamEngine:
    """
    Central data ingestion engine for Live Trading.
    Started once at app startup via asyncio.create_task(engine.start()).
    """

    def __init__(self) -> None:
        self.watchlist: list[str] = []
        self.latest_prices: dict[str, dict] = {}
        self._news_cache = BoundedNewsCache(ttl_hours=24)
        self._last_market_state: str = "closed"
        self._running = False
        # Callback injected by StreamProcessor after it's initialised
        self._on_new_news = None        # async callable(item: dict)
        self._on_warmup_trigger = None  # async callable()

    def set_news_callback(self, cb) -> None:
        self._on_new_news = cb

    def set_warmup_callback(self, cb) -> None:
        self._on_warmup_trigger = cb

    async def start(self) -> None:
        self._running = True
        logger.info("[LIVE_ENGINE] Starting — watchlist=%s", self.watchlist)
        await self._load_seen_ids_from_db()
        await asyncio.gather(
            _supervised(self._news_poll_loop, "news_poll"),
            _supervised(self._price_poll_loop, "price_poll"),
        )

    async def _load_seen_ids_from_db(self) -> None:
        """Pre-populate news cache from DB to survive restarts."""
        try:
            from app.core.database import AsyncSessionLocal
            from app.live_trading.models import LiveNewsItem
            from sqlalchemy import select
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(LiveNewsItem.id).where(LiveNewsItem.fetched_at >= cutoff)
                )
                ids = [row[0] for row in result.fetchall()]
                self._news_cache.load_from_ids(ids)
                logger.info("[LIVE_ENGINE] Loaded %d seen news IDs from DB", len(ids))
        except Exception as exc:
            logger.warning("[LIVE_ENGINE] Could not load seen IDs from DB: %s", exc)

    # ── News loop ─────────────────────────────────────────────────────────────

    async def _news_poll_loop(self) -> None:
        while True:
            try:
                await self._poll_news_once()
            except Exception as exc:
                logger.warning("[LIVE_ENGINE] News poll error: %s", exc)
            self._news_cache.prune()
            await asyncio.sleep(30)

    async def _poll_news_once(self) -> None:
        from app.services.ingestion.rss import fetch_all_feeds
        items: list[dict] = await asyncio.to_thread(fetch_all_feeds)
        new_items = [it for it in items if it["id"] not in self._news_cache]
        if not new_items:
            return

        await self._save_live_news(new_items)
        for item in new_items:
            self._news_cache.add(item["id"])

        # Broadcast each new article to WS clients
        for item in new_items:
            tags = item.get("asset_tags") or []
            if isinstance(tags, str):
                try:
                    import json as _json
                    tags = _json.loads(tags)
                except Exception:
                    tags = []
            sentiment = item.get("sentiment_score") or 0.0
            await ws_manager.broadcast({
                "type": "news",
                "headline": (item.get("title") or "")[:120],
                "source": item.get("source", ""),
                "sentiment": round(float(sentiment), 2),
                "asset_tags": tags,
                "published": str(item.get("published", "")),
            })

        # Notify stream processor
        if self._on_new_news:
            for item in new_items:
                try:
                    await self._on_new_news(item)
                except Exception as exc:
                    logger.warning("[LIVE_ENGINE] news callback error: %s", exc)

        logger.info("[LIVE_ENGINE] %d new articles ingested", len(new_items))

    async def _save_live_news(self, items: list[dict]) -> None:
        try:
            from app.core.database import AsyncSessionLocal
            from app.live_trading.models import LiveNewsItem
            from sqlalchemy import select
            from app.services.sentiment.ticker_detection import tag_assets

            now = datetime.now(timezone.utc)
            async with AsyncSessionLocal() as session:
                ids = [it["id"] for it in items]
                result = await session.execute(
                    select(LiveNewsItem.id).where(LiveNewsItem.id.in_(ids))
                )
                existing = {row[0] for row in result.fetchall()}

                for it in items:
                    if it["id"] in existing:
                        continue
                    text = (it.get("title") or "") + " " + (it.get("description") or "")
                    sentiment = float(_vader.polarity_scores(text).get("compound", 0.0))
                    try:
                        tags = await asyncio.to_thread(tag_assets, text)
                    except Exception:
                        tags = []
                    session.add(LiveNewsItem(
                        id=it["id"],
                        title=(it.get("title") or "")[:500],
                        link=(it.get("link") or "")[:2048],
                        published=it.get("published"),
                        fetched_at=now,
                        source=(it.get("source") or "")[:256],
                        category=(it.get("category") or "")[:64],
                        asset_tags=json.dumps(tags),
                        sentiment_score=sentiment,
                    ))
                await session.commit()
        except Exception as exc:
            logger.warning("[LIVE_ENGINE] Failed to save live news: %s", exc)

    # ── Price loop ────────────────────────────────────────────────────────────

    async def _price_poll_loop(self) -> None:
        while True:
            state = get_market_state(self.watchlist)
            interval = _POLL_INTERVAL.get(state, 120)

            # Detect closed → pre_market_warmup transition → fire warmup analysis
            if self._last_market_state == "closed" and state == "pre_market_warmup":
                logger.info("[LIVE_ENGINE] Market window opened (08:30 ET) — pre-market warmup")
                if self._on_warmup_trigger:
                    try:
                        asyncio.create_task(self._on_warmup_trigger())
                    except Exception as exc:
                        logger.warning("[LIVE_ENGINE] Warmup trigger error: %s", exc)
            self._last_market_state = state

            # Broadcast market state to UI
            await ws_manager.broadcast({"type": "market_state", "state": state})

            # Fetch prices if watchlist non-empty and market not fully closed
            if self.watchlist and state != "closed":
                try:
                    prices = await asyncio.to_thread(self._fetch_prices_sync, self.watchlist)
                    self.latest_prices.update(prices)
                    for ticker, data in prices.items():
                        await ws_manager.broadcast({"type": "price", "ticker": ticker, **data})
                except Exception as exc:
                    logger.warning("[LIVE_ENGINE] Price fetch error: %s", exc)

            await asyncio.sleep(interval)

    @staticmethod
    def _fetch_prices_sync(tickers: list[str]) -> dict[str, dict]:
        """Blocking yfinance price fetch — called via asyncio.to_thread."""
        import yfinance as yf
        result = {}
        for sym in tickers:
            try:
                fi = yf.Ticker(sym).fast_info
                last = float(fi.get("last_price") or 0)
                prev = float(fi.get("previous_close") or last or 1)
                volume = fi.get("last_volume")
                result[sym] = {
                    "price": round(last, 4),
                    "volume": int(volume) if volume else None,
                    "change_pct": round((last - prev) / prev * 100, 2) if prev else 0.0,
                }
            except Exception:
                pass
        return result


# ── Module-level singleton ────────────────────────────────────────────────────

engine = DataStreamEngine()
