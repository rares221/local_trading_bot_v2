"""
Backtesting API routes.

POST /api/backtest/run      — start a new backtest run (async, runs in background thread)
GET  /api/backtest/runs     — list all backtest runs
GET  /api/backtest/runs/{id} — get run details + metrics
GET  /api/backtest/runs/{id}/trades — paginated trade list for a run
DELETE /api/backtest/runs/{id} — delete a run + its trades
"""
import asyncio
import json
import logging
from datetime import datetime, timezone, date
from typing import Any, Optional
from uuid import uuid4

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
