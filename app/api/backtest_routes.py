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


def _sanitize_metrics(obj: Any) -> Any:
    """Recursively replace NaN/Inf with None so metrics_json is valid JSON for PostgreSQL."""
    import math
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize_metrics(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_metrics(v) for v in obj]
    return obj

# In-memory cache for background LLM insight results
# key: f"{asset_id}:{direction}:{risk_tier}" → {"status": "pending"|"done"|"error", "result": dict|None}
_insight_cache: dict[str, dict] = {}

# Live progress for running backtests — updated from the executor thread via progress_callback
# key: run_id → {"phase": "prefetch"|"replay"|"done", "current": int, "total": int}
_backtest_progress: dict[str, dict] = {}

# Live progress for walk-forward runs
# key: run_id → {"phase": "prefetch"|"windows"|"done"|"failed", "current": int, "total": int}
_walkforward_progress: dict[str, dict] = {}

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


class WalkForwardRequest(BaseModel):
    start_date: str
    end_date: str
    symbols: Optional[list[str]] = None
    risk_tier: str = "moderate"
    min_confidence: int = 40
    period: str = "5y"
    in_sample_days: int = 252    # 1 year IS window
    out_sample_days: int = 63    # 1 quarter OOS window


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

    # Limit to avoid very long-running requests (5 years max window)
    if (end - start).days > 1825:
        raise HTTPException(status_code=422, detail="Date range too wide (max ~5 years)")

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
                "market_regime": t.market_regime,
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


@router.get("/backtest/status/{run_id}")
async def get_backtest_status(run_id: str) -> dict[str, Any]:
    """Live progress for a running backtest. Polled every 2s by the frontend progress bar."""
    progress = _backtest_progress.get(run_id)
    if progress is None:
        return {"run_id": run_id, "phase": "unknown", "current": 0, "total": 0}
    return {"run_id": run_id, **progress}


