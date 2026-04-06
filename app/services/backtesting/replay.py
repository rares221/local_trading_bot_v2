"""
Historical replay engine for backtesting.

For each trading day in [start_date, end_date]:
  1. Set backtest mode in technicals (cutoff = that day)
  2. For each symbol, build TechnicalSnapshot as of that day
  3. Compute confidence score (technicals + optional historical sentiment)
  4. If confidence >= min_confidence, record a signal (entry, SL, TP)
  5. After all signals: simulate outcome from future bars

If sentiment_history is provided (list of (datetime, by_asset_sentiment) tuples from
market_analyses records), the closest past record is used per replay date to inject
historical sentiment into the confidence computation.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

import pandas as pd

from app.services import technicals as tech
from app.services.technicals import (
    build_snapshot,
    compute_atr_levels,
    compute_confidence,
    calc_risk_reward,
)
from app.services.backtesting.data_provider import (
    get_future_bars,
    get_trading_days,
    prefetch_symbols,
    get_engine_symbols,
)
from app.services.backtesting.simulator import simulate_outcome
from app.services.backtesting.metrics import compute_metrics

logger = logging.getLogger(__name__)

# Min data bars required before producing a signal (indicators need at least 30+ bars)
MIN_BARS_FOR_SIGNAL = 35

# Outcome simulation max bars (30 trading days ~ 6 calendar weeks)
OUTCOME_MAX_BARS = 30


def run_backtest(
    start_date: date,
    end_date: date,
    symbols: Optional[list[str]] = None,
    risk_tier: str = "moderate",
    min_confidence: int = 40,
    period: str = "3y",
    progress_callback: Optional[Any] = None,
    sentiment_history: Optional[list[tuple]] = None,
    max_outcome_bars: int = OUTCOME_MAX_BARS,
) -> dict[str, Any]:
    """
    Run full historical backtest.

    Args:
        start_date: First date to generate signals (inclusive).
        end_date: Last date to generate signals (inclusive).
        symbols: Yahoo Finance symbols to test. If None, uses all engine symbols.
        risk_tier: conservative | moderate | aggressive — controls ATR SL/TP multiplier.
        min_confidence: Minimum confidence score to record a signal.
        period: yfinance period string for pre-fetching ("2y", "3y", etc.).
        progress_callback: Optional callable(current_day, total_days) for progress tracking.
        sentiment_history: Optional list of (datetime, by_asset_sentiment) tuples from
            market_analyses records. If provided, the closest past record is used per day.
        max_outcome_bars: Maximum trading bars to hold a trade before expiry (default 30).

    Returns:
        Dict with 'signals', 'metrics', and summary fields.
    """
    if symbols is None:
        symbols = get_engine_symbols()

    logger.info(
        "[BACKTEST] Starting replay: %s → %s | %d symbols | tier=%s | min_conf=%d",
        start_date, end_date, len(symbols), risk_tier, min_confidence,
    )

    # Step 1: Pre-fetch all historical data into technicals cache
    tech.clear_cache()
    ohlcv_data = prefetch_symbols(symbols, period=period)

    if not ohlcv_data:
        return {"error": "no_data_fetched", "signals": [], "metrics": {}}

    # Step 2: Get list of trading days
    trading_days = get_trading_days(ohlcv_data, start_date, end_date)
    if not trading_days:
        return {"error": "no_trading_days", "signals": [], "metrics": {}}

    logger.info("[BACKTEST] %d trading days to replay", len(trading_days))

    # Sort sentiment_history ascending by datetime for bisect lookups
    _sent_hist: list[tuple] = []
    if sentiment_history:
        _sent_hist = sorted(sentiment_history, key=lambda t: t[0])
        logger.info("[BACKTEST] Sentiment history: %d market_analyses records loaded", len(_sent_hist))

    signals: list[dict] = []

    # Step 3: For each day, build signals
    for day_idx, sim_date in enumerate(trading_days):
        if progress_callback:
            try:
                progress_callback(day_idx + 1, len(trading_days))
            except Exception:
                pass

        # Set cutoff so fetch_ohlcv slices data to sim_date
        cutoff_ts = pd.Timestamp(sim_date)
        tech.set_backtest_mode(cutoff_ts)

        # Find closest past sentiment snapshot
        day_sentiment: Optional[dict] = None
        if _sent_hist:
            sim_dt = pd.Timestamp(sim_date, tz="UTC")
            # Find last record with as_of <= sim_date
            lo, hi = 0, len(_sent_hist) - 1
            idx = -1
            while lo <= hi:
                mid = (lo + hi) // 2
                if _sent_hist[mid][0] <= sim_dt:
                    idx = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            if idx >= 0:
                day_sentiment = _sent_hist[idx][1]

        for symbol in symbols:
            try:
                sig = _build_signal(symbol, sim_date, risk_tier, min_confidence, day_sentiment)
                if sig:
                    signals.append(sig)
            except Exception as e:
                logger.debug("[BACKTEST] Error building signal %s @%s: %s", symbol, sim_date, e)

    # Step 4: Clear backtest mode before outcome simulation (need full data)
    tech.clear_backtest_mode()

    # Step 5: Simulate outcomes using future bars from full OHLCV
    logger.info("[BACKTEST] Simulating outcomes for %d signals (max %d bars)", len(signals), max_outcome_bars)
    for sig in signals:
        _simulate_signal_outcome(sig, ohlcv_data, max_outcome_bars)

    # Step 6: Compute aggregate metrics
    metrics = compute_metrics(signals)

    logger.info(
        "[BACKTEST] Done: %d signals, win_rate=%.1f%%, sharpe=%s, max_dd=%sR",
        metrics.get("total_signals", 0),
        (metrics.get("win_rate") or 0) * 100,
        metrics.get("sharpe_ratio"),
        metrics.get("max_drawdown_r"),
    )

    return {
        "signals": signals,
        "metrics": metrics,
        "total_days": len(trading_days),
        "symbols_tested": list(ohlcv_data.keys()),
    }


def _build_signal(
    symbol: str,
    sim_date: date,
    risk_tier: str,
    min_confidence: int,
    by_asset_sentiment: Optional[dict] = None,
) -> Optional[dict]:
    """
    Build a single signal for one symbol on one day.
    Returns None if signal doesn't qualify (no snapshot, below confidence gate).
    """
    snap = build_snapshot(symbol)
    if snap is None or snap.price <= 0:
        return None

    # Check we have enough bars (anti-lookahead: need MIN_BARS for indicators)
    df = tech._ohlcv_cache.get(symbol)
    if df is not None and not df.empty:
        cutoff_ts = pd.Timestamp(sim_date)
        available = df[df.index <= cutoff_ts]
        if len(available) < MIN_BARS_FOR_SIGNAL:
            return None

    # Extract per-symbol sentiment from historical snapshot (if available)
    # by_asset_sentiment keys are asset tags (e.g. "AAPL", "Oil", "Gold")
    # Try symbol as-is, then strip exchange suffix (e.g. "CL=F" → "CL")
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

    # Direction from technicals (sentiment nudges confidence, not direction)
    from app.services.technicals import determine_direction as _determine_direction
    direction = _determine_direction(None, snap)

    confidence = compute_confidence(
        sentiment_avg=hist_sentiment,
        sentiment_count=hist_count,
        snapshot=snap,
        direction=direction,
    )

    if confidence < min_confidence:
        return None

    # SL/TP
    sl, tp = compute_atr_levels(snap.price, risk_tier, snapshot=snap, direction=direction)
    rr = calc_risk_reward(snap.price, sl, tp)

    if rr is None or rr < 1.0:
        return None

    return {
        "symbol": symbol,
        "asset_category": _guess_category(symbol),
        "direction": direction,
        "risk_tier": risk_tier,
        "entry_date": sim_date.isoformat(),
        "entry_price": round(snap.price, 4),
        "stop_loss": round(sl, 4),
        "take_profit": round(tp, 4),
        "confidence_score": confidence,
        "risk_reward": round(rr, 4) if rr else None,
        "volatility_regime": snap.volatility_regime,
        "trend_bias": snap.trend_bias,
        "atr_pct": round(snap.atr_pct, 6) if snap.atr_pct else None,
        # Outcome fields (populated later)
        "outcome": None,
        "closing_price": None,
        "outcome_date": None,
        "bars_held": None,
        "pnl_r": None,
    }


def _simulate_signal_outcome(sig: dict, ohlcv_data: dict[str, pd.DataFrame], max_bars: int = OUTCOME_MAX_BARS) -> None:
    """Simulate outcome in-place on the signal dict."""
    symbol = sig["symbol"]
    entry_date = date.fromisoformat(sig["entry_date"])
    future = get_future_bars(symbol, entry_date, max_bars, ohlcv_data)

    if future is None:
        sig["outcome"] = "expired"
        sig["closing_price"] = sig["entry_price"]
        sig["outcome_date"] = entry_date.isoformat()
        sig["bars_held"] = 0
        sig["pnl_r"] = 0.0
        return

    result = simulate_outcome(
        entry_price=sig["entry_price"],
        stop_loss=sig["stop_loss"],
        take_profit=sig["take_profit"],
        direction=sig["direction"],
        future_bars=future,
        max_bars=max_bars,
    )

    sig["outcome"] = result.outcome
    sig["closing_price"] = round(result.closing_price, 4)
    sig["outcome_date"] = result.outcome_date.isoformat()
    sig["bars_held"] = result.bars_held
    sig["pnl_r"] = result.pnl_r


# Symbol → category mapping (best-effort; used for metrics grouping only)
_CATEGORY_MAP: dict[str, str] = {
    "XOM": "stocks", "CVX": "stocks", "BP": "stocks", "SHEL": "stocks", "NEM": "stocks",
    "CL=F": "commodities", "BZ=F": "commodities", "GC=F": "commodities",
    "NG=F": "commodities", "SI=F": "commodities",
    "EURUSD=X": "other", "GBPUSD=X": "other", "USDJPY=X": "other",
    "BTC-USD": "other", "ETH-USD": "other",
    "^GSPC": "indices", "^DJI": "indices", "^IXIC": "indices", "^RUT": "indices",
    "^FTSE": "indices", "^GDAXI": "indices", "^FCHI": "indices", "^STOXX50E": "indices",
    "^N225": "indices", "^HSI": "indices", "000001.SS": "indices",
    "^KS11": "indices", "^BSESN": "indices", "^AXJO": "indices", "^NSEI": "indices",
    "^VIX": "indices",
}


def _guess_category(symbol: str) -> str:
    return _CATEGORY_MAP.get(symbol, "stocks")
