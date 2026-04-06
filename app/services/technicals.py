"""
Technical analysis module v2: ATR, RSI, MACD, Bollinger Bands, ADX, Stochastic,
OBV, Support/Resistance (swing high/low), Fibonacci retracements, confidence score.
Pure pandas/numpy — no external TA library needed.
"""
from __future__ import annotations

import concurrent.futures
import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Price / OHLCV cache
# ---------------------------------------------------------------------------

_ohlcv_cache: dict[str, pd.DataFrame] = {}
# Separate cache for intraday (key = symbol) to avoid overwriting daily
_ohlcv_intraday_cache: dict[str, pd.DataFrame] = {}


def clear_cache() -> None:
    _ohlcv_cache.clear()
    _ohlcv_intraday_cache.clear()


def fetch_ohlcv(symbol: str, period: str = "90d", interval: Optional[str] = None) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV history from yfinance with caching. Returns None on failure.
    interval: None or "1d" for daily; "1h" for hourly (uses 30d period, cached separately).
    """
    use_interval = interval or "1d"
    cache_key = symbol if use_interval == "1d" else f"{symbol}_{use_interval}"
    cache = _ohlcv_cache if use_interval == "1d" else _ohlcv_intraday_cache
    if cache_key in cache:
        cached = cache[cache_key]
        return cached if not cached.empty else None
    # yfinance logs and some builds also print "$TICKER: possibly delisted" to the console;
    # keep loggers quiet (failures still return None / empty df).
    log = logging.getLogger("yfinance")
    old_level = log.level
    log.setLevel(logging.CRITICAL)
    try:
        ticker = yf.Ticker(symbol)
        kwargs = {"period": "30d" if use_interval == "1h" else period, "interval": use_interval}
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(ticker.history, **kwargs)
            try:
                df = future.result(timeout=30)
            except concurrent.futures.TimeoutError:
                logger.warning("[TECHNICALS] yfinance timeout (30s) for symbol=%s", symbol)
                cache[cache_key] = pd.DataFrame()
                return None
        if df is None or df.empty or "Close" not in df.columns:
            cache[cache_key] = pd.DataFrame()
            return None
        cache[cache_key] = df
        return df
    except Exception:
        cache[cache_key] = pd.DataFrame()
        return None
    finally:
        log.setLevel(old_level)


def get_cached_price(symbol: str) -> Optional[float]:
    df = fetch_ohlcv(symbol)
    if df is None or df.empty:
        return None
    return float(df["Close"].iloc[-1])


def get_cached_previous_close(symbol: str) -> Optional[float]:
    """Previous trading day close for divergence / trend. None if not enough data."""
    df = fetch_ohlcv(symbol)
    if df is None or len(df) < 2:
        return None
    return float(df["Close"].iloc[-2])


def get_return_correlation(symbol_a: str, symbol_b: str, period_days: int = 60) -> Optional[float]:
    """
    Rolling correlation of daily returns between two symbols over the last period_days.
    Used for correlation-aware filters to avoid over-concentration. Returns None if
    insufficient data or symbols are the same.
    """
    if not symbol_a or not symbol_b or symbol_a == symbol_b:
        return None
    df_a = fetch_ohlcv(symbol_a, period="90d")
    df_b = fetch_ohlcv(symbol_b, period="90d")
    if df_a is None or df_b is None or len(df_a) < period_days or len(df_b) < period_days:
        return None
    ret_a = df_a["Close"].pct_change().dropna()
    ret_b = df_b["Close"].pct_change().dropna()
    common = ret_a.index.intersection(ret_b.index)
    if len(common) < max(10, period_days // 2):
        return None
    try:
        c = ret_a.loc[common].corr(ret_b.loc[common])
        return float(c) if pd.notna(c) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core indicators
# ---------------------------------------------------------------------------

def calc_atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    if len(df) < period + 1:
        return None
    high, low, close = df["High"], df["Low"], df["Close"].shift(1)
    tr = pd.concat([high - low, (high - close).abs(), (low - close).abs()], axis=1).max(axis=1)
    val = tr.rolling(window=period).mean().iloc[-1]
    return float(val) if pd.notna(val) else None


def calc_rsi(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    if len(df) < period + 1:
        return None
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return float(val) if pd.notna(val) else None


def calc_macd(
    df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
) -> Optional[tuple[float, float, float]]:
    if len(df) < slow + signal:
        return None
    close = df["Close"]
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    m, s, h = macd_line.iloc[-1], signal_line.iloc[-1], hist.iloc[-1]
    if any(pd.isna(x) for x in (m, s, h)):
        return None
    return (float(m), float(s), float(h))


def calc_bollinger(
    df: pd.DataFrame, period: int = 20, std_dev: float = 2.0
) -> Optional[tuple[float, float, float]]:
    if len(df) < period:
        return None
    close = df["Close"]
    sma = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    u, m, l_ = (sma + std_dev * std).iloc[-1], sma.iloc[-1], (sma - std_dev * std).iloc[-1]
    if any(pd.isna(x) for x in (u, m, l_)):
        return None
    return (float(u), float(m), float(l_))


def calc_avg_daily_move(df: pd.DataFrame, period: int = 20) -> Optional[float]:
    if len(df) < period + 1:
        return None
    pct = df["Close"].pct_change().abs().dropna().tail(period)
    return float(pct.mean()) if len(pct) > 0 else None


# ---------------------------------------------------------------------------
# New indicators: ADX, Stochastic, OBV
# ---------------------------------------------------------------------------

def calc_adx(df: pd.DataFrame, period: int = 14) -> Optional[tuple[float, float, float]]:
    """ADX, +DI, -DI. Returns (adx, plus_di, minus_di) or None."""
    if len(df) < period * 2 + 1:
        return None
    high, low, close = df["High"], df["Low"], df["Close"]
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    # Zero out whichever is smaller
    plus_dm[plus_dm < minus_dm] = 0
    minus_dm[minus_dm < plus_dm] = 0

    tr = pd.concat([
        high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr)
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    a, p, m = adx.iloc[-1], plus_di.iloc[-1], minus_di.iloc[-1]
    if any(pd.isna(x) for x in (a, p, m)):
        return None
    return (float(a), float(p), float(m))


def calc_stochastic(
    df: pd.DataFrame, k_period: int = 14, d_period: int = 3
) -> Optional[tuple[float, float]]:
    """Stochastic Oscillator (%K, %D). Returns (k, d) or None."""
    if len(df) < k_period + d_period:
        return None
    low_min = df["Low"].rolling(window=k_period).min()
    high_max = df["High"].rolling(window=k_period).max()
    denom = high_max - low_min
    k = 100 * ((df["Close"] - low_min) / denom.replace(0, np.nan))
    d = k.rolling(window=d_period).mean()
    kv, dv = k.iloc[-1], d.iloc[-1]
    if any(pd.isna(x) for x in (kv, dv)):
        return None
    return (float(kv), float(dv))


def calc_obv(df: pd.DataFrame) -> Optional[tuple[float, float]]:
    """On-Balance Volume. Returns (obv, obv_slope_5d) or None."""
    if len(df) < 6 or "Volume" not in df.columns:
        return None
    direction = np.sign(df["Close"].diff())
    obv = (direction * df["Volume"]).cumsum()
    if obv.isna().all():
        return None
    current = float(obv.iloc[-1])
    slope = float(obv.iloc[-1] - obv.iloc[-5]) if len(obv) >= 5 else 0
    return (current, slope)


# ---------------------------------------------------------------------------
# Support / Resistance detection (swing high/low method)
# ---------------------------------------------------------------------------

def detect_support_resistance(
    df: pd.DataFrame, window: int = 5, max_levels: int = 5
) -> tuple[list[float], list[float]]:
    """
    Detect support and resistance levels using swing highs/lows.
    Returns (supports, resistances) sorted by proximity to current price.
    """
    if len(df) < window * 2 + 1:
        return [], []
    highs = df["High"].values
    lows = df["Low"].values
    price = float(df["Close"].iloc[-1])

    resistances: list[float] = []
    supports: list[float] = []

    for i in range(window, len(df) - window):
        # Swing high: higher than `window` bars on each side
        if all(highs[i] >= highs[i - j] for j in range(1, window + 1)) and \
           all(highs[i] >= highs[i + j] for j in range(1, window + 1)):
            resistances.append(float(highs[i]))
        # Swing low
        if all(lows[i] <= lows[i - j] for j in range(1, window + 1)) and \
           all(lows[i] <= lows[i + j] for j in range(1, window + 1)):
            supports.append(float(lows[i]))

    # Deduplicate close levels (within 0.5% of each other)
    def _dedup(levels: list[float]) -> list[float]:
        if not levels:
            return []
        levels.sort()
        result = [levels[0]]
        for lv in levels[1:]:
            if abs(lv - result[-1]) / max(result[-1], 0.01) > 0.005:
                result.append(lv)
        return result

    supports = _dedup(supports)
    resistances = _dedup(resistances)

    # Sort by proximity to current price
    supports.sort(key=lambda s: abs(s - price))
    resistances.sort(key=lambda r: abs(r - price))

    return supports[:max_levels], resistances[:max_levels]


# ---------------------------------------------------------------------------
# Fibonacci retracement levels
# ---------------------------------------------------------------------------

def calc_fibonacci_levels(
    df: pd.DataFrame, lookback: int = 50
) -> Optional[dict[str, float]]:
    """
    Fibonacci retracement levels from the recent swing range.
    Returns dict with keys: '0%', '23.6%', '38.2%', '50%', '61.8%', '78.6%', '100%'.
    """
    if len(df) < lookback:
        return None
    recent = df.tail(lookback)
    high = float(recent["High"].max())
    low = float(recent["Low"].min())
    diff = high - low
    if diff <= 0:
        return None
    return {
        "0%": round(high, 4),
        "23.6%": round(high - diff * 0.236, 4),
        "38.2%": round(high - diff * 0.382, 4),
        "50%": round(high - diff * 0.5, 4),
        "61.8%": round(high - diff * 0.618, 4),
        "78.6%": round(high - diff * 0.786, 4),
        "100%": round(low, 4),
    }


# ---------------------------------------------------------------------------
# Composite snapshot (extended)
# ---------------------------------------------------------------------------

@dataclass
class TechnicalSnapshot:
    symbol: str
    price: float
    atr: Optional[float] = None
    atr_pct: Optional[float] = None
    rsi: Optional[float] = None
    macd: Optional[tuple[float, float, float]] = None
    bollinger: Optional[tuple[float, float, float]] = None
    adx: Optional[tuple[float, float, float]] = None  # (adx, +di, -di)
    stochastic: Optional[tuple[float, float]] = None   # (%K, %D)
    obv: Optional[tuple[float, float]] = None           # (value, slope_5d)
    supports: list[float] = field(default_factory=list)
    resistances: list[float] = field(default_factory=list)
    fibonacci: Optional[dict[str, float]] = None
    avg_daily_move: Optional[float] = None
    trend_bias: str = "neutral"
    trend_strength: str = "weak"  # "weak" | "moderate" | "strong"
    vwap: Optional[float] = None  # volume-weighted average price (e.g. 20d)
    volatility_regime: str = "stable"  # "expanding" | "contracting" | "stable" (ATR slope)

    @property
    def price_above_vwap(self) -> bool:
        return self.vwap is not None and self.price > self.vwap

    @property
    def is_overbought(self) -> bool:
        return self.rsi is not None and self.rsi > 70

    @property
    def is_oversold(self) -> bool:
        return self.rsi is not None and self.rsi < 30

    @property
    def macd_bullish(self) -> bool:
        return self.macd is not None and self.macd[2] > 0

    @property
    def macd_bearish(self) -> bool:
        return self.macd is not None and self.macd[2] < 0

    @property
    def adx_trending(self) -> bool:
        return self.adx is not None and self.adx[0] > 25

    @property
    def adx_strong_trend(self) -> bool:
        return self.adx is not None and self.adx[0] > 40

    @property
    def stoch_overbought(self) -> bool:
        return self.stochastic is not None and self.stochastic[0] > 80

    @property
    def stoch_oversold(self) -> bool:
        return self.stochastic is not None and self.stochastic[0] < 20

    @property
    def obv_confirming_up(self) -> bool:
        return self.obv is not None and self.obv[1] > 0

    @property
    def obv_confirming_down(self) -> bool:
        return self.obv is not None and self.obv[1] < 0

    @property
    def near_upper_band(self) -> bool:
        if self.bollinger is None:
            return False
        upper, mid, lower = self.bollinger
        bw = upper - lower
        return bw > 0 and (self.price - mid) / bw > 0.35

    @property
    def near_lower_band(self) -> bool:
        if self.bollinger is None:
            return False
        upper, mid, lower = self.bollinger
        bw = upper - lower
        return bw > 0 and (mid - self.price) / bw > 0.35

    def nearest_support_below(self) -> Optional[float]:
        for s in sorted(self.supports, reverse=True):
            if s < self.price:
                return s
        return None

    def nearest_resistance_above(self) -> Optional[float]:
        for r in sorted(self.resistances):
            if r > self.price:
                return r
        return None

    def nearest_support_above(self) -> Optional[float]:
        for s in sorted(self.supports):
            if s > self.price:
                return s
        return None

    def nearest_resistance_below(self) -> Optional[float]:
        for r in sorted(self.resistances, reverse=True):
            if r < self.price:
                return r
        return None


def calc_atr_regime(df: pd.DataFrame, lookback: int = 10, period: int = 14) -> str:
    """
    ATR volatility regime: expanding (ATR rising), contracting (ATR falling), stable.
    Used to adjust SL/TP width: contracting -> slightly wider to avoid whipsaw.
    """
    if df is None or len(df) < period + lookback + 1:
        return "stable"
    atr_now = calc_atr(df, period=period)
    df_older = df.iloc[:-lookback]
    if len(df_older) < period + 1:
        return "stable"
    atr_older = calc_atr(df_older, period=period)
    if atr_now is None or atr_older is None or atr_older <= 0:
        return "stable"
    change = (atr_now - atr_older) / atr_older
    if change > 0.05:
        return "expanding"
    if change < -0.05:
        return "contracting"
    return "stable"


def calc_vwap(df: pd.DataFrame, period: int = 20) -> Optional[float]:
    """
    Volume-weighted average price over the last `period` bars.
    VWAP = sum(typical_price * volume) / sum(volume), typical_price = (H+L+C)/3.
    """
    if df is None or df.empty or "Volume" not in df.columns:
        return None
    required = ["High", "Low", "Close", "Volume"]
    if not all(c in df.columns for c in required):
        return None
    tail = df[required].tail(period)
    vol = tail["Volume"]
    if vol.sum() <= 0:
        return None
    typical = (tail["High"] + tail["Low"] + tail["Close"]) / 3.0
    vwap = (typical * vol).sum() / vol.sum()
    return float(vwap)


def build_snapshot(symbol: str) -> Optional[TechnicalSnapshot]:
    """Build full technical snapshot for one symbol."""
    df = fetch_ohlcv(symbol)
    if df is None or df.empty or len(df) < 5:
        return None

    price = float(df["Close"].iloc[-1])
    if price <= 0:
        return None

    atr = calc_atr(df)
    atr_pct = (atr / price) if atr else None
    rsi = calc_rsi(df)
    macd = calc_macd(df)
    bollinger = calc_bollinger(df)
    adx = calc_adx(df)
    stochastic = calc_stochastic(df)
    obv = calc_obv(df)
    supports, resistances = detect_support_resistance(df)
    fibonacci = calc_fibonacci_levels(df)
    avg_daily = calc_avg_daily_move(df)
    vwap = calc_vwap(df, period=20)
    vol_regime = calc_atr_regime(df, lookback=10)

    # Trend bias from all indicators
    bull, bear = 0, 0

    if rsi is not None:
        if rsi > 55:
            bull += 1
        elif rsi < 45:
            bear += 1
    if macd is not None:
        (bull if macd[2] > 0 else bear).__class__  # dummy; real logic below
        if macd[2] > 0:
            bull += 1
        else:
            bear += 1
    if bollinger is not None:
        upper, mid, lower = bollinger
        if price > mid:
            bull += 1
        elif price < mid:
            bear += 1
    if adx is not None:
        _, plus_di, minus_di = adx
        if plus_di > minus_di:
            bull += 1
        elif minus_di > plus_di:
            bear += 1
    if stochastic is not None:
        k, _ = stochastic
        if k > 55:
            bull += 1
        elif k < 45:
            bear += 1
    if obv is not None:
        if obv[1] > 0:
            bull += 1
        elif obv[1] < 0:
            bear += 1

    if bull > bear:
        trend_bias = "bullish"
    elif bear > bull:
        trend_bias = "bearish"
    else:
        trend_bias = "neutral"

    # Trend strength from ADX
    if adx is not None:
        adx_val = adx[0]
        if adx_val >= 40:
            trend_strength = "strong"
        elif adx_val >= 25:
            trend_strength = "moderate"
        else:
            trend_strength = "weak"
    else:
        trend_strength = "weak"

    def _r(v, d=4):
        return round(v, d) if v is not None else None

    return TechnicalSnapshot(
        symbol=symbol,
        price=round(price, 4),
        atr=_r(atr),
        atr_pct=_r(atr_pct, 6),
        rsi=_r(rsi, 2),
        macd=tuple(round(x, 4) for x in macd) if macd else None,
        bollinger=tuple(round(x, 4) for x in bollinger) if bollinger else None,
        adx=tuple(round(x, 2) for x in adx) if adx else None,
        stochastic=tuple(round(x, 2) for x in stochastic) if stochastic else None,
        obv=obv,
        supports=[round(s, 2) for s in supports],
        resistances=[round(r, 2) for r in resistances],
        fibonacci=fibonacci,
        avg_daily_move=_r(avg_daily, 6),
        trend_bias=trend_bias,
        trend_strength=trend_strength,
        vwap=_r(vwap, 4) if vwap is not None else None,
        volatility_regime=vol_regime,
    )


# ---------------------------------------------------------------------------
# ATR + S/R aware SL/TP
# ---------------------------------------------------------------------------

ATR_MULTIPLIERS = {
    "conservative": (1.5, 2.5),
    "moderate":     (2.0, 3.5),
    "aggressive":   (3.0, 5.0),
}

FALLBACK_SL_TP = {
    "conservative": (0.015, 0.03),
    "moderate":     (0.02, 0.04),
    "aggressive":   (0.03, 0.06),
}


def compute_atr_levels(
    price: float,
    risk_tier: str,
    snapshot: Optional[TechnicalSnapshot] = None,
    direction: str = "long",
) -> tuple[float, float]:
    """
    Compute (stop_loss, take_profit) using ATR + support/resistance awareness.
    For longs: SL snaps to nearest support below (if closer than ATR level).
    For shorts: SL snaps to nearest resistance above.
    TP considers Fibonacci levels and nearest S/R.
    """
    # Base ATR levels (volatility regime adjusts width: contracting -> wider, expanding -> tighter)
    if snapshot and snapshot.atr and snapshot.atr > 0:
        sl_mult, tp_mult = ATR_MULTIPLIERS.get(risk_tier, ATR_MULTIPLIERS["moderate"])
        atr = snapshot.atr
        vol_mult = 1.1 if getattr(snapshot, "volatility_regime", "stable") == "contracting" else (
            0.95 if getattr(snapshot, "volatility_regime", "stable") == "expanding" else 1.0
        )
        atr = atr * vol_mult
        if direction == "short":
            sl_base = price + atr * sl_mult
            tp_base = price - atr * tp_mult
        else:
            sl_base = price - atr * sl_mult
            tp_base = price + atr * tp_mult
    else:
        sl_pct, tp_pct = FALLBACK_SL_TP.get(risk_tier, FALLBACK_SL_TP["moderate"])
        if direction == "short":
            sl_base = price * (1 + sl_pct)
            tp_base = price * (1 - tp_pct)
        else:
            sl_base = price * (1 - sl_pct)
            tp_base = price * (1 + tp_pct)

    sl, tp = sl_base, tp_base

    # S/R-aware adjustment
    if snapshot:
        if direction == "long":
            # SL: snap to nearest support below price if it provides a tighter stop
            support = snapshot.nearest_support_below()
            if support is not None and support < price:
                # Place SL slightly below the support (0.2% buffer)
                sr_sl = support * 0.998
                # Only snap if it's above the ATR-based SL (tighter) and at least 0.5% from price
                min_sl = price * 0.995  # never tighter than 0.5%
                if sr_sl > sl_base and sr_sl < min_sl:
                    sl = sr_sl  # valid: tighter than ATR and far enough from price
                elif sr_sl >= min_sl:
                    pass  # too tight (within 0.5% of price), keep ATR level

            # TP: consider nearest resistance above for a realistic target
            resistance = snapshot.nearest_resistance_above()
            if resistance is not None and resistance > price:
                # Use resistance as TP if it's between price and ATR-based TP
                if resistance < tp_base:
                    tp = resistance * 0.998  # just below resistance
        else:  # short
            resistance = snapshot.nearest_resistance_above()
            if resistance is not None and resistance > price:
                sr_sl = resistance * 1.002
                max_sl = price * 1.005
                if sr_sl < sl_base and sr_sl > max_sl:
                    pass
                elif sr_sl < sl_base:
                    sl = sr_sl

            support = snapshot.nearest_support_below()
            if support is not None and support < price:
                if support > tp_base:
                    tp = support * 1.002

    return round(sl, 2), round(tp, 2)


def estimate_expiry_days(
    price: float,
    take_profit: float,
    snapshot: Optional[TechnicalSnapshot] = None,
    min_days: int = 1,
    max_days: int = 30,
) -> int:
    if not snapshot or not snapshot.avg_daily_move or snapshot.avg_daily_move <= 0:
        return max_days // 2
    distance_pct = abs(take_profit - price) / price if price > 0 else 0
    if distance_pct <= 0:
        return min_days
    estimated = math.ceil(distance_pct / snapshot.avg_daily_move)
    return max(min_days, min(estimated, max_days))


# ---------------------------------------------------------------------------
# Regime-dependent weights (dynamic indicator weights)
# ---------------------------------------------------------------------------

def _regime_direction_weights(regime: Optional[str] = None) -> tuple[float, float]:
    """
    Sentiment vs technical weight for direction. risk_on: trust trend more (less sentiment).
    risk_off: slightly more sentiment; cautious: balanced.
    """
    if regime == "risk_on":
        return (0.20, 0.80)
    if regime == "risk_off":
        return (0.25, 0.75)
    # cautious or None
    return (0.30, 0.70)


def _regime_tech_weights(regime: Optional[str] = None) -> tuple[float, float]:
    """
    Trend (MACD, ADX) vs mean-reversion (RSI, Stochastic, Bollinger) multiplier.
    risk_on: favor trend; risk_off: favor mean reversion.
    """
    if regime == "risk_on":
        return (1.2, 0.85)
    if regime == "risk_off":
        return (0.85, 1.2)
    return (1.0, 1.0)


def _regime_rsi_thresholds(regime: Optional[str] = None) -> tuple[float, float, float, float]:
    """
    (bull_bar, bear_bar, overbought, oversold). risk_off: stricter (60/40, 65/35).
    risk_on: more lenient (52/48, 72/28). cautious: default (55/45, 70/30).
    """
    if regime == "risk_off":
        return (60.0, 40.0, 65.0, 35.0)
    if regime == "risk_on":
        return (52.0, 48.0, 72.0, 28.0)
    return (55.0, 45.0, 70.0, 30.0)


def _regime_stoch_thresholds(regime: Optional[str] = None) -> tuple[float, float, float, float]:
    """
    (bull_bar, bear_bar, overbought, oversold). risk_off: stricter; risk_on: lenient.
    """
    if regime == "risk_off":
        return (60.0, 40.0, 75.0, 25.0)
    if regime == "risk_on":
        return (52.0, 48.0, 85.0, 15.0)
    return (55.0, 45.0, 80.0, 20.0)


def _style_adjust_direction_weights(
    sent_w: float,
    tech_w: float,
    regime: Optional[str],
    asset_direction_style: Optional[str],
) -> tuple[float, float]:
    """Shift weight toward headlines for macro-linked assets (oil, FX, crypto, indices)."""
    if asset_direction_style == "macro_linked":
        bump = 0.15 if regime == "risk_off" else 0.10
        sent_w = min(0.50, sent_w + bump)
        tech_w = 1.0 - sent_w
    elif asset_direction_style == "index_macro":
        bump = 0.12 if regime == "risk_off" else 0.06
        sent_w = min(0.44, sent_w + bump)
        tech_w = 1.0 - sent_w
    return sent_w, tech_w


# ---------------------------------------------------------------------------
# Direction determination (enhanced with ADX, Stochastic, OBV)
# ---------------------------------------------------------------------------

def determine_direction(
    sentiment_avg: Optional[float],
    snapshot: Optional[TechnicalSnapshot] = None,
    regime: Optional[str] = None,
    asset_direction_style: Optional[str] = None,
) -> str:
    """
    Decide long vs short. Weights are regime-dependent: risk_on → more technical/trend;
    risk_off → more mean-reversion weight; cautious → balanced (30% sentiment, 70% technical).
    Technicals: RSI, MACD, Bollinger, ADX (+DI vs -DI), Stochastic, OBV.
    """
    sent_w, tech_w = _regime_direction_weights(regime)
    sent_w, tech_w = _style_adjust_direction_weights(sent_w, tech_w, regime, asset_direction_style)
    score = 0.0

    if sentiment_avg is not None:
        score += sentiment_avg * sent_w

    if snapshot:
        trend_mult, mean_rev_mult = _regime_tech_weights(regime)
        trend_sum = 0.0
        trend_count = 0
        mean_rev_sum = 0.0
        mean_rev_count = 0

        # Mean-reversion: RSI, Stochastic, Bollinger (regime-specific thresholds)
        rsi_bull, rsi_bear, _, _ = _regime_rsi_thresholds(regime)
        st_bull, st_bear, _, _ = _regime_stoch_thresholds(regime)
        if snapshot.rsi is not None:
            mean_rev_count += 1
            if snapshot.rsi > rsi_bull:
                mean_rev_sum += 1.0
            elif snapshot.rsi < rsi_bear:
                mean_rev_sum -= 1.0
            else:
                mid_rsi = (rsi_bull + rsi_bear) / 2
                half = (rsi_bull - rsi_bear) / 2 or 1
                mean_rev_sum += max(-1, min(1, (snapshot.rsi - mid_rsi) / half))

        if snapshot.bollinger is not None:
            mean_rev_count += 1
            upper, mid, lower = snapshot.bollinger
            bw = upper - lower
            if bw > 0:
                mean_rev_sum += max(-1, min(1, (snapshot.price - mid) / bw * 2))

        if snapshot.stochastic is not None:
            mean_rev_count += 1
            k, _ = snapshot.stochastic
            if k > st_bull:
                mean_rev_sum += 0.8
            elif k < st_bear:
                mean_rev_sum -= 0.8

        # Trend: MACD, ADX, OBV
        if snapshot.macd is not None:
            trend_count += 1
            trend_sum += 1.0 if snapshot.macd[2] > 0 else -1.0

        if snapshot.adx is not None:
            trend_count += 1
            _, plus_di, minus_di = snapshot.adx
            if plus_di > minus_di:
                trend_sum += 1.0
            elif minus_di > plus_di:
                trend_sum -= 1.0

        if snapshot.obv is not None:
            trend_count += 1
            trend_sum += 0.6 if snapshot.obv[1] > 0 else -0.6

        # Weighted blend: trend and mean-rev each in [-1,1], combined by regime multipliers
        trend_avg = (trend_sum / trend_count) if trend_count > 0 else None
        mean_rev_avg = (mean_rev_sum / mean_rev_count) if mean_rev_count > 0 else None
        if trend_avg is not None and mean_rev_avg is not None:
            total_mult = trend_mult + mean_rev_mult
            combined = (trend_avg * trend_mult + mean_rev_avg * mean_rev_mult) / total_mult
        elif trend_avg is not None:
            combined = trend_avg
        elif mean_rev_avg is not None:
            combined = mean_rev_avg
        else:
            combined = 0.0
        score += max(-1.0, min(1.0, combined)) * tech_w

    return "long" if score >= 0 else "short"


# ---------------------------------------------------------------------------
# Indicator conflict rules (reduce confidence when key indicators disagree)
# ---------------------------------------------------------------------------

def get_indicator_conflict_penalty(
    snapshot: Optional[TechnicalSnapshot],
    direction: str,
    regime: Optional[str] = None,
    penalty_scale: float = 1.0,
) -> int:
    """
    Returns a penalty (0–25) when key indicators disagree with the chosen direction.
    E.g. RSI overbought but direction long, or MACD bearish but direction long.
    """
    if not snapshot:
        return 0
    penalty = 0
    is_long = direction == "long"
    rsi_bull, rsi_bear, rsi_ob, rsi_os = _regime_rsi_thresholds(regime)
    st_bull, st_bear, st_ob, st_os = _regime_stoch_thresholds(regime)

    # RSI vs direction: overbought + long, or oversold + short
    if snapshot.rsi is not None:
        if is_long and snapshot.rsi >= rsi_ob:
            penalty += 8
        elif not is_long and snapshot.rsi <= rsi_os:
            penalty += 8

    # MACD vs direction: bearish + long, or bullish + short
    if snapshot.macd is not None:
        hist = snapshot.macd[2]
        if is_long and hist < 0:
            penalty += 7
        elif not is_long and hist > 0:
            penalty += 7

    # ADX DI: -DI > +DI suggests bearish trend; +DI > -DI suggests bullish
    if snapshot.adx is not None:
        _, plus_di, minus_di = snapshot.adx
        if is_long and minus_di > plus_di:
            penalty += 5
        elif not is_long and plus_di > minus_di:
            penalty += 5

    # Stochastic vs direction: overbought + long, or oversold + short
    if snapshot.stochastic is not None:
        k, _ = snapshot.stochastic
        if is_long and k >= st_ob:
            penalty += 5
        elif not is_long and k <= st_os:
            penalty += 5

    # Bollinger: near upper band + long (stretched), near lower + short
    if snapshot.bollinger is not None:
        if is_long and snapshot.near_upper_band:
            penalty += 5
        elif not is_long and snapshot.near_lower_band:
            penalty += 5

    scaled = int(round(penalty * max(0.0, min(1.5, penalty_scale))))
    return min(25, scaled)


# ---------------------------------------------------------------------------
# Confidence score (0–100%)
# ---------------------------------------------------------------------------

def compute_confidence(
    sentiment_avg: Optional[float],
    sentiment_count: int,
    snapshot: Optional[TechnicalSnapshot] = None,
    direction: str = "long",
    regime: Optional[str] = None,
    sentiment_signal_scale: float = 1.0,
    indicator_conflict_multiplier: float = 1.0,
) -> int:
    """
    Confidence score 0–100. Measures how many indicators agree with the direction
    and how strongly. Higher = more conviction. Uses regime-specific RSI/Stochastic
    thresholds when regime is provided.
    """
    points = 0.0
    max_points = 0.0

    is_long = direction == "long"
    rsi_bull, rsi_bear, rsi_ob, rsi_os = _regime_rsi_thresholds(regime)
    st_bull, st_bear, st_ob, st_os = _regime_stoch_thresholds(regime)

    ss = max(0.5, min(1.6, sentiment_signal_scale))

    # Sentiment alignment (15 pts)
    max_points += 15
    if sentiment_avg is not None:
        if (is_long and sentiment_avg > 0) or (not is_long and sentiment_avg < 0):
            points += min(15.0, abs(sentiment_avg) * 15.0 * ss)

    # Mention volume (10 pts) — more mentions = more confidence
    max_points += 10
    if sentiment_count > 0:
        points += min(10.0, math.log1p(sentiment_count) * 3.0 * ss)

    if not snapshot:
        return max(0, min(100, int(points / max_points * 100))) if max_points > 0 else 0

    # ADX trend strength (15 pts)
    max_points += 15
    if snapshot.adx is not None:
        adx_val, plus_di, minus_di = snapshot.adx
        trend_aligns = (is_long and plus_di > minus_di) or (not is_long and minus_di > plus_di)
        if trend_aligns:
            if adx_val >= 40:
                points += 15
            elif adx_val >= 25:
                points += 10
            elif adx_val >= 15:
                points += 5

    # RSI alignment (15 pts) — regime-specific thresholds
    max_points += 15
    if snapshot.rsi is not None:
        if is_long:
            if rsi_bear <= snapshot.rsi <= rsi_ob:
                points += 15  # healthy bull territory
            elif snapshot.rsi < rsi_os:
                points += 12  # oversold bounce potential
            elif snapshot.rsi < rsi_bear:
                points += 8
        else:
            if rsi_os <= snapshot.rsi <= rsi_bull:
                points += 15
            elif snapshot.rsi > rsi_ob:
                points += 12
            elif snapshot.rsi > rsi_bull:
                points += 8

    # MACD alignment (15 pts)
    max_points += 15
    if snapshot.macd is not None:
        _, _, hist = snapshot.macd
        if (is_long and hist > 0) or (not is_long and hist < 0):
            points += 15
        elif abs(hist) / max(snapshot.price, 1) < 0.001:
            points += 5  # near crossover

    # Stochastic alignment (10 pts) — regime-specific thresholds
    max_points += 10
    if snapshot.stochastic is not None:
        k, d = snapshot.stochastic
        if is_long:
            if st_os <= k <= st_ob and k > d:
                points += 10
            elif k < st_os:
                points += 7  # oversold, bullish potential
        else:
            if st_os <= k <= st_ob and k < d:
                points += 10
            elif k > st_ob:
                points += 7

    # OBV confirmation (10 pts)
    max_points += 10
    if snapshot.obv is not None:
        if (is_long and snapshot.obv[1] > 0) or (not is_long and snapshot.obv[1] < 0):
            points += 10

    # Bollinger position (10 pts)
    max_points += 10
    if snapshot.bollinger is not None:
        upper, mid, lower = snapshot.bollinger
        if is_long:
            if snapshot.price > mid and not snapshot.near_upper_band:
                points += 10
            elif snapshot.near_lower_band:
                points += 8  # bounce potential
        else:
            if snapshot.price < mid and not snapshot.near_lower_band:
                points += 10
            elif snapshot.near_upper_band:
                points += 8

    # VWAP confirmation (5 pts) — price above VWAP supports long, below supports short
    max_points += 5
    if snapshot.vwap is not None and snapshot.price != snapshot.vwap:
        if (is_long and snapshot.price_above_vwap) or (not is_long and not snapshot.price_above_vwap):
            points += 5

    # Indicator conflict penalty: reduce score when key indicators disagree with direction
    conflict_penalty = get_indicator_conflict_penalty(
        snapshot, direction, regime, penalty_scale=indicator_conflict_multiplier
    )
    points = max(0.0, points - conflict_penalty)

    return max(0, min(100, int(points / max_points * 100))) if max_points > 0 else 0


# ---------------------------------------------------------------------------
# Combined ranking score
# ---------------------------------------------------------------------------

def compute_technical_score(
    sentiment_avg: Optional[float],
    sentiment_count: int,
    snapshot: Optional[TechnicalSnapshot] = None,
) -> float:
    """
    Combined ranking score. Higher = stronger signal. Range roughly -3 to +4.
    Enhanced with ADX, Stochastic, OBV, and trend strength.
    """
    score = 0.0

    if sentiment_avg is not None:
        score += sentiment_avg * 1.5

    if sentiment_count > 0:
        score += min(0.5, math.log1p(sentiment_count) * 0.15)

    if not snapshot:
        return score

    # RSI extremes
    if snapshot.rsi is not None:
        if snapshot.rsi > 70 or snapshot.rsi < 30:
            score += 0.5
        elif snapshot.rsi > 60 or snapshot.rsi < 40:
            score += 0.2

    # MACD momentum
    if snapshot.macd is not None:
        price = snapshot.price if snapshot.price > 0 else 1
        score += min(0.4, abs(snapshot.macd[2]) / price * 100)

    # Bollinger extremes
    if snapshot.near_upper_band or snapshot.near_lower_band:
        score += 0.3

    # ADX: strong trends get a boost
    if snapshot.adx is not None:
        adx_val = snapshot.adx[0]
        if adx_val >= 40:
            score += 0.6
        elif adx_val >= 25:
            score += 0.3

    # Stochastic extremes
    if snapshot.stochastic is not None:
        k, _ = snapshot.stochastic
        if k > 80 or k < 20:
            score += 0.3

    # OBV momentum
    if snapshot.obv is not None and snapshot.obv[1] != 0:
        score += 0.2

    return round(score, 4)


# ---------------------------------------------------------------------------
# Risk:Reward ratio
# ---------------------------------------------------------------------------

def calc_risk_reward(
    price: float, stop_loss: float, take_profit: float
) -> Optional[float]:
    """Risk:Reward ratio. E.g. 2.5 means reward is 2.5x the risk."""
    risk = abs(price - stop_loss)
    reward = abs(take_profit - price)
    if risk <= 0:
        return None
    return round(reward / risk, 2)