@router.get("/backtest/quick")
async def quick_backtest(
    asset_id: str,
    category: str = "stocks_large",
    direction: str = "long",
    risk_tier: str = "moderate",
    days: int = 90,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Quick 14-day-outcome backtest over a configurable lookback window (default 90 days, max 365).
    Uses recent market conditions and historical sentiment.
    Includes the current engine signal for today and an LLM 14-day forward prediction.
    """
    from app.services.recommendations.engine import get_yf_symbol
    from app.services.backtesting.replay import run_backtest
    from app.models.market_analysis import MarketAnalysis

    # Clamp days to valid range (min 30, max 730 = 2 years)
    days = max(30, min(730, days))

    yahoo_symbol = get_yf_symbol(category, asset_id)
    if not yahoo_symbol:
        yahoo_symbol = asset_id.upper()

    # Signal window: configurable lookback (90–365 days)
    # Pre-fetch: always 2y so indicators have enough bars regardless of window size
    end = date.today()
    start = end - timedelta(days=days)
    prefetch_period = "2y"
    max_outcome_bars = 14  # 14 trading days = ~3 calendar weeks

    # Load historical sentiment snapshots for the lookback window
    start_dt = datetime.combine(start, datetime.min.time()).replace(tzinfo=timezone.utc)
    ma_result = await db.execute(
        select(MarketAnalysis)
        .where(MarketAnalysis.as_of >= start_dt)
        .order_by(MarketAnalysis.as_of.asc())
        .limit(500)
    )
    ma_rows = ma_result.scalars().all()

    sentiment_history: list[tuple] = []
    for ma in ma_rows:
        payload = ma.payload or {}
        by_asset = payload.get("by_asset_sentiment")
        if by_asset and ma.as_of:
            sentiment_history.append((ma.as_of, by_asset))

    # Load LATEST sentiment snapshot for the current signal analysis
    latest_by_asset: dict = {}
    if sentiment_history:
        latest_by_asset = sentiment_history[-1][1]

    logger.info(
        "[BACKTEST/quick] %s: %d-day window, 14-bar outcomes, %d sentiment snapshots",
        asset_id, days, len(sentiment_history)
    )

    def _run() -> dict:
        with _backtest_lock:
            return run_backtest(
                start_date=start,
                end_date=end,
                symbols=[yahoo_symbol],
                risk_tier=risk_tier,
                min_confidence=30,  # permissive — short window, want sample size
                period=prefetch_period,
                sentiment_history=sentiment_history if sentiment_history else None,
                max_outcome_bars=max_outcome_bars,
                window_days=days,
            )

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _run)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backtest failed: {e}")

    metrics = result.get("metrics", {})
    signals = result.get("signals", [])

    # Direction-specific stats
    by_dir = metrics.get("by_direction", {})
    dir_stats = by_dir.get(direction, {})
    other_dir = "short" if direction == "long" else "long"
    other_stats = by_dir.get(other_dir, {})

    win_rate = dir_stats.get("win_rate")
    total_for_dir = dir_stats.get("total", 0)

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

    # Compute TODAY's engine signal for this asset (current conditions)
    current_signal: Optional[dict] = None
    try:
        current_signal = await loop.run_in_executor(None, lambda: _compute_current_signal(yahoo_symbol, risk_tier, latest_by_asset))
    except Exception as e:
        logger.warning("[BACKTEST/quick] Current signal computation failed: %s", e)

    # Plain-language verdict (90-day context, 14-day outcomes)
    if total_for_dir < 3:
        verdict = (
            f"Too few {direction.upper()} signals in the last {days} days ({total_for_dir} found) "
            f"to draw reliable conclusions for a 14-day trade horizon."
        )
    else:
        wr_pct = round((win_rate or 0) * 100, 1)
        avg_pnl = metrics.get("avg_pnl_r") or 0
        verdict = (
            f"In the last {days} days, {direction.upper()} signals on {asset_id} hit target "
            f"{wr_pct}% of the time within 14 trading days. "
            f"Average outcome: {avg_pnl:+.2f}R per trade."
        )

    # Fire LLM insight in background — don't block the response
    cache_key = f"{asset_id}:{direction}:{risk_tier}"
    _insight_cache[cache_key] = {"status": "pending", "result": None}

    async def _run_insight():
        try:
            from app.services.research.llm import generate_backtest_insight
            result = await generate_backtest_insight(
                asset_id=asset_id,
                direction=direction,
                risk_tier=risk_tier,
                metrics=metrics,
                direction_stats=direction_stats_out,
                by_confidence_bucket=by_confidence_bucket,
                current_signal=current_signal,
                horizon_days=14,
            )
            _insight_cache[cache_key] = {"status": "done", "result": result}
        except Exception as e:
            logger.warning("[BACKTEST/quick] LLM insight failed: %s", e)
            _insight_cache[cache_key] = {"status": "error", "result": None}

    asyncio.create_task(_run_insight())

    return {
        "symbol": yahoo_symbol,
        "asset_id": asset_id,
        "direction": direction,
        "risk_tier": risk_tier,
        "window_days": days,
        "outcome_bars": max_outcome_bars,
        "total_signals": metrics.get("total_signals", len(signals)),
        "sentiment_snapshots_used": len(sentiment_history),
        "current_signal": current_signal,
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
        "by_regime": metrics.get("by_regime", {}),
        "verdict": verdict,
        "llm_analysis": None,
        "llm_insight_key": cache_key,
        "disclaimer": DISCLAIMER,
    }


@router.get("/backtest/insight")
async def get_backtest_insight(asset_id: str, direction: str = "long", risk_tier: str = "moderate") -> dict[str, Any]:
    """
    Poll for the background LLM insight result from /backtest/quick.
    Returns {"status": "pending"|"done"|"error", "llm_analysis": dict|None}
    """
    cache_key = f"{asset_id}:{direction}:{risk_tier}"
    entry = _insight_cache.get(cache_key)
    if entry is None:
        return {"status": "not_found", "llm_analysis": None}
    return {"status": entry["status"], "llm_analysis": entry.get("result")}


@router.post("/backtest/walkforward")
async def start_walkforward(
    body: WalkForwardRequest = Body(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Start walk-forward analysis in the background. Returns run_id immediately.
    Poll GET /api/backtest/walkforward/status/{run_id} for progress and results.
    """
    from app.models.walkforward import WalkForwardRun

    try:
        start = date.fromisoformat(body.start_date)
        end = date.fromisoformat(body.end_date)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid date format: {e}")

    if start >= end:
        raise HTTPException(status_code=422, detail="start_date must be before end_date")
    if (end - start).days > 1825:
        raise HTTPException(status_code=422, detail="Date range too wide (max ~5 years)")
    if body.in_sample_days < 63:
        raise HTTPException(status_code=422, detail="in_sample_days must be >= 63")
    if body.out_sample_days < 21:
        raise HTTPException(status_code=422, detail="out_sample_days must be >= 21")

    run_id = str(uuid4())
    run = WalkForwardRun(
        id=run_id,
        created_at=datetime.now(timezone.utc),
        start_date=start,
        end_date=end,
        risk_tier=body.risk_tier,
        min_confidence=body.min_confidence,
        in_sample_days=body.in_sample_days,
        out_sample_days=body.out_sample_days,
        status="running",
    )
    db.add(run)
    await db.commit()

    asyncio.create_task(_run_walkforward_background(run_id, body, start, end))

    return {
        "run_id": run_id,
        "status": "running",
        "message": f"Walk-forward started. Poll GET /api/backtest/walkforward/status/{run_id} for progress.",
    }


@router.get("/backtest/walkforward/status/{run_id}")
async def get_walkforward_status(run_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Live progress for a running walk-forward. Polled every 2s by the frontend."""
    from app.models.walkforward import WalkForwardRun

    progress = _walkforward_progress.get(run_id, {})
    result = await db.execute(select(WalkForwardRun).where(WalkForwardRun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        return {"run_id": run_id, "status": "not_found", "phase": "unknown", "current": 0, "total": 0}

    response: dict[str, Any] = {
        "run_id": run_id,
        "status": run.status,
        "phase": progress.get("phase", "unknown"),
        "current": progress.get("current", 0),
        "total": progress.get("total", 0),
        "error_message": run.error_message,
    }
    if run.status == "completed":
        response["result"] = run.result
        response["degradation_ratio"] = run.degradation_ratio
        response["robustness"] = run.robustness
        response["total_windows"] = run.total_windows
    return response


@router.get("/backtest/walkforward/runs")
async def list_walkforward_runs(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """List past walk-forward runs, newest first."""
    from app.models.walkforward import WalkForwardRun

    result = await db.execute(
        select(WalkForwardRun).order_by(WalkForwardRun.created_at.desc()).limit(20)
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
                "in_sample_days": r.in_sample_days,
                "out_sample_days": r.out_sample_days,
                "total_windows": r.total_windows,
                "degradation_ratio": r.degradation_ratio,
                "robustness": r.robustness,
            }
            for r in runs
        ]
    }


@router.get("/backtest/walkforward/runs/{run_id}")
async def get_walkforward_run(run_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Full result for a specific walk-forward run."""
    from app.models.walkforward import WalkForwardRun

    result = await db.execute(select(WalkForwardRun).where(WalkForwardRun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Walk-forward run not found")
    return {
        "id": run.id,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "start_date": run.start_date.isoformat() if run.start_date else None,
        "end_date": run.end_date.isoformat() if run.end_date else None,
        "status": run.status,
        "error_message": run.error_message,
        "risk_tier": run.risk_tier,
        "in_sample_days": run.in_sample_days,
        "out_sample_days": run.out_sample_days,
        "total_windows": run.total_windows,
        "degradation_ratio": run.degradation_ratio,
        "robustness": run.robustness,
        "result": run.result,
        "disclaimer": DISCLAIMER,
    }


def _run_walkforward_in_thread(body: WalkForwardRequest, start: date, end: date, progress_callback: Any) -> dict:
    from app.services.backtesting.replay import run_walkforward_backtest
    with _backtest_lock:
        return run_walkforward_backtest(
            start_date=start,
            end_date=end,
            symbols=body.symbols if body.symbols else None,
            risk_tier=body.risk_tier,
            min_confidence=body.min_confidence,
            period=body.period,
            in_sample_days=body.in_sample_days,
            out_sample_days=body.out_sample_days,
            progress_callback=progress_callback,
        )


async def _run_walkforward_background(run_id: str, body: WalkForwardRequest, start: date, end: date) -> None:
    from app.core.database import AsyncSessionLocal
    from app.models.walkforward import WalkForwardRun

    try:
        _walkforward_progress[run_id] = {"phase": "prefetch", "current": 0, "total": 0}

        def _progress_cb(current: int, total: int) -> None:
            _walkforward_progress[run_id] = {"phase": "windows", "current": current, "total": total}

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: _run_walkforward_in_thread(body, start, end, _progress_cb),
        )

        _walkforward_progress[run_id] = {"phase": "done", "current": 1, "total": 1}

        async with AsyncSessionLocal() as db:
            run_result = await db.execute(select(WalkForwardRun).where(WalkForwardRun.id == run_id))
            run = run_result.scalar_one_or_none()
            if run:
                run.status = "completed"
                run.total_windows = result.get("total_windows", 0)
                run.degradation_ratio = result.get("degradation_ratio")
                run.robustness = result.get("robustness")
                run.result_json = json.dumps(_sanitize_metrics(result))
            await db.commit()

        logger.info("[WF] Run %s completed: %d windows, degradation=%.3f",
                    run_id, result.get("total_windows", 0), result.get("degradation_ratio") or 0)

    except Exception as e:
        logger.exception("[WF] Run %s failed: %s", run_id, e)
        _walkforward_progress[run_id] = {"phase": "failed", "current": 0, "total": 0}
        try:
            from app.core.database import AsyncSessionLocal
            from app.models.walkforward import WalkForwardRun
            async with AsyncSessionLocal() as db:
                run_result = await db.execute(select(WalkForwardRun).where(WalkForwardRun.id == run_id))
                run = run_result.scalar_one_or_none()
                if run:
                    run.status = "failed"
                    run.error_message = str(e)[:500]
                await db.commit()
        except Exception:
            pass


def _compute_current_signal(symbol: str, risk_tier: str, by_asset_sentiment: dict) -> Optional[dict]:
    """
    Compute today's engine signal for a symbol using current OHLCV + optional sentiment.
    Returns a signal dict with confidence, direction, SL, TP, or None if no data.
    """
    from app.services.technicals import (
        build_snapshot, compute_atr_levels, compute_confidence,
        calc_risk_reward, determine_direction,
    )
    import app.services.technicals as tech

    # Use current data (no backtest cutoff)
    tech.clear_backtest_mode()
    snap = build_snapshot(symbol)
    if snap is None or snap.price <= 0:
        return None

    # Extract sentiment for this symbol
    hist_sentiment: Optional[float] = None
    hist_count: int = 0
    if by_asset_sentiment:
        candidates = [symbol, symbol.split("=")[0], symbol.split("-")[0], symbol.split(".")[0]]
        for cand in candidates:
            entry = by_asset_sentiment.get(cand)
            if entry:
                hist_sentiment = entry.get("avg")
                hist_count = entry.get("count", 0)
                break

    direction = determine_direction(None, snap)
    confidence = compute_confidence(
        sentiment_avg=hist_sentiment,
        sentiment_count=hist_count,
        snapshot=snap,
        direction=direction,
    )
    sl, tp = compute_atr_levels(snap.price, risk_tier, snapshot=snap, direction=direction)
    rr = calc_risk_reward(snap.price, sl, tp)

    return {
        "direction": direction,
        "confidence": confidence,
        "price": round(snap.price, 4),
        "stop_loss": round(sl, 4),
        "take_profit": round(tp, 4),
        "risk_reward": round(rr, 3) if rr else None,
        "volatility_regime": snap.volatility_regime,
        "trend_bias": snap.trend_bias,
        "sentiment_avg": round(hist_sentiment, 4) if hist_sentiment is not None else None,
        "sentiment_count": hist_count,
    }


async def _run_backtest_background(run_id: str, body: BacktestRequest) -> None:
    """Run the backtest in a background thread and persist results to DB."""
    import asyncio
    from app.core.database import AsyncSessionLocal
    from app.services.backtesting.replay import run_backtest

    try:
        start = date.fromisoformat(body.start_date)
        end = date.fromisoformat(body.end_date)

        _backtest_progress[run_id] = {"phase": "prefetch", "current": 0, "total": 0}

        def _progress_cb(current: int, total: int) -> None:
            _backtest_progress[run_id] = {"phase": "replay", "current": current, "total": total}

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
                progress_callback=_progress_cb,
            ),
        )

        _backtest_progress[run_id] = {"phase": "done", "current": 1, "total": 1}

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
                    asset_category=sig.get("asset_category", "stocks_large"),
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
                    market_regime=sig.get("market_regime"),
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
                run.metrics_json = json.dumps(_sanitize_metrics(metrics))
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
