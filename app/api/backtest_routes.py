"""
Backtesting API routes.

POST /api/backtest/run          — start a new backtest run (async, background)
GET  /api/backtest/runs         — list all backtest runs
GET  /api/backtest/runs/{id}    — get run details + metrics
GET  /api/backtest/runs/{id}/trades — paginated trade list for a run
DELETE /api/backtest/runs/{id}  — delete a run + its trades
GET  /api/backtest/quick        — quick single-symbol backtest (sync, ~15s), used by active trade cards
"""
import asyncio
import json
import logging
import threading
from datetime import datetime, timezone, date, timedelta
from typing import Any, Optional
from uuid import uuid4

# Prevents concurrent backtests from clobbering the shared technicals cache
_backtest_lock = threading.Lock()

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.backtest import BacktestRun, BacktestTrade

logger = logging.getLogger(__name__)

router = APIRouter()

DISCLAIMER = (
    "Backtesting results are for educational purposes only. Past performance does not "
    "guarantee future results. Backtests do not account for slippage, commissions, or "
    "liquidity constraints. Not financial advice."
)


class BacktestRequest(BaseModel):
    start_date: str         # ISO format: YYYY-MM-DD
    end_date: str           # ISO format: YYYY-MM-DD
    symbols: Optional[list[str]] = None   # None = all engine symbols
    risk_tier: str = "moderate"
    min_confidence: int = 40
    period: str = "3y"      # yfinance history period for pre-fetch


