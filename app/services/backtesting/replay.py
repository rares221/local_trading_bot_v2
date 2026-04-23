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
    compute_relative_strength,
    calc_risk_reward,
)
from app.services.recommendations.engine import (
    SHORTS_ENABLED,
    SHORTS_BEAR_REGIME_ENABLED,
    BEAR_REGIME_SHORT_MIN_CONFIDENCE,
    _STOCK_CATEGORIES,
    MIN_RISK_REWARD,
    _min_rr_floor_for_build,
)
from app.services.recommendations.asset_class_policy import get_asset_class_policy
from app.services.backtesting.data_provider import (
    get_future_bars,
    get_trading_days,
    prefetch_symbols,
    get_engine_symbols,
)
from app.services.backtesting.simulator import simulate_outcome
from app.services.backtesting.metrics import compute_metrics
from app.services.backtesting.regime import build_regime_calendar

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
    window_days: int = 0,
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
    # Also pre-fetch ^GSPC and common sector ETFs for RS calculation
    for _rs_sym in ["^GSPC", "XLK", "XLE", "XLF", "XLV", "XLY", "XLI", "XLB"]:
        tech.fetch_ohlcv(_rs_sym, period=period)

    if not ohlcv_data:
        return {"error": "no_data_fetched", "signals": [], "metrics": {}}

    # Step 2: Get list of trading days
    trading_days = get_trading_days(ohlcv_data, start_date, end_date)
    if not trading_days:
        return {"error": "no_trading_days", "signals": [], "metrics": {}}

    logger.info("[BACKTEST] %d trading days to replay", len(trading_days))

    # Step 2b: Build regime calendar for all trading days (vectorized, efficient)
    regime_calendar: dict = build_regime_calendar(trading_days, ohlcv_data)

    # Sort sentiment_history ascending by datetime for bisect lookups.
    # Normalize all timestamps to UTC-aware to avoid naive/aware comparison crashes.
    _sent_hist: list[tuple[pd.Timestamp, dict]] = []
    if sentiment_history:
        for raw_dt, by_asset in sentiment_history:
            ts = pd.Timestamp(raw_dt)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")
            _sent_hist.append((ts, by_asset))
        _sent_hist.sort(key=lambda t: t[0])
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

        day_regime = regime_calendar.get(sim_date, "neutral")

        for symbol in symbols:
            try:
                sig = _build_signal(symbol, sim_date, risk_tier, min_confidence, day_sentiment, day_regime)
                if sig:
                    sig["market_regime"] = day_regime
                    signals.append(sig)
            except Exception as e:
                logger.debug("[BACKTEST] Error building signal %s @%s: %s", symbol, sim_date, e)

    # Step 4: Clear backtest mode before outcome simulation (need full data)
    tech.clear_backtest_mode()

    # Step 5: Simulate outcomes using future bars from full OHLCV
    logger.info("[BACKTEST] Simulating outcomes for %d signals (max %d bars)", len(signals), max_outcome_bars)
    for sig in signals:
        _simulate_signal_outcome(sig, ohlcv_data, max_outcome_bars)

    # Step 6: Compute aggregate metrics (pass total_trading_days for correct Sharpe)
    # window_days overrides if provided (e.g. quick backtest passes calendar days → convert)
    td = len(trading_days) if not window_days else max(len(trading_days), 1)
    metrics = compute_metrics(signals, total_trading_days=td)

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
    day_regime: str = "neutral",
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

    # Hard-block crisis and high_vol regimes — mirrors live engine gate.
    # Backtest: crisis=8.3% WR (-0.809R), high_vol=26.0% WR (-0.002R) — both net negative EV.
    if day_regime in ("crisis", "high_vol"):
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

    # ADX trend-strength soft penalty: ADX < 20 = weak/no trend — mirrors live engine.
    # Soft penalty (-8) rather than hard gate preserves diversification.
    _adx_penalty = 0
    if snap.adx is not None:
        _adx_val, *_ = snap.adx
        if _adx_val < 20:
            _adx_penalty = 8

    # Direction from sentiment + technicals, mirroring the live engine.
    # Pass hist_sentiment so macro-linked assets (oil, FX, gold) use the same
    # 30-50% sentiment weight the engine uses, not pure technicals.
    # Pass day_regime so RSI/Stochastic thresholds match the live engine's regime context.
    from app.services.technicals import determine_direction as _determine_direction
    direction = _determine_direction(hist_sentiment, snap, regime=day_regime)

    # EMA200 hard gate for shorts: mirror live engine behaviour.
    # Do not short an asset that is above its 200-day EMA — structural upward bias
    # in equities means these shorts overwhelmingly hit SL (confirmed by backtest data).
    if direction == "short" and snap.ema200 is not None and snap.price > snap.ema200:
        return None

    confidence = compute_confidence(
        sentiment_avg=hist_sentiment,
        sentiment_count=hist_count,
        snapshot=snap,
        direction=direction,
        regime=day_regime,
    )

    # Short filtering — driven by the same flags as the live engine so backtest and live stay in sync.
    if direction == "short" and not SHORTS_ENABLED:
        _bear_short_allowed = (
            SHORTS_BEAR_REGIME_ENABLED
            and day_regime == "bear"
        )
        if not _bear_short_allowed:
            return None
        if confidence < BEAR_REGIME_SHORT_MIN_CONFIDENCE:
            return None

    # Combined soft-penalty cap: all confidence-reducing signals pooled so no combination of weak
    # signals can stack into an invisible hard gate. Cap 25 pts.
    # --- MIRRORED FROM engine.py ---
    category = _guess_category(symbol)
    pol = get_asset_class_policy(category, symbol)
    _obv_penalty = 5 if (direction == "long" and snap.obv_confirming_down) else 0
    _macd_decay_penalty = 0
    if direction == "long" and snap.macd is not None and snap.macd_hist_slope is not None:
        if snap.macd[2] > 0 and snap.macd_hist_slope < 0:
            _macd_decay_penalty = 5
    _rsi_slope_penalty = 0
    if snap.rsi is not None and snap.rsi_slope is not None:
        if direction == "long":
            if snap.rsi_slope < -4.0:
                _rsi_slope_penalty = 8
            elif snap.rsi_slope < -2.0:
                _rsi_slope_penalty = 4
        elif direction == "short":
            if snap.rsi_slope > 2.0:
                _rsi_slope_penalty = 4
    _macd_alignment_penalty = 0
    if snap.macd is not None:
        if direction == "long" and snap.macd_bearish:
            _macd_alignment_penalty = 8
        elif direction == "short" and snap.macd_bullish:
            _macd_alignment_penalty = 8
    _vol_floor = 0.75  # moderate default; Step 4 will make this preference-aware
    _volume_penalty = 0
    if snap.volume_ratio is not None and category in _STOCK_CATEGORIES:
        if snap.volume_ratio < _vol_floor:
            _volume_penalty = 6
    # --- MIRRORED FROM engine.py ---
    # Step 6 confirmed sweet spot — mid -4 / small -6 / other ungated. See engine.py for rationale.
    _cap_tier_penalty = 0
    if category == "stocks_mid":
        _cap_tier_penalty = 4
    elif category == "stocks_small":
        _cap_tier_penalty = 6
    _new_filter_penalty = min(
        _adx_penalty + _obv_penalty + _macd_decay_penalty
        + _rsi_slope_penalty + _macd_alignment_penalty + _volume_penalty + _cap_tier_penalty,
        25,
    )
    if _new_filter_penalty > 0:
        confidence = max(0, confidence - _new_filter_penalty)

    # Signal bonuses (mirrors engine.py)
    if direction == "long" and snap.macd is not None and snap.rsi is not None:
        if snap.macd[2] > 0 and 45 <= snap.rsi <= 65:
            confidence = min(100, confidence + 6)
    if snap.volume_ratio is not None and snap.volume_ratio > 1.5:
        confidence = min(100, confidence + 5)
    # RSI momentum bonus (+4 pts): positive slope + non-overbought. Penalties pooled above.
    if snap.rsi is not None and snap.rsi_slope is not None:
        if direction == "long" and snap.rsi_slope > 0 and snap.rsi < 65:
            confidence = min(100, confidence + 4)
    if direction == "long" and snap.pct_from_52w_high is not None:
        vol_ok = snap.volume_ratio is None or snap.volume_ratio >= 1.0
        if snap.pct_from_52w_high <= 0.03 and vol_ok:
            confidence = min(100, confidence + 8)
        elif snap.pct_from_52w_high <= 0.08 and vol_ok:
            confidence = min(100, confidence + 4)
    if direction == "long" and snap.ema200 is not None and snap.ema200 > 0:
        ema_dist = (snap.price - snap.ema200) / snap.ema200
        if 0.02 <= ema_dist <= 0.10:
            confidence = min(100, confidence + 3)
        elif ema_dist > 0.15:
            confidence = max(0, confidence - 3)

    # Volume Profile walk-forward adjustments (same logic as live engine)
    # snap.vpoc is computed from OHLCV sliced to sim_date — fully walk-forward.
    vp_flag = "none"
    if snap.vpoc is not None:
        if snap.in_value_area():
            confidence = max(0, confidence - 3)
            vp_flag = "in_value_area"
        ext = abs(snap.vp_overextension or 0)
        if ext > 2.0:
            confidence = max(0, confidence - 5)
            vp_flag = vp_flag + "+overextended" if vp_flag != "none" else "overextended"
        elif ext > 1.3:
            confidence = max(0, confidence - 3)
        if (direction == "long" and snap.vp_bias == "below_vpoc") or \
           (direction == "short" and snap.vp_bias == "above_vpoc"):
            confidence = min(100, confidence + 4)

    # Relative Strength walk-forward (fetch_ohlcv respects _backtest_cutoff)
    rs_20d = None
    if category in _STOCK_CATEGORIES:
        try:
            rs_20d = compute_relative_strength(symbol, "^GSPC", period=20)
            if rs_20d is not None:
                if rs_20d > 1.2:
                    rs_adj = +5 if direction == "long" else -5
                elif rs_20d < 0.8:
                    rs_adj = -8 if direction == "long" else +5
                else:
                    rs_adj = 0
                confidence = max(0, min(100, confidence + rs_adj))
        except Exception:
            pass

    # Per-category confidence gate: use asset_class_policy offsets — mirrors live engine exactly.
    # pol.min_confidence_offset: +0 large-cap, +5 mid-cap, +10 small-cap, +20 FX, +35 crypto.
    # --- MIRRORED FROM engine.py ---
    effective_min_confidence = min_confidence + pol.min_confidence_offset

    # Stocks 60-69 confidence penalty: this band has 25% avg win rate — but the breakdown
    # reveals it is entirely a volatility-regime problem:
    #   contracting vol: 12.9% win rate, -0.685 pnl_r  ← catastrophic
    #   expanding vol:   27.3% win rate, -0.674 pnl_r  ← very bad
    #   stable vol:      50.0% win rate, +1.290 pnl_r  ← excellent, keep untouched
    # Root cause: in contracting/expanding ATR environments, multiple indicators align
    # mid-move (late momentum or false breakout) inflating confidence without a real edge.
    if category in _STOCK_CATEGORIES and 60 <= confidence < 70:
        if snap.volatility_regime in ("contracting", "expanding"):
            confidence -= 7  # push most out of the 60-69 danger zone

    # 50-59 stable vol penalty: both stocks and indices show negative/near-zero pnl_r
    # in stable vol at low confidence — quiet drift market, weak signal = noise trade.
    #   stocks 50-54 stable:  26.7% win rate, -0.246 pnl_r
    #   stocks 55-59 stable:  22.2% win rate, -0.519 pnl_r
    #   indices 50-54 stable: 37.2% win rate, +0.009 pnl_r (near-zero, remove noise)
    #   indices 55-59 stable: 33.3% win rate, -0.251 pnl_r
    if category in (*_STOCK_CATEGORIES, "indices") and 50 <= confidence < 60:
        if snap.volatility_regime == "stable":
            confidence -= 7

    if confidence < effective_min_confidence:
        return None

    # SL/TP (VP-aware via compute_atr_levels — uses snap.hvn_levels/val/vah)
    sl, tp = compute_atr_levels(snap.price, risk_tier, snapshot=snap, direction=direction)
    rr = calc_risk_reward(snap.price, sl, tp)

    # --- MIRRORED FROM engine.py ---
    _min_rr = _min_rr_floor_for_build(category, pol)
    if rr is None or rr < _min_rr:
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
        # Volume Profile walk-forward data (for analysis/metrics)
        "vpoc": snap.vpoc,
        "vah": snap.vah,
        "val": snap.val,
        "vp_bias": snap.vp_bias,
        "vp_flag": vp_flag,  # what VP adjustment fired (for analytics)
        "in_value_area": snap.in_value_area(),
        # Relative Strength walk-forward data
        "rs_20d": rs_20d,
        # Market regime (populated by caller after build_regime_calendar)
        "market_regime": None,
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
    # Large cap stocks
    "AAPL": "stocks_large", "MSFT": "stocks_large", "GOOGL": "stocks_large",
    "NVDA": "stocks_large", "META": "stocks_large", "AMZN": "stocks_large",
    "TSLA": "stocks_large", "AVGO": "stocks_large",
    "JPM": "stocks_large", "BAC": "stocks_large", "GS": "stocks_large",
    "JNJ": "stocks_large", "UNH": "stocks_large",
    "XOM": "stocks_large", "CVX": "stocks_large", "BP": "stocks_large", "SHEL": "stocks_large",
    "NEM": "stocks_large", "PG": "stocks_large", "WMT": "stocks_large", "V": "stocks_large",
    # Mid cap stocks
    "PLTR": "stocks_mid", "SMCI": "stocks_mid", "IONQ": "stocks_mid",
    "RIVN": "stocks_mid", "ENPH": "stocks_mid", "FSLR": "stocks_mid",
    "CCJ": "stocks_mid", "MP": "stocks_mid",
    "SOFI": "stocks_mid", "HOOD": "stocks_mid",
    "RIG": "stocks_mid", "HP": "stocks_mid",
    "HIMS": "stocks_mid", "RKLB": "stocks_mid",
    # Small cap stocks
    "BBAI": "stocks_small", "SOUN": "stocks_small", "LUNR": "stocks_small",
    "ACHR": "stocks_small", "ASTS": "stocks_small", "JOBY": "stocks_small",
    "ACMR": "stocks_small", "ARRY": "stocks_small", "RXRX": "stocks_small", "OPEN": "stocks_small",
    # Commodities
    "CL=F": "commodities", "BZ=F": "commodities", "GC=F": "commodities",
    "NG=F": "commodities", "SI=F": "commodities",
    # FX + Crypto
    "EURUSD=X": "other", "GBPUSD=X": "other", "USDJPY=X": "other",
    "BTC-USD": "other", "ETH-USD": "other",
    # Indices
    "^GSPC": "indices", "^DJI": "indices", "^IXIC": "indices", "^RUT": "indices",
    "^FTSE": "indices", "^GDAXI": "indices", "^FCHI": "indices", "^STOXX50E": "indices",
    "^N225": "indices", "^HSI": "indices", "000001.SS": "indices",
    "^KS11": "indices", "^BSESN": "indices", "^AXJO": "indices", "^NSEI": "indices",
    "^VIX": "indices",
}


