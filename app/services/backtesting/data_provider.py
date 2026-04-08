"""
Historical data provider for backtesting.

Pre-fetches OHLCV data for all symbols using yfinance with a long lookback period
(e.g. 3 years) and injects the full DataFrames into the technicals module cache.
During replay, the technicals module slices data via _backtest_cutoff.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

from app.services import technicals as tech

logger = logging.getLogger(__name__)

# Default lookback period for historical data fetching
DEFAULT_PERIOD = "3y"

# All Yahoo symbols tracked by the engine (from ASSETS_BY_CATEGORY)
_ENGINE_SYMBOLS: list[str] = [
    # Stocks
    "XOM", "CVX", "BP", "SHEL", "NEM",
    # Commodities futures
    "CL=F", "BZ=F", "GC=F", "NG=F", "SI=F",
    # FX + Crypto
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "BTC-USD", "ETH-USD",
    # US Indices
    "^GSPC", "^DJI", "^IXIC", "^RUT",
    # European Indices
    "^FTSE", "^GDAXI", "^FCHI", "^STOXX50E",
    # Asia-Pacific
    "^N225", "^HSI", "000001.SS", "^KS11", "^BSESN", "^AXJO", "^NSEI",
    # Volatility + Macro
    "^VIX", "^GSPC",  # SPX for market context
    # DXY
    "DX-Y.NYB",
]


def get_engine_symbols() -> list[str]:
    """Return deduplicated list of all Yahoo symbols used by the engine."""
    return list(dict.fromkeys(_ENGINE_SYMBOLS))


def prefetch_symbols(
    symbols: list[str],
    period: str = DEFAULT_PERIOD,
    extra_symbols: Optional[list[str]] = None,
) -> dict[str, pd.DataFrame]:
    """
    Fetch full OHLCV history for each symbol and inject into the technicals cache.
    Returns a dict {symbol: DataFrame} for the caller to use in outcome simulation.

    This REPLACES the cache contents, so call clear_cache() or set_backtest_mode()
    before calling this in a new backtest run.
    """
    all_syms = list(dict.fromkeys(symbols + (extra_symbols or [])))
    fetched: dict[str, pd.DataFrame] = {}
    failed: list[str] = []

    logger.info("[BACKTEST] Pre-fetching %d symbols with period=%s", len(all_syms), period)

    # Silence yfinance noise
    import logging as _logging
    yf_log = _logging.getLogger("yfinance")
    old_level = yf_log.level
    yf_log.setLevel(_logging.CRITICAL)

    try:
        for sym in all_syms:
            try:
                ticker = yf.Ticker(sym)
                df = ticker.history(period=period, interval="1d")
                if df is None or df.empty or "Close" not in df.columns:
                    failed.append(sym)
                    # Cache empty so fetch_ohlcv doesn't retry yfinance during replay
                    tech._ohlcv_cache[sym] = pd.DataFrame()
                    continue
                # Normalize index to UTC-naive for consistent comparisons
                if df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                tech._ohlcv_cache[sym] = df
                fetched[sym] = df
            except Exception as e:
                logger.warning("[BACKTEST] Failed to fetch %s: %s", sym, e)
                failed.append(sym)
                tech._ohlcv_cache[sym] = pd.DataFrame()
    finally:
        yf_log.setLevel(old_level)

    if failed:
        logger.warning("[BACKTEST] Could not fetch %d symbols: %s", len(failed), failed)
    logger.info("[BACKTEST] Pre-fetched %d/%d symbols", len(fetched), len(all_syms))
    return fetched


def get_trading_days(
    ohlcv_data: dict[str, pd.DataFrame],
    start_date: date,
    end_date: date,
    reference_symbol: str = "^GSPC",
) -> list[date]:
    """
    Return list of trading days in [start_date, end_date] from the reference symbol's index.
    Falls back to any available symbol if reference not found.
    """
    ref_df = ohlcv_data.get(reference_symbol)
    if ref_df is None or ref_df.empty:
        # fallback to first available
        for sym, df in ohlcv_data.items():
            if df is not None and not df.empty:
                ref_df = df
                break

    if ref_df is None:
        logger.error("[BACKTEST] No data available to derive trading days")
        return []

    # Normalize index
    idx = ref_df.index
    if hasattr(idx, "tz") and idx.tz is not None:
        idx = idx.tz_localize(None)

    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)
    days = [ts.date() for ts in idx if start_ts <= ts <= end_ts]
    return sorted(days)


def get_future_bars(
    symbol: str,
    from_date: date,
    max_bars: int,
    ohlcv_data: dict[str, pd.DataFrame],
) -> Optional[pd.DataFrame]:
    """
    Return up to max_bars of OHLCV data AFTER from_date for a symbol.
    Used by the simulator to check if SL/TP was hit.
    """
    df = ohlcv_data.get(symbol)
    if df is None or df.empty:
        return None
    idx = df.index
    if hasattr(idx, "tz") and idx.tz is not None:
        idx = df.index.tz_localize(None)
        df = df.copy()
        df.index = idx

    cutoff = pd.Timestamp(from_date)
    future = df[df.index > cutoff]
    if future.empty:
        return None
    return future.iloc[:max_bars]