@router.post("/backtest/run")
async def start_backtest(
    body: BacktestRequest = Body(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Start a new backtest run in the background.
    Returns immediately with the run ID; poll GET /backtest/runs/{id} for status.
    """
    # Validate dates
    try:
        start = date.fromisoformat(body.start_date)
        end = date.fromisoformat(body.end_date)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid date format: {e}")

    if start >= end:
        raise HTTPException(status_code=422, detail="start_date must be before end_date")

    # Limit to avoid very long-running requests (3 years max window)
    if (end - start).days > 1200:
        raise HTTPException(status_code=422, detail="Date range too wide (max ~3 years)")

    run_id = str(uuid4())
    run = BacktestRun(
        id=run_id,
        created_at=datetime.now(timezone.utc),
        start_date=start,
        end_date=end,
        symbols_json=json.dumps(body.symbols or []),
        risk_tier=body.risk_tier,
        min_confidence=body.min_confidence,
        status="running",
    )
    db.add(run)
    await db.commit()

    # Fire off the replay in a background thread (CPU-bound, avoid blocking event loop)
    asyncio.create_task(_run_backtest_background(run_id, body))

    return {
        "run_id": run_id,
        "status": "running",
        "message": f"Backtest started. Poll GET /api/backtest/runs/{run_id} for status.",
        "disclaimer": DISCLAIMER,
    }


@router.get("/backtest/runs")
async def list_backtest_runs(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """List all backtest runs, newest first."""
    result = await db.execute(
        select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(50)
    )
    runs = result.scalars().all()

    return {
        "runs": [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "start_date": r.start_date.isoformat() if r.start_date else None,
                "end_date": r.end_date.isoformat() if r.end_date else None,
                "status": r.status,
                "risk_tier": r.risk_tier,
                "min_confidence": r.min_confidence,
                "total_signals": r.total_signals,
                "total_trades": r.total_trades,
                "win_rate": r.win_rate,
                "sharpe_ratio": r.sharpe_ratio,
                "max_drawdown": r.max_drawdown,
            }
            for r in runs
        ],
        "disclaimer": DISCLAIMER,
    }


@router.get("/backtest/runs/{run_id}")
async def get_backtest_run(run_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Get full details + metrics for a specific run."""
    result = await db.execute(select(BacktestRun).where(BacktestRun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Backtest run not found")

    return {
        "id": run.id,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "start_date": run.start_date.isoformat() if run.start_date else None,
        "end_date": run.end_date.isoformat() if run.end_date else None,
        "status": run.status,
        "error_message": run.error_message,
        "risk_tier": run.risk_tier,
        "min_confidence": run.min_confidence,
        "symbols": run.symbols,
        "total_signals": run.total_signals,
        "total_trades": run.total_trades,
        "win_rate": run.win_rate,
        "sharpe_ratio": run.sharpe_ratio,
        "max_drawdown": run.max_drawdown,
        "avg_rr_realized": run.avg_rr_realized,
        "avg_rr_projected": run.avg_rr_projected,
        "metrics": run.metrics,
        "disclaimer": DISCLAIMER,
    }


@router.get("/backtest/runs/{run_id}/trades")
async def get_backtest_trades(
    run_id: str,
    offset: int = 0,
    limit: int = 100,
    outcome: Optional[str] = None,
    symbol: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Paginated list of individual trades for a backtest run."""
    result = await db.execute(select(BacktestRun).where(BacktestRun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Backtest run not found")

    q = select(BacktestTrade).where(BacktestTrade.run_id == run_id)
    if outcome:
        q = q.where(BacktestTrade.outcome == outcome)
    if symbol:
        q = q.where(BacktestTrade.symbol == symbol)
    q = q.order_by(BacktestTrade.entry_date).offset(offset).limit(limit)

    result = await db.execute(q)
    trades = result.scalars().all()

    return {
        "run_id": run_id,
        "offset": offset,
        "limit": limit,
        "trades": [
            {
                "id": t.id,
                "symbol": t.symbol,
                "asset_category": t.asset_category,
                "direction": t.direction,
                "risk_tier": t.risk_tier,
                "entry_date": t.entry_date.isoformat() if t.entry_date else None,
                "entry_price": t.entry_price,
                "stop_loss": t.stop_loss,
                "take_profit": t.take_profit,
                "confidence_score": t.confidence_score,
                "risk_reward": t.risk_reward,
                "outcome": t.outcome,
                "closing_price": t.closing_price,
                "outcome_date": t.outcome_date.isoformat() if t.outcome_date else None,
                "bars_held": t.bars_held,
                "pnl_r": t.pnl_r,
                "volatility_regime": t.volatility_regime,
                "trend_bias": t.trend_bias,
                "atr_pct": t.atr_pct,
            }
            for t in trades
        ],
    }


@router.delete("/backtest/runs/{run_id}")
async def delete_backtest_run(run_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Delete a backtest run and all its trades."""
    result = await db.execute(select(BacktestRun).where(BacktestRun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Backtest run not found")

    await db.execute(delete(BacktestTrade).where(BacktestTrade.run_id == run_id))
    await db.execute(delete(BacktestRun).where(BacktestRun.id == run_id))
    await db.commit()

    return {"deleted": run_id}


@router.get("/backtest/quick")
async def quick_backtest(
    asset_id: str,
    category: str = "stocks",
    direction: str = "long",
    risk_tier: str = "moderate",
    period: str = "1y",
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Quick single-symbol backtest for an active trade card.
    Runs synchronously in a thread pool (~10-20s). Returns metrics filtered
    by the specified direction so the user sees relevant stats for their trade.
    Includes historical sentiment from market_analyses and LLM interpretation.
    """
    from app.services.recommendations.engine import get_yf_symbol
    from app.services.backtesting.replay import run_backtest
    from app.models.market_analysis import MarketAnalysis

    yahoo_symbol = get_yf_symbol(category, asset_id)
    if not yahoo_symbol:
        yahoo_symbol = asset_id.upper()

    # Map user-facing period to yfinance period for pre-fetch
    prefetch_period = "3y" if period in ("1y", "2y") else "5y"

    end = date.today()
    if period == "1y":
        start = end - timedelta(days=365)
    elif period == "2y":
        start = end - timedelta(days=730)
    else:
        start = end - timedelta(days=365)

    # Pre-load historical sentiment from market_analyses for the period
    start_dt = datetime.combine(start, datetime.min.time()).replace(tzinfo=timezone.utc)
    ma_result = await db.execute(
        select(MarketAnalysis)
        .where(MarketAnalysis.as_of >= start_dt)
        .order_by(MarketAnalysis.as_of.asc())
        .limit(500)
    )
    ma_rows = ma_result.scalars().all()

    # Build list of (datetime, by_asset_sentiment) tuples
    sentiment_history: list[tuple] = []
    for ma in ma_rows:
        payload = ma.payload or {}
        by_asset = payload.get("by_asset_sentiment")
        if by_asset and ma.as_of:
            sentiment_history.append((ma.as_of, by_asset))

    logger.info(
        "[BACKTEST/quick] %s: %d sentiment snapshots loaded from market_analyses",
        asset_id, len(sentiment_history)
    )

    def _run() -> dict:
        with _backtest_lock:
            return run_backtest(
                start_date=start,
                end_date=end,
                symbols=[yahoo_symbol],
                risk_tier=risk_tier,
                min_confidence=35,  # permissive — we want sample size
                period=prefetch_period,
                sentiment_history=sentiment_history if sentiment_history else None,
            )

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _run)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backtest failed: {e}")

    metrics = result.get("metrics", {})
    signals = result.get("signals", [])
    total_days = result.get("total_days", 0)

    # Direction-specific stats
    by_dir = metrics.get("by_direction", {})
    dir_stats = by_dir.get(direction, {})
    other_dir = "short" if direction == "long" else "long"
    other_stats = by_dir.get(other_dir, {})

    win_rate = dir_stats.get("win_rate")
    total_for_dir = dir_stats.get("total", 0)

    # Plain language verdict
    if total_for_dir < 5:
        verdict = f"Not enough {direction.upper()} signals in this period to draw conclusions ({total_for_dir} found)."
    else:
        wr_pct = round((win_rate or 0) * 100, 1)
        avg_pnl = metrics.get("avg_pnl_r") or 0
        verdict = (
            f"Over the last {period}, when the engine said {direction.upper()} on {asset_id}, "
            f"{wr_pct}% of signals hit take-profit. "
            f"Average trade returned {avg_pnl:+.2f}R."
        )

    direction_stats_out = {
        direction: {
            "total": dir_stats.get("total", 0),
            "wins": dir_stats.get("wins", 0),
            "win_rate": dir_stats.get("win_rate"),
        },
        other_dir: {
            "total": other_stats.get("total", 0),
            "wins": other_stats.get("wins", 0),
            "win_rate": other_stats.get("win_rate"),
        },
    }

    by_confidence_bucket = metrics.get("by_confidence_bucket", {})

    # LLM post-analysis (non-blocking; returns None if LLM not configured)
    llm_analysis: Optional[str] = None
    try:
        from app.services.research.llm import generate_backtest_insight
        llm_analysis = await generate_backtest_insight(
            asset_id=asset_id,
            direction=direction,
            risk_tier=risk_tier,
            metrics=metrics,
            direction_stats=direction_stats_out,
            by_confidence_bucket=by_confidence_bucket,
        )
    except Exception as e:
        logger.warning("[BACKTEST/quick] LLM insight failed: %s", e)

    return {
        "symbol": yahoo_symbol,
        "asset_id": asset_id,
        "direction": direction,
        "risk_tier": risk_tier,
        "period": period,
        "test_days": total_days,
        "total_signals": metrics.get("total_signals", len(signals)),
        "sentiment_snapshots_used": len(sentiment_history),
        "direction_stats": direction_stats_out,
        "overall": {
            "win_rate": metrics.get("win_rate"),
            "avg_pnl_r": metrics.get("avg_pnl_r"),
            "sharpe_ratio": metrics.get("sharpe_ratio"),
            "max_drawdown_r": metrics.get("max_drawdown_r"),
            "wins": metrics.get("wins"),
            "losses": metrics.get("losses"),
            "expired": metrics.get("expired"),
        },
        "by_confidence_bucket": by_confidence_bucket,
        "verdict": verdict,
        "llm_analysis": llm_analysis,
        "disclaimer": DISCLAIMER,
    }


async def _run_backtest_background(run_id: str, body: BacktestRequest) -> None:
    """Run the backtest in a background thread and persist results to DB."""
    import asyncio
    from app.core.database import AsyncSessionLocal
    from app.services.backtesting.replay import run_backtest

    try:
        start = date.fromisoformat(body.start_date)
        end = date.fromisoformat(body.end_date)

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: run_backtest(
                start_date=start,
                end_date=end,
                symbols=body.symbols if body.symbols else None,
                risk_tier=body.risk_tier,
                min_confidence=body.min_confidence,
                period=body.period,
            ),
        )

        signals = result.get("signals", [])
        metrics = result.get("metrics", {})

        async with AsyncSessionLocal() as db:
            # Persist trades
            for sig in signals:
                if sig.get("outcome") is None:
                    continue
                trade = BacktestTrade(
                    id=str(uuid4()),
                    run_id=run_id,
                    symbol=sig["symbol"],
                    asset_category=sig.get("asset_category", "stocks"),
                    direction=sig["direction"],
                    risk_tier=sig["risk_tier"],
                    entry_date=date.fromisoformat(sig["entry_date"]),
                    entry_price=sig["entry_price"],
                    stop_loss=sig["stop_loss"],
                    take_profit=sig["take_profit"],
                    confidence_score=sig["confidence_score"],
                    risk_reward=sig.get("risk_reward"),
                    outcome=sig.get("outcome"),
                    closing_price=sig.get("closing_price"),
                    outcome_date=date.fromisoformat(sig["outcome_date"]) if sig.get("outcome_date") else None,
                    bars_held=sig.get("bars_held"),
                    pnl_r=sig.get("pnl_r"),
                    volatility_regime=sig.get("volatility_regime"),
                    trend_bias=sig.get("trend_bias"),
                    atr_pct=sig.get("atr_pct"),
                )
                db.add(trade)

            # Update run with aggregates
            run_result = await db.execute(select(BacktestRun).where(BacktestRun.id == run_id))
            run = run_result.scalar_one_or_none()
            if run:
                run.status = "completed"
                run.total_signals = metrics.get("total_signals", len(signals))
                run.total_trades = metrics.get("total_trades", 0)
                run.win_rate = metrics.get("win_rate")
                run.sharpe_ratio = metrics.get("sharpe_ratio")
                run.max_drawdown = metrics.get("max_drawdown_r")
                run.avg_rr_realized = metrics.get("avg_pnl_r")
                run.avg_rr_projected = metrics.get("avg_rr_projected")
                run.metrics_json = json.dumps(metrics)
                run.symbols_json = json.dumps(result.get("symbols_tested", []))

            await db.commit()

        logger.info("[BACKTEST] Run %s completed: %d signals", run_id, len(signals))

    except Exception as e:
        logger.exception("[BACKTEST] Run %s failed: %s", run_id, e)
        try:
            from app.core.database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                run_result = await db.execute(select(BacktestRun).where(BacktestRun.id == run_id))
                run = run_result.scalar_one_or_none()
                if run:
                    run.status = "failed"
                    run.error_message = str(e)[:500]
                await db.commit()
        except Exception:
            pass
