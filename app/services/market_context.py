"""
Market context module: regime detection, cross-asset correlation awareness,
multi-timeframe trend alignment, EMA trend filter.
Provides a MarketContext that gates and adjusts recommendations.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from app.services.technicals import fetch_ohlcv, get_cached_price

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Market regime detection (SPX + VIX based)
# ---------------------------------------------------------------------------

@dataclass
class MarketRegime:
    regime: str  # "risk_on" | "risk_off" | "cautious"
    spx_trend: str  # "bullish" | "bearish" | "neutral"
    vix_level: Optional[float] = None
    vix_regime: str = "normal"  # "low" | "normal" | "elevated" | "fear"
    spx_above_sma200: Optional[bool] = None
    spx_above_sma50: Optional[bool] = None
    description: str = ""


def detect_market_regime() -> MarketRegime:
    """
    Detect current market regime from S&P 500 trend + VIX level.
    - risk_on: SPX above SMA200, VIX < 20 — favor longs, full confidence
    - cautious: SPX near SMA200 or VIX 20–30 — reduce position count, tighten stops
    - risk_off: SPX below SMA200 and VIX > 25 — favor shorts/cash, skip aggressive longs
    """
    spx_df = fetch_ohlcv("^GSPC", period="250d")
    vix_price = get_cached_price("^VIX")

    # SPX trend from SMA 50 and SMA 200
    spx_above_200 = None
    spx_above_50 = None
    spx_trend = "neutral"
    if spx_df is not None and len(spx_df) >= 200:
        close = spx_df["Close"]
        sma200 = close.rolling(200).mean().iloc[-1]
        sma50 = close.rolling(50).mean().iloc[-1]
        current = close.iloc[-1]
        spx_above_200 = bool(current > sma200)
        spx_above_50 = bool(current > sma50)
        if current > sma200 and current > sma50:
            spx_trend = "bullish"
        elif current < sma200 and current < sma50:
            spx_trend = "bearish"
        else:
            spx_trend = "neutral"
    elif spx_df is not None and len(spx_df) >= 50:
        close = spx_df["Close"]
        sma50 = close.rolling(50).mean().iloc[-1]
        current = close.iloc[-1]
        spx_above_50 = bool(current > sma50)
        spx_trend = "bullish" if spx_above_50 else "bearish"

    # VIX regime
    vix_regime = "normal"
    if vix_price is not None:
        if vix_price < 15:
            vix_regime = "low"
        elif vix_price < 20:
            vix_regime = "normal"
        elif vix_price < 30:
            vix_regime = "elevated"
        else:
            vix_regime = "fear"

    # Combined regime
    if spx_trend == "bullish" and vix_regime in ("low", "normal"):
        regime = "risk_on"
        desc = f"Risk-on: SPX trending up, VIX {vix_price:.1f} ({vix_regime}). Favor longs."
    elif spx_trend == "bearish" and vix_regime in ("elevated", "fear"):
        regime = "risk_off"
        desc = f"Risk-off: SPX trending down, VIX {vix_price:.1f} ({vix_regime}). Favor shorts/hedges."
    else:
        regime = "cautious"
        vix_str = f"{vix_price:.1f}" if vix_price else "?"
        desc = f"Cautious: SPX {spx_trend}, VIX {vix_str} ({vix_regime}). Mixed signals."

    return MarketRegime(
        regime=regime,
        spx_trend=spx_trend,
        vix_level=round(vix_price, 2) if vix_price else None,
        vix_regime=vix_regime,
        spx_above_sma200=spx_above_200,
        spx_above_sma50=spx_above_50,
        description=desc,
    )


# ---------------------------------------------------------------------------
# Multi-timeframe trend alignment
# ---------------------------------------------------------------------------

def calc_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


@dataclass
class TrendAlignment:
    daily_trend: str = "neutral"   # "bullish" | "bearish" | "neutral"
    weekly_trend: str = "neutral"
    h4_trend: str = "neutral"      # 4H trend when hourly data available
    ema_stack: str = "none"        # "bullish_stack" | "bearish_stack" | "mixed"
    aligned: bool = False          # True if daily and weekly agree (and 4H when present)
    alignment_score: float = 0.0   # -1 to +1 (3 timeframes when 4H available)


def _check_4h_trend(symbol: str) -> str:
    """4H trend from hourly data: EMA 20 on 4H bars. Returns bullish | bearish | neutral."""
    df = fetch_ohlcv(symbol, period="30d", interval="1h")
    if df is None or len(df) < 20:
        return "neutral"
    if "Close" not in df.columns:
        return "neutral"
    # Resample to 4H
    df = df.copy()
    df.index = pd.DatetimeIndex(df.index)
    ohlc = df.resample("4h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    if len(ohlc) < 20:
        return "neutral"
    close = ohlc["Close"]
    ema20 = calc_ema(close, 20)
    current = float(close.iloc[-1])
    e20 = float(ema20.iloc[-1])
    if current > e20 * 1.002:
        return "bullish"
    if current < e20 * 0.998:
        return "bearish"
    return "neutral"


def check_trend_alignment(symbol: str) -> TrendAlignment:
    """
    Multi-timeframe trend check: 4H (when available), daily EMA 20/50, weekly SMA 200.
    Alignment score combines all three timeframes.
    """
    df = fetch_ohlcv(symbol, period="250d")
    if df is None or len(df) < 50:
        return TrendAlignment()

    h4_trend = _check_4h_trend(symbol)

    close = df["Close"]
    current = float(close.iloc[-1])

    ema20 = calc_ema(close, 20)
    ema50 = calc_ema(close, 50)

    e20 = float(ema20.iloc[-1])
    e50 = float(ema50.iloc[-1])

    # Daily trend from EMA 20 vs EMA 50
    if current > e20 > e50:
        daily_trend = "bullish"
    elif current < e20 < e50:
        daily_trend = "bearish"
    else:
        daily_trend = "neutral"

    # Weekly trend approximation from SMA 200 slope
    weekly_trend = "neutral"
    if len(close) >= 200:
        sma200 = close.rolling(200).mean()
        s200_now = float(sma200.iloc[-1])
        s200_20ago = float(sma200.iloc[-20]) if len(sma200) >= 20 else s200_now
        if s200_now > s200_20ago * 1.005:
            weekly_trend = "bullish"
        elif s200_now < s200_20ago * 0.995:
            weekly_trend = "bearish"

    # EMA stack: 20 > 50 > 200 (or all below)
    ema_stack = "mixed"
    if len(close) >= 200:
        sma200_val = float(close.rolling(200).mean().iloc[-1])
        if e20 > e50 > sma200_val:
            ema_stack = "bullish_stack"
        elif e20 < e50 < sma200_val:
            ema_stack = "bearish_stack"

    # Alignment: daily and weekly agree; when 4H is not neutral, all three must agree
    aligned = daily_trend == weekly_trend and daily_trend != "neutral"
    if h4_trend != "neutral":
        aligned = aligned and (daily_trend == h4_trend)

    # Alignment score: 4H + daily + weekly (3 timeframes when 4H present)
    score = 0.0
    if h4_trend == "bullish":
        score += 0.25
    elif h4_trend == "bearish":
        score -= 0.25
    if daily_trend == "bullish":
        score += 0.35
    elif daily_trend == "bearish":
        score -= 0.35
    if weekly_trend == "bullish":
        score += 0.25
    elif weekly_trend == "bearish":
        score -= 0.25
    if ema_stack == "bullish_stack":
        score += 0.15
    elif ema_stack == "bearish_stack":
        score -= 0.15
    # Clamp to [-1, 1]
    score = max(-1.0, min(1.0, score))

    return TrendAlignment(
        daily_trend=daily_trend,
        weekly_trend=weekly_trend,
        h4_trend=h4_trend,
        ema_stack=ema_stack,
        aligned=aligned,
        alignment_score=round(score, 2),
    )


# ---------------------------------------------------------------------------
# Cross-asset correlation awareness
# ---------------------------------------------------------------------------

# Known relationships: when these move together/apart, it signals something
CORRELATION_PAIRS = {
    "gold_usd_inverse": ("GC=F", "DX-Y.NYB"),       # Gold vs USD index — inverse
    "oil_energy_stocks": ("CL=F", "XOM"),              # Oil vs energy stocks — positive
    "spx_vix_inverse": ("^GSPC", "^VIX"),              # SPX vs VIX — inverse
    "gold_spx_risk": ("GC=F", "^GSPC"),                # Gold vs SPX — stress indicator
}


@dataclass
class CorrelationSignal:
    pair_name: str
    asset_a: str
    asset_b: str
    a_direction: str  # "up" | "down" | "flat"
    b_direction: str
    expected_relationship: str  # "inverse" | "positive"
    diverging: bool  # True if relationship is breaking down
    signal: str  # "normal" | "divergence_warning" | "confirming"


def check_cross_asset_signals() -> list[CorrelationSignal]:
    """
    Check key cross-asset relationships for divergences or confirmation.
    Returns list of signals about current market dynamics.
    """
    signals: list[CorrelationSignal] = []

    pairs = [
        ("gold_usd_inverse", "GC=F", "DX-Y.NYB", "inverse"),
        ("spx_vix_inverse", "^GSPC", "^VIX", "inverse"),
        ("gold_spx_risk", "GC=F", "^GSPC", "inverse"),
    ]

    for name, sym_a, sym_b, expected in pairs:
        df_a = fetch_ohlcv(sym_a, period="20d")
        df_b = fetch_ohlcv(sym_b, period="20d")
        if df_a is None or df_b is None or len(df_a) < 5 or len(df_b) < 5:
            continue

        # 5-day return direction
        ret_a = (float(df_a["Close"].iloc[-1]) / float(df_a["Close"].iloc[-5])) - 1
        ret_b = (float(df_b["Close"].iloc[-1]) / float(df_b["Close"].iloc[-5])) - 1

        dir_a = "up" if ret_a > 0.002 else ("down" if ret_a < -0.002 else "flat")
        dir_b = "up" if ret_b > 0.002 else ("down" if ret_b < -0.002 else "flat")

        if expected == "inverse":
            # Normal: one up, other down. Divergence: both same direction
            if dir_a == dir_b and dir_a != "flat":
                diverging = True
                signal = "divergence_warning"
            else:
                diverging = False
                signal = "confirming" if dir_a != dir_b else "normal"
        else:  # positive
            if dir_a != dir_b and dir_a != "flat" and dir_b != "flat":
                diverging = True
                signal = "divergence_warning"
            else:
                diverging = False
                signal = "confirming" if dir_a == dir_b else "normal"

        signals.append(CorrelationSignal(
            pair_name=name,
            asset_a=sym_a,
            asset_b=sym_b,
            a_direction=dir_a,
            b_direction=dir_b,
            expected_relationship=expected,
            diverging=diverging,
            signal=signal,
        ))

    return signals


# ---------------------------------------------------------------------------
# Full market context (used by engine at batch time)
# ---------------------------------------------------------------------------

@dataclass
class MarketContext:
    regime: MarketRegime
    correlation_signals: list[CorrelationSignal] = field(default_factory=list)
    has_divergence_warning: bool = False

    @property
    def should_reduce_exposure(self) -> bool:
        """True when regime is risk_off or there are correlation divergences."""
        return self.regime.regime == "risk_off" or self.has_divergence_warning

    @property
    def regime_direction_bias(self) -> str:
        """Suggest overall direction bias from regime."""
        if self.regime.regime == "risk_on":
            return "long"
        elif self.regime.regime == "risk_off":
            return "short"
        return "neutral"

    def confidence_adjustment(self) -> int:
        """
        Adjustment to confidence score based on market context.
        risk_on: +5, cautious: 0, risk_off: -10.
        Divergence warnings: additional -5.
        """
        adj = 0
        if self.regime.regime == "risk_on":
            adj += 5
        elif self.regime.regime == "risk_off":
            adj -= 10
        if self.has_divergence_warning:
            adj -= 5
        return adj

    def min_confidence_threshold(self) -> int:
        """
        Minimum confidence to include a recommendation.
        Uses adaptive thresholds if available (based on empirical hit rates),
        otherwise falls back to static defaults:
        risk_on: 30 (more permissive), cautious: 35, risk_off: 40 (stricter).
        """
        from app.services.adaptive_thresholds import get_adaptive_threshold, is_adaptive_enabled
        
        if is_adaptive_enabled():
            adaptive = get_adaptive_threshold(self.regime.regime)
            if adaptive is not None:
                return adaptive
        
        # Fallback to static thresholds
        if self.regime.regime == "risk_on":
            return 30
        elif self.regime.regime == "risk_off":
            return 40
        return 35

    def max_recommendations_per_category(self) -> int:
        """risk_off: cap at 5 per category instead of 10."""
        if self.regime.regime == "risk_off":
            return 5
        return 10

    def describe(self) -> str:
        parts = [self.regime.description]
        for cs in self.correlation_signals:
            if cs.diverging:
                parts.append(f"WARNING: {cs.pair_name} divergence ({cs.asset_a} {cs.a_direction}, {cs.asset_b} {cs.b_direction})")
        return " | ".join(parts)


def build_market_context() -> MarketContext:
    """Build full market context: regime + correlations. Called once per batch."""
    regime = detect_market_regime()
    corr_signals = check_cross_asset_signals()
    has_div = any(cs.diverging for cs in corr_signals)

    return MarketContext(
        regime=regime,
        correlation_signals=corr_signals,
        has_divergence_warning=has_div,
    )
