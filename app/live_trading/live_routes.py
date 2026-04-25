"""
Live Trading API routes + WebSocket endpoint.

All routes prefixed /api/live (registered in main.py).
WebSocket: /api/live/ws
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

import pandas as pd

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.live_trading.stream_engine import engine as stream_engine, ws_manager, get_market_state, analysis_allowed
from app.live_trading.stream_processor import processor
from app.live_trading.trade_watcher import watcher, get_watchlist, set_watchlist
from app.live_trading import trading_api

logger = logging.getLogger(__name__)
live_router = APIRouter()


# ── WebSocket ─────────────────────────────────────────────────────────────────

@live_router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    ws_manager.connect(ws)
    try:
        # Send initial state on connect
        await ws.send_text(json.dumps({
            "type": "init",
            "market_state": get_market_state(stream_engine.watchlist),
            "analysis_allowed": analysis_allowed(stream_engine.watchlist),
            "watchlist": stream_engine.watchlist,
            "prices": stream_engine.latest_prices,
            "selected_trade": watcher.get(),
            "connected_clients": ws_manager.count,
        }))
        # Keep alive — wait for client disconnect
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        ws_manager.disconnect(ws)


# ── Status ────────────────────────────────────────────────────────────────────

@live_router.get("/status")
async def get_status():
    daily_cost = await processor.get_daily_cost()
    return {
        "engine_running": stream_engine._running,
        "connected_clients": ws_manager.count,
        "tickers_watched": stream_engine.watchlist,
        "market_state": get_market_state(stream_engine.watchlist),
        "analysis_allowed": analysis_allowed(stream_engine.watchlist),
        "analysis_in_progress": processor._lock.locked(),
        "active_run_id": processor._active_run_id,
        "daily_llm_cost_usd": daily_cost,
        "selected_trade": watcher.get(),
    }


# ── Watchlist ─────────────────────────────────────────────────────────────────

class WatchlistBody(BaseModel):
    tickers: list[str]


@live_router.get("/watchlist")
async def get_watchlist_route():
    return {"tickers": stream_engine.watchlist}


@live_router.post("/watchlist")
async def set_watchlist_route(body: WatchlistBody):
    tickers = [t.strip().upper() for t in body.tickers if t.strip()][:30]
    set_watchlist(tickers)
    stream_engine.watchlist = tickers
    return {"tickers": tickers}


# ── Analysis ──────────────────────────────────────────────────────────────────

@live_router.post("/analyze")
async def trigger_analysis():
    """Manually trigger full LLM batch analysis. Always runs regardless of market hours."""
    import asyncio
    if processor._lock.locked():
        return {
            "status": "already_running",
            "run_id": processor._active_run_id,
            "message": "Analysis already in progress.",
        }
    run_id_holder = {}

    async def _run():
        rid = await processor.run_full_analysis(trigger="manual_button")
        run_id_holder["run_id"] = rid

    asyncio.create_task(_run())
    return {"status": "started", "message": "Analysis started in background."}


@live_router.get("/analysis/latest")
async def get_latest_analysis():
    from app.core.database import AsyncSessionLocal
    from app.live_trading.models import LiveAnalysisRun, LiveRecommendation
    from sqlalchemy import select, desc
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LiveAnalysisRun).order_by(desc(LiveAnalysisRun.created_at)).limit(1)
        )
        run = result.scalar_one_or_none()
        if not run:
            return {"run": None, "recommendations": {}}

        recs_result = await session.execute(
            select(LiveRecommendation).where(LiveRecommendation.run_id == run.id)
        )
        recs = recs_result.scalars().all()

        by_category: dict[str, list] = {}
        for r in recs:
            cat = r.asset_category
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append({
                "id": r.id,
                "ticker": r.ticker,
                "asset_category": r.asset_category,
                "direction": r.direction,
                "entry_price": r.entry_price,
                "stop_loss": r.stop_loss,
                "take_profit": r.take_profit,
                "llm_stop_loss": r.llm_stop_loss,
                "llm_take_profit": r.llm_take_profit,
                "confidence": r.confidence,
                "risk_reward": r.risk_reward,
                "rationale": r.rationale,
                "timeframe": r.timeframe,
                "catalysts": json.loads(r.catalysts or "[]"),
                "market_session": r.market_session,
            })

        return {
            "run": {
                "id": run.id,
                "created_at": run.created_at.isoformat(),
                "trigger": run.trigger,
                "market_session": run.market_session,
                "provider": run.provider,
                "model": run.model,
                "estimated_cost_usd": run.estimated_cost_usd,
                "parsed_ok": run.parsed_ok,
            },
            "recommendations": by_category,
        }


@live_router.get("/analysis/history")
async def get_analysis_history(limit: int = 10):
    from app.core.database import AsyncSessionLocal
    from app.live_trading.models import LiveAnalysisRun
    from sqlalchemy import select, desc
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LiveAnalysisRun).order_by(desc(LiveAnalysisRun.created_at)).limit(limit)
        )
        runs = result.scalars().all()
        return {
            "runs": [
                {
                    "id": r.id,
                    "created_at": r.created_at.isoformat(),
                    "trigger": r.trigger,
                    "market_session": r.market_session,
                    "provider": r.provider,
                    "model": r.model,
                    "estimated_cost_usd": r.estimated_cost_usd,
                    "parsed_ok": r.parsed_ok,
                }
                for r in runs
            ]
        }


# ── Recommendations ───────────────────────────────────────────────────────────

@live_router.get("/recommendations")
async def get_recommendations(run_id: Optional[str] = None, category: Optional[str] = None):
    from app.core.database import AsyncSessionLocal
    from app.live_trading.models import LiveAnalysisRun, LiveRecommendation
    from sqlalchemy import select, desc
    async with AsyncSessionLocal() as session:
        if not run_id:
            result = await session.execute(
                select(LiveAnalysisRun.id).order_by(desc(LiveAnalysisRun.created_at)).limit(1)
            )
            row = result.scalar_one_or_none()
            run_id = row if row else None

        if not run_id:
            return {"items": []}

        q = select(LiveRecommendation).where(LiveRecommendation.run_id == run_id)
        if category:
            q = q.where(LiveRecommendation.asset_category == category)
        result = await session.execute(q)
        recs = result.scalars().all()
        return {
            "items": [
                {
                    "id": r.id,
                    "ticker": r.ticker,
                    "asset_category": r.asset_category,
                    "direction": r.direction,
                    "entry_price": r.entry_price,
                    "stop_loss": r.stop_loss,
                    "take_profit": r.take_profit,
                    "llm_stop_loss": r.llm_stop_loss,
                    "llm_take_profit": r.llm_take_profit,
                    "confidence": r.confidence,
                    "risk_reward": r.risk_reward,
                    "rationale": r.rationale,
                    "timeframe": r.timeframe,
                    "catalysts": json.loads(r.catalysts or "[]"),
                    "market_session": r.market_session,
                }
                for r in recs
            ]
        }


# ── Selected Trade ────────────────────────────────────────────────────────────

class SelectTradeBody(BaseModel):
    ticker: str
    category: str
    entry_price: Optional[float] = None
    direction: str = "long"
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    confidence: Optional[int] = None


@live_router.get("/selected-trade")
async def get_selected_trade():
    trade = watcher.get()
    if not trade:
        return {"trade": None, "updates": []}

    # Fetch last 5 updates
    updates = []
    try:
        from app.core.database import AsyncSessionLocal
        from app.live_trading.models import LiveTradeUpdate
        from sqlalchemy import select, desc
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(LiveTradeUpdate)
                .where(LiveTradeUpdate.ticker == trade["ticker"])
                .order_by(desc(LiveTradeUpdate.created_at))
                .limit(5)
            )
            rows = result.scalars().all()
            updates = [
                {
                    "id": r.id,
                    "created_at": r.created_at.isoformat(),
                    "trigger_headline": r.trigger_headline,
                    "confidence_before": r.confidence_before,
                    "confidence_after": r.confidence_after,
                    "direction_flip": r.direction_flip,
                    "suggested_sl": r.suggested_sl,
                    "suggested_tp": r.suggested_tp,
                    "assessment": r.assessment,
                    "action_advised": r.action_advised,
                }
                for r in rows
            ]
    except Exception:
        pass

    return {"trade": trade, "updates": updates}


@live_router.post("/selected-trade")
async def select_trade(body: SelectTradeBody):
    trade = watcher.set(
        ticker=body.ticker.upper(),
        category=body.category,
        entry_price=body.entry_price,
        direction=body.direction,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        confidence=body.confidence,
    )
    await ws_manager.broadcast({"type": "trade_selected", "trade": trade})
    return {"trade": trade}


@live_router.delete("/selected-trade")
async def deselect_trade():
    watcher.clear()
    await ws_manager.broadcast({"type": "trade_deselected"})
    return {"status": "cleared"}


@live_router.get("/selected-trade/history")
async def get_trade_history():
    trade = watcher.get()
    if not trade:
        return {"items": []}
    try:
        from app.core.database import AsyncSessionLocal
        from app.live_trading.models import LiveTradeUpdate
        from sqlalchemy import select, desc
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(LiveTradeUpdate)
                .where(LiveTradeUpdate.ticker == trade["ticker"])
                .order_by(desc(LiveTradeUpdate.created_at))
                .limit(50)
            )
            rows = result.scalars().all()
            return {
                "items": [
                    {
                        "id": r.id,
                        "created_at": r.created_at.isoformat(),
                        "trigger_headline": r.trigger_headline,
                        "confidence_before": r.confidence_before,
                        "confidence_after": r.confidence_after,
                        "direction_flip": r.direction_flip,
                        "suggested_sl": r.suggested_sl,
                        "suggested_tp": r.suggested_tp,
                        "assessment": r.assessment,
                        "action_advised": r.action_advised,
                    }
                    for r in rows
                ]
            }
    except Exception:
        return {"items": []}


# ── Live Prices ───────────────────────────────────────────────────────────────

@live_router.get("/prices")
async def get_prices(tickers: Optional[str] = None):
    if tickers:
        syms = [t.strip().upper() for t in tickers.split(",") if t.strip()]
        return {"prices": {s: stream_engine.latest_prices.get(s) for s in syms}}
    return {"prices": stream_engine.latest_prices}


# ── News feed ─────────────────────────────────────────────────────────────────

@live_router.get("/news")
async def get_recent_news(limit: int = 30):
    from app.core.database import AsyncSessionLocal
    from app.live_trading.models import LiveNewsItem
    from sqlalchemy import select, desc
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LiveNewsItem)
            .where(LiveNewsItem.fetched_at >= cutoff)
            .order_by(desc(LiveNewsItem.fetched_at))
            .limit(limit)
        )
        rows = result.scalars().all()
        return {
            "items": [
                {
                    "id": r.id,
                    "title": r.title,
                    "source": r.source,
                    "sentiment_score": r.sentiment_score,
                    "asset_tags": r.asset_tags or [],
                    "fetched_at": r.fetched_at.isoformat(),
                }
                for r in rows
            ]
        }


# ── Chart data ───────────────────────────────────────────────────────────────

@live_router.get("/chart/{ticker}")
async def get_chart_data(ticker: str, period: str = "1d", interval: str = "5m"):
    """Return OHLCV candles for Lightweight Charts. period/interval follow yfinance conventions."""
    import asyncio

    _INTERVAL_MAP = {
        "1d": "5m", "5d": "30m", "1mo": "1d", "3mo": "1d",
    }
    interval = _INTERVAL_MAP.get(period, interval)

    def _fetch():
        from app.services.market_data import fetch_ohlcv
        hist = fetch_ohlcv(ticker.upper(), period=period, interval=interval)
        if hist is None or hist.empty:
            return []
        candles = []
        for ts, row in hist.iterrows():
            t = int(pd.Timestamp(ts).timestamp())
            candles.append({
                "time":  t,
                "open":  round(float(row["Open"]),  4),
                "high":  round(float(row["High"]),  4),
                "low":   round(float(row["Low"]),   4),
                "close": round(float(row["Close"]), 4),
            })
        return candles

    try:
        candles = await asyncio.to_thread(_fetch)
        return {"ticker": ticker.upper(), "period": period, "interval": interval, "candles": candles}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Trading API (placeholder) ─────────────────────────────────────────────────

@live_router.get("/trading-api/status")
async def trading_api_status():
    return trading_api.get_status()


@live_router.post("/trading-api/order")
async def trading_api_order(body: dict):
    return trading_api.place_order(
        ticker=body.get("ticker", ""),
        direction=body.get("direction", "long"),
        qty=body.get("qty", 0),
        order_type=body.get("order_type", "market"),
        price=body.get("price"),
    )
