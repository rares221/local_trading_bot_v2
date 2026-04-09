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

# ---------------------------------------------------------------------------
# Backtest mode: when set, fetch_ohlcv slices data up to this date.
# Use set_backtest_mode() / clear_backtest_mode() from the replay engine.
# ---------------------------------------------------------------------------
_backtest_cutoff: Optional[pd.Timestamp] = None


def set_backtest_mode(cutoff: Optional[pd.Timestamp]) -> None:
    """Enable/disable backtest mode. When enabled, fetch_ohlcv returns data <= cutoff."""
    global _backtest_cutoff
    _backtest_cutoff = cutoff


def clear_backtest_mode() -> None:
    """Return to live mode (no date slicing)."""
    global _backtest_cutoff
    _backtest_cutoff = None


def _apply_backtest_cutoff(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Slice df to <= _backtest_cutoff if in backtest mode. Returns None if empty after slice."""
    if _backtest_cutoff is None or df is None or df.empty:
        return df
    try:
        sliced = df[df.index <= _backtest_cutoff]
        return sliced if not sliced.empty else None
    except Exception:
        return df


def clear_cache() -> None:
    _ohlcv_cache.clear()
    _ohlcv_intraday_cache.clear()


def fetch_ohlcv(symbol: str, period: str = "90d", interval: Optional[str] = None) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV history from yfinance with caching. Returns None on failure.
    interval: None or "1d" for daily; "1h" for hourly (uses 30d period, cached separately).
    In backtest mode (_backtest_cutoff set), returns data sliced to that date.
    """
    use_interval = interval or "1d"
    cache_key = symbol if use_interval == "1d" else f"{symbol}_{use_interval}"
    cache = _ohlcv_cache if use_interval == "1d" else _ohlcv_intraday_cache
    if cache_key in cache:
        cached = cache[cache_key]
        if cached.empty:
            return None
        return _apply_backtest_cutoff(cached)
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
        return _apply_backtest_cutoff(df)
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


def compute_relative_strength(symbol: str, benchmark: str, period: int = 20) -> Optional[float]:
    """
    Relative Strength ratio of symbol vs benchmark over N trading days.

    Returns (1 + cumulative_return_symbol) / (1 + cumulative_return_benchmark).
    > 1.0  = symbol outperforms benchmark (strong)
    < 1.0  = symbol underperforms benchmark (weak)
    = 1.0  = in line with benchmark

    Uses the existing fetch_ohlcv() cache and respects backtest mode cutoff.
    Returns None if data is insufficient or an error occurs.
    Output is clamped to [0.1, 10.0] to avoid extreme values from bad data.
    """
    if not symbol or not benchmark or symbol == benchmark:
        return None
    min_bars = period + 1
    fetch_period = "250d" if period > 60 else "90d"
    df_sym = fetch_ohlcv(symbol, period=fetch_period)
    df_bm  = fetch_ohlcv(benchmark, period=fetch_period)
    if df_sym is None or df_bm is None:
        return None
    if len(df_sym) < min_bars or len(df_bm) < min_bars:
        return None
    try:
        close_sym = df_sym["Close"].dropna()
        close_bm  = df_bm["Close"].dropna()
        if len(close_sym) < min_bars or len(close_bm) < min_bars:
            return None
        ret_sym = float(close_sym.iloc[-1]) / float(close_sym.iloc[-period]) - 1
        ret_bm  = float(close_bm.iloc[-1]) / float(close_bm.iloc[-period]) - 1
        if close_sym.iloc[-period] <= 0 or close_bm.iloc[-period] <= 0:
            return None
        rs = (1 + ret_sym) / (1 + ret_bm)
        # Clamp to avoid extreme values from bad/sparse data
        return round(max(0.1, min(10.0, rs)), 4)
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
    # Gap risk metrics: computed from Open vs prev Close across OHLCV history
    overnight_gap_pct_avg: Optional[float] = None  # mean |Open - prev_Close| / prev_Close
    overnight_gap_pct_p95: Optional[float] = None  # 95th percentile gap (tail risk)
    # Volume Profile (daily-bar approximation, 60-day lookback)
    vpoc: Optional[float] = None          # Volume Point of Control: price with highest traded volume
    vah: Optional[float] = None           # Value Area High: upper bound of 70% volume zone
    val: Optional[float] = None           # Value Area Low: lower bound of 70% volume zone
    hvn_levels: list[float] = field(default_factory=list)  # High Volume Nodes (institutional interest)
    lvn_levels: list[float] = field(default_factory=list)  # Low Volume Nodes (price moves fast here)
    vp_bias: str = "at_vpoc"             # "above_vpoc" | "below_vpoc" | "at_vpoc"
    vp_overextension: Optional[float] = None  # (price - vpoc) / (vah - val); >1.5 = overextended

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

    # ── Volume Profile helpers ────────────────────────────────────────────────

    def in_value_area(self) -> bool:
        """Price is inside the 70% value area (choppy equilibrium zone)."""
        return self.vah is not None and self.val is not None and self.val < self.price < self.vah

    def nearest_hvn_below(self) -> Optional[float]:
        candidates = [h for h in self.hvn_levels if h < self.price]
        return max(candidates) if candidates else None

    def nearest_hvn_above(self) -> Optional[float]:
        candidates = [h for h in self.hvn_levels if h > self.price]
        return min(candidates) if candidates else None

    def nearest_lvn_above(self) -> Optional[float]:
        candidates = [l for l in self.lvn_levels if l > self.price]
        return min(candidates) if candidates else None

    def nearest_lvn_below(self) -> Optional[float]:
        candidates = [l for l in self.lvn_levels if l < self.price]
        return max(candidates) if candidates else None


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


def compute_volume_profile(
    df: pd.DataFrame,
    lookback: int = 60,
    n_bins: int = 120,
    value_area_pct: float = 0.70,
    hvn_percentile: float = 0.65,
    lvn_percentile: float = 0.25,
) -> Optional[dict]:
    """
    Build a Volume Profile from daily OHLCV bars (TPO-style approximation).

    Each day's volume is distributed uniformly across its High-Low range into
    price bins. Accumulating across lookback days produces a volume histogram
    from which VPOC, VAH, VAL, HVN and LVN levels are derived.

    Returns dict with keys: vpoc, vah, val, hvn_levels, lvn_levels,
    vp_bias, vp_overextension — or None if data insufficient.
    """
    required = {"High", "Low", "Close", "Volume"}
    if df is None or df.empty or not required.issubset(df.columns):
        return None

    tail = df.tail(lookback).copy()
    if len(tail) < 10:
        return None

    # Drop zero-volume bars and rows with NaN price data (halts, data gaps)
    tail = tail[tail["Volume"] > 0]
    tail = tail.dropna(subset=["High", "Low", "Close", "Volume"])
    if tail.empty:
        return None

    price_min = float(tail["Low"].min())
    price_max = float(tail["High"].max())
    if price_max <= price_min:
        return None

    bin_size = (price_max - price_min) / n_bins
    if bin_size <= 0:
        return None

    # Bin centers
    bin_edges = np.linspace(price_min, price_max, n_bins + 1)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    vol_hist = np.zeros(n_bins)

    for _, row in tail.iterrows():
        lo = float(row["Low"])
        hi = float(row["High"])
        vol = float(row["Volume"])
        if hi <= lo:
            # Single-price bar — put all volume in nearest bin
            idx = int((lo - price_min) / bin_size)
            idx = max(0, min(n_bins - 1, idx))
            vol_hist[idx] += vol
            continue
        # Find bins that overlap [lo, hi]
        i_start = max(0, int((lo - price_min) / bin_size))
        i_end = min(n_bins - 1, int((hi - price_min) / bin_size))
        n_covered = i_end - i_start + 1
        vol_per_bin = vol / n_covered
        vol_hist[i_start:i_end + 1] += vol_per_bin

    if vol_hist.sum() <= 0:
        return None

    # VPOC: bin with highest volume
    vpoc_idx = int(np.argmax(vol_hist))
    vpoc = float(bin_centers[vpoc_idx])

    # Value Area: expand from VPOC until 70% of total volume captured
    total_vol = vol_hist.sum()
    target_vol = total_vol * value_area_pct
    lo_idx = hi_idx = vpoc_idx
    accumulated = vol_hist[vpoc_idx]

    while accumulated < target_vol:
        can_expand_up = hi_idx + 1 < n_bins
        can_expand_dn = lo_idx - 1 >= 0
        if not can_expand_up and not can_expand_dn:
            break
        vol_up = vol_hist[hi_idx + 1] if can_expand_up else -1
        vol_dn = vol_hist[lo_idx - 1] if can_expand_dn else -1
        if vol_up >= vol_dn:
            hi_idx += 1
            accumulated += vol_hist[hi_idx]
        else:
            lo_idx -= 1
            accumulated += vol_hist[lo_idx]

    vah = float(bin_edges[hi_idx + 1])   # upper edge of highest VA bin
    val = float(bin_edges[lo_idx])        # lower edge of lowest VA bin

    # HVN: bins above hvn_percentile of all non-zero volume bins
    nonzero = vol_hist[vol_hist > 0]
    hvn_thresh = float(np.percentile(nonzero, hvn_percentile * 100)) if len(nonzero) > 0 else 0
    lvn_thresh = float(np.percentile(nonzero, lvn_percentile * 100)) if len(nonzero) > 0 else 0

    hvn_raw = [float(bin_centers[i]) for i in range(n_bins) if vol_hist[i] >= hvn_thresh]
    lvn_raw = [float(bin_centers[i]) for i in range(n_bins) if 0 < vol_hist[i] <= lvn_thresh]

    # Cluster HVN/LVN levels: merge within 0.5% of each other
    def _cluster(levels: list[float], tol: float = 0.005) -> list[float]:
        if not levels:
            return []
        levels = sorted(levels)
        clusters: list[list[float]] = [[levels[0]]]
        for lv in levels[1:]:
            if abs(lv - clusters[-1][-1]) / max(clusters[-1][-1], 1e-9) <= tol:
                clusters[-1].append(lv)
            else:
                clusters.append([lv])
        return [float(np.mean(c)) for c in clusters]

    hvn_levels = _cluster(hvn_raw)
    lvn_levels = _cluster(lvn_raw)

    # VP bias and overextension
    current_price = float(df["Close"].iloc[-1])
    va_width = vah - val
    if va_width > 0:
        vp_overextension = (current_price - vpoc) / va_width
    else:
        vp_overextension = 0.0

    if current_price > vpoc * 1.005:
        vp_bias = "above_vpoc"
    elif current_price < vpoc * 0.995:
        vp_bias = "below_vpoc"
    else:
        vp_bias = "at_vpoc"

    return {
        "vpoc": round(vpoc, 4),
        "vah": round(vah, 4),
        "val": round(val, 4),
        "hvn_levels": [round(h, 4) for h in hvn_levels],
        "lvn_levels": [round(l, 4) for l in lvn_levels],
        "vp_bias": vp_bias,
        "vp_overextension": round(vp_overextension, 4),
    }


def build_snapshot(symbol: str) -> Optional[TechnicalSnapshot]:
    """Build full technical snapshot for one symbol."""
    df = fetch_ohlcv(symbol)
    if df is None or df.empty or len(df) < 5:
        return None

    price = float(df["Close"].iloc[-1])
    if not price or not (price > 0):
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

    # Volume Profile (daily-bar approximation, 60-day lookback)
    vp = compute_volume_profile(df)
    vpoc = vp["vpoc"] if vp else None
    vah = vp["vah"] if vp else None
    val = vp["val"] if vp else None
    hvn_levels = vp["hvn_levels"] if vp else []
    lvn_levels = vp["lvn_levels"] if vp else []
    vp_bias = vp["vp_bias"] if vp else "at_vpoc"
    vp_overextension = vp["vp_overextension"] if vp else None

    # Overnight gap distribution: |Open[t] - Close[t-1]| / Close[t-1]
    overnight_gap_pct_avg: Optional[float] = None
    overnight_gap_pct_p95: Optional[float] = None
    if "Open" in df.columns and len(df) >= 5:
        try:
            gaps = (df["Open"] - df["Close"].shift(1)).abs() / df["Close"].shift(1)
            gaps = gaps.dropna()
            if len(gaps) >= 3:
                overnight_gap_pct_avg = float(gaps.mean())
                overnight_gap_pct_p95 = float(gaps.quantile(0.95))
        except Exception:
            pass

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
        overnight_gap_pct_avg=_r(overnight_gap_pct_avg, 6),
        overnight_gap_pct_p95=_r(overnight_gap_pct_p95, 6),
        vpoc=_r(vpoc, 4) if vpoc is not None else None,
        vah=_r(vah, 4) if vah is not None else None,
        val=_r(val, 4) if val is not None else None,
        hvn_levels=[round(h, 4) for h in hvn_levels],
        lvn_levels=[round(l, 4) for l in lvn_levels],
        vp_bias=vp_bias,
        vp_overextension=_r(vp_overextension, 4) if vp_overextension is not None else None,
    )