def _guess_category(symbol: str) -> str:
    return _CATEGORY_MAP.get(symbol, "stocks_large")


def run_walkforward_backtest(
    start_date: date,
    end_date: date,
    symbols: Optional[list[str]] = None,
    risk_tier: str = "moderate",
    min_confidence: int = 40,
    period: str = "5y",
    in_sample_days: int = 252,   # 1 year in-sample
    out_sample_days: int = 63,   # 1 quarter out-of-sample
    max_outcome_bars: int = OUTCOME_MAX_BARS,
) -> dict[str, Any]:
    """
    Walk-forward analysis: split the date range into rolling IS/OOS windows
    and compare in-sample vs out-of-sample performance.

    This is the industry-standard method to detect overfitting — a strategy
    that degrades significantly OOS is likely curve-fit to the IS period.

    Args:
        start_date: First date (in-sample window starts here).
        end_date: Last date (last OOS window ends here).
        in_sample_days: Number of trading days per in-sample window (default 252 = 1 year).
        out_sample_days: Number of trading days per OOS window (default 63 = 1 quarter).

    Returns:
        Dict with 'windows' (list of IS/OOS pairs), 'is_metrics', 'oos_metrics',
        'degradation' (OOS win_rate / IS win_rate ratio), and overall summary.
    """
    if symbols is None:
        symbols = get_engine_symbols()

    logger.info(
        "[WF] Walk-forward: %s → %s | IS=%d OOS=%d days | %d symbols",
        start_date, end_date, in_sample_days, out_sample_days, len(symbols),
    )

    # Pre-fetch once for the entire range
    tech.clear_cache()
    ohlcv_data = prefetch_symbols(symbols, period=period)
    for _rs_sym in ["^GSPC", "^VIX", "XLK", "XLE", "XLF", "XLV", "XLY", "XLI", "XLB"]:
        tech.fetch_ohlcv(_rs_sym, period=period)

    if not ohlcv_data:
        return {"error": "no_data_fetched", "windows": []}

    all_trading_days = get_trading_days(ohlcv_data, start_date, end_date)
    if not all_trading_days:
        return {"error": "no_trading_days", "windows": []}

    # Build rolling windows: each window = IS block + OOS block
    windows = []
    i = 0
    while i + in_sample_days + out_sample_days <= len(all_trading_days):
        is_days = all_trading_days[i : i + in_sample_days]
        oos_days = all_trading_days[i + in_sample_days : i + in_sample_days + out_sample_days]
        windows.append({
            "is_start": is_days[0],
            "is_end": is_days[-1],
            "oos_start": oos_days[0],
            "oos_end": oos_days[-1],
        })
        i += out_sample_days  # advance by OOS step (rolling, not anchored)

    if not windows:
        return {"error": "date_range_too_short_for_walkforward", "windows": [], "required_days": in_sample_days + out_sample_days}

    logger.info("[WF] %d walk-forward windows", len(windows))

    # Pre-compute regime calendar once for all days
    regime_calendar = build_regime_calendar(all_trading_days, ohlcv_data)

    all_is_signals: list[dict] = []
    all_oos_signals: list[dict] = []
    window_results = []

    for w_idx, w in enumerate(windows):
        is_start, is_end = w["is_start"], w["is_end"]
        oos_start, oos_end = w["oos_start"], w["oos_end"]

        # Run IS replay
        is_signals = _replay_window(
            ohlcv_data, regime_calendar, symbols, is_start, is_end,
            risk_tier, min_confidence, max_outcome_bars,
        )
        # Run OOS replay
        oos_signals = _replay_window(
            ohlcv_data, regime_calendar, symbols, oos_start, oos_end,
            risk_tier, min_confidence, max_outcome_bars,
        )

        is_m = compute_metrics(is_signals, total_trading_days=in_sample_days)
        oos_m = compute_metrics(oos_signals, total_trading_days=out_sample_days)

        all_is_signals.extend(is_signals)
        all_oos_signals.extend(oos_signals)

        window_results.append({
            "window": w_idx + 1,
            "is_start": is_start.isoformat(),
            "is_end": is_end.isoformat(),
            "oos_start": oos_start.isoformat(),
            "oos_end": oos_end.isoformat(),
            "is_trades": is_m.get("total_trades", 0),
            "is_win_rate": is_m.get("win_rate"),
            "is_sharpe": is_m.get("sharpe_ratio"),
            "oos_trades": oos_m.get("total_trades", 0),
            "oos_win_rate": oos_m.get("win_rate"),
            "oos_sharpe": oos_m.get("sharpe_ratio"),
        })

        logger.info(
            "[WF] Window %d: IS WR=%.1f%% (%d trades) | OOS WR=%.1f%% (%d trades)",
            w_idx + 1,
            (is_m.get("win_rate") or 0) * 100, is_m.get("total_trades", 0),
            (oos_m.get("win_rate") or 0) * 100, oos_m.get("total_trades", 0),
        )

    # Aggregate IS vs OOS metrics
    total_is_days = len(windows) * in_sample_days
    total_oos_days = len(windows) * out_sample_days
    overall_is = compute_metrics(all_is_signals, total_trading_days=total_is_days)
    overall_oos = compute_metrics(all_oos_signals, total_trading_days=total_oos_days)

    # Degradation ratio: how much does OOS win_rate fall vs IS?
    is_wr = overall_is.get("win_rate") or 0
    oos_wr = overall_oos.get("win_rate") or 0
    degradation_ratio = round(oos_wr / is_wr, 4) if is_wr > 0 else None

    # Interpret: >0.85 = good, 0.70-0.85 = acceptable, <0.70 = likely overfit
    if degradation_ratio is None:
        robustness = "insufficient_data"
    elif degradation_ratio >= 0.85:
        robustness = "robust"
    elif degradation_ratio >= 0.70:
        robustness = "acceptable"
    else:
        robustness = "likely_overfit"

    return {
        "windows": window_results,
        "in_sample_days": in_sample_days,
        "out_sample_days": out_sample_days,
        "total_windows": len(window_results),
        "is_metrics": overall_is,
        "oos_metrics": overall_oos,
        "degradation_ratio": degradation_ratio,
        "robustness": robustness,
        "symbols_tested": list(ohlcv_data.keys()),
    }


def _replay_window(
    ohlcv_data: dict,
    regime_calendar: dict,
    symbols: list[str],
    start: date,
    end: date,
    risk_tier: str,
    min_confidence: int,
    max_outcome_bars: int,
) -> list[dict]:
    """Run a single IS or OOS replay window and return signals with outcomes."""
    trading_days = get_trading_days(ohlcv_data, start, end)
    signals: list[dict] = []

    for sim_date in trading_days:
        cutoff_ts = pd.Timestamp(sim_date)
        tech.set_backtest_mode(cutoff_ts)
        day_regime = regime_calendar.get(sim_date, "neutral")

        for symbol in symbols:
            try:
                sig = _build_signal(symbol, sim_date, risk_tier, min_confidence, None, day_regime)
                if sig:
                    sig["market_regime"] = day_regime
                    signals.append(sig)
            except Exception as e:
                logger.debug("[WF] Error building signal %s @%s: %s", symbol, sim_date, e)

    tech.clear_backtest_mode()

    for sig in signals:
        _simulate_signal_outcome(sig, ohlcv_data, max_outcome_bars)

    return signals