# ---------------------------------------------------------------------------
# ATR + S/R aware SL/TP
# ---------------------------------------------------------------------------

# (sl_mult, tp_mult) for long trades
ATR_MULTIPLIERS = {
    "conservative": (1.5, 2.0),
    "moderate":     (2.0, 2.5),
    "aggressive":   (3.0, 4.0),
}

# Short SL gets extra room to survive counter-trend rallies (bear-market whipsaw).
# Data: short SL hit rate 71.7% vs 60.9% for longs — widening SL reduces premature stops.
# Short TP is kept tighter than old long TP for the same reason: closer target, more hits.
SHORT_SL_EXTRA_MULT = 1.25   # short SL = long SL × 1.25  (moderate: 2.0 → 2.5× ATR)

FALLBACK_SL_TP = {
    "conservative": (0.015, 0.025),
    "moderate":     (0.02,  0.03),
    "aggressive":   (0.03,  0.05),
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
            sl_base = price + atr * sl_mult * SHORT_SL_EXTRA_MULT
            tp_base = price - atr * tp_mult
        else:
            sl_base = price - atr * sl_mult
            tp_base = price + atr * tp_mult
    else:
        sl_pct, tp_pct = FALLBACK_SL_TP.get(risk_tier, FALLBACK_SL_TP["moderate"])
        if direction == "short":
            sl_base = price * (1 + sl_pct * SHORT_SL_EXTRA_MULT)
            tp_base = price * (1 - tp_pct)
        else:
            sl_base = price * (1 - sl_pct)
            tp_base = price * (1 + tp_pct)

    sl, tp = sl_base, tp_base

    # ── Volume Profile-aware adjustment (priority over swing S/R) ────────────
    if snapshot and snapshot.vpoc is not None:
        min_sl_dist = price * 0.995  # SL never tighter than 0.5% from price

        if direction == "long":
            # SL: prefer nearest HVN below (institutional support) or VAL
            for vp_anchor in [snapshot.nearest_hvn_below(), snapshot.val]:
                if vp_anchor and vp_anchor < price:
                    vp_sl = vp_anchor * 0.998
                    if sl_base < vp_sl < min_sl_dist:
                        sl = vp_sl
                        break

            # TP: avoid placing TP inside an HVN (price stalls/reverses there)
            hvn_above = snapshot.nearest_hvn_above()
            if hvn_above and price < hvn_above < tp_base:
                tp = hvn_above * 0.997  # just below the HVN — realistic exit
            # VAH as TP cap: strong resistance at value area boundary
            if snapshot.vah and price < snapshot.vah < tp_base:
                tp = snapshot.vah * 0.998

        else:  # short
            # SL: only snap to HVN/VAH that is ABOVE sl_base (widens stop to natural resistance).
            # Do NOT snap to closer HVNs — tightening a short stop increases whipsaw risk.
            for vp_anchor in [snapshot.nearest_hvn_above(), snapshot.vah]:
                if vp_anchor and vp_anchor > sl_base:
                    sl = vp_anchor * 1.002
                    break

            # TP: avoid placing TP inside an HVN below
            hvn_below = snapshot.nearest_hvn_below()
            if hvn_below and tp_base < hvn_below < price:
                tp = hvn_below * 1.003  # just above the HVN
            # VAL as TP cap: strong support at value area boundary
            if snapshot.val and tp_base < snapshot.val < price:
                tp = snapshot.val * 1.002

    # ── Swing S/R fallback (when VP levels don't apply) ──────────────────────
    if snapshot:
        if direction == "long":
            support = snapshot.nearest_support_below()
            if support is not None and support < price:
                sr_sl = support * 0.998
                min_sl = price * 0.995
                if sr_sl > sl_base and sr_sl < min_sl:
                    sl = sr_sl

            resistance = snapshot.nearest_resistance_above()
            if resistance is not None and resistance > price:
                if resistance < tp_base:
                    snapped_tp = resistance * 0.998
                    # Only snap if resulting R:R >= 1.0 — otherwise resistance is too close
                    sl_dist = abs(price - sl)
                    tp_dist = abs(snapped_tp - price)
                    if sl_dist > 0 and tp_dist / sl_dist >= 1.0:
                        tp = snapped_tp
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
                    snapped_tp = support * 1.002
                    sl_dist = abs(price - sl)
                    tp_dist = abs(snapped_tp - price)
                    if sl_dist > 0 and tp_dist / sl_dist >= 1.0:
                        tp = snapped_tp

    return round(sl, 2), round(tp, 2)


def estimate_expiry_days(
    price: float,
    take_profit: float,
    snapshot: Optional[TechnicalSnapshot] = None,
    min_days: int = 1,
    max_days: int = 60,
) -> int:
    if not snapshot or not snapshot.avg_daily_move or snapshot.avg_daily_move <= 0:
        return max_days // 2
    distance_pct = abs(take_profit - price) / price if price > 0 else 0
    if distance_pct <= 0:
        return min_days
    estimated = math.ceil(distance_pct / snapshot.avg_daily_move)
    return max(min_days, min(estimated, max_days))


def classify_horizon(expiry_days: int) -> str:
    """Classify trade horizon: near (≤14 trading days) or far (>14 trading days)."""
    return "near" if expiry_days <= 14 else "far"


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
