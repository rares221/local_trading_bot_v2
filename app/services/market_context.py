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

# Dollar index (Yahoo): ICE futures proxy; UUP = ETF fallback if primary fails.
DXY_YAHOO_PRIMARY = "DX-Y.NYB"
DXY_YAHOO_FALLBACK = "UUP"

# ---------------------------------------------------------------------------
# USD / DXY macro (complements SPX+VIX regime; drives FX + metals nudges)
# ---------------------------------------------------------------------------


@dataclass
class DxyMacro:
    """US dollar strength snapshot for macro context and light confidence nudges."""

    available: bool = False
    symbol: str = ""
    price: Optional[float] = None
    trend: str = "neutral"  # bullish = USD strengthening vs recent mean (price > SMA50)
    change_5d_pct: Optional[float] = None
    change_20d_pct: Optional[float] = None
    summary: str = ""


def detect_dxy_macro() -> DxyMacro:
    """Load DXY or UUP; trend from spot vs 50-day mean."""
    for sym in (DXY_YAHOO_PRIMARY, DXY_YAHOO_FALLBACK):
        df = fetch_ohlcv(sym, period="130d")
        if df is None or len(df) < 55 or "Close" not in df.columns:
            continue
        close = df["Close"]
        cur = float(close.iloc[-1])
        sma50 = float(close.rolling(50).mean().iloc[-1])
        if cur > sma50 * 1.002:
            trend = "bullish"
        elif cur < sma50 * 0.998:
            trend = "bearish"
        else:
            trend = "neutral"

        ch5 = ch20 = None
        if len(close) >= 5:
            ch5 = round((cur / float(close.iloc[-5]) - 1.0) * 100.0, 2)
        if len(close) >= 20:
            ch20 = round((cur / float(close.iloc[-20]) - 1.0) * 100.0, 2)

        label = "DXY" if sym == DXY_YAHOO_PRIMARY else "USD (UUP)"
        parts = [f"{label} {trend}", f"{cur:.2f}"]
        if ch5 is not None:
            parts.append(f"5d {ch5:+.1f}%")
        if ch20 is not None:
            parts.append(f"20d {ch20:+.1f}%")
        summary = " | ".join(parts)

        return DxyMacro(
            available=True,
            symbol=sym,
            price=round(cur, 2),
            trend=trend,
            change_5d_pct=ch5,
            change_20d_pct=ch20,
            summary=summary,
        )

    return DxyMacro()


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
# Sector-specific regime detection
# ---------------------------------------------------------------------------

# Sector ETF proxies for regime detection
SECTOR_ETFS = {
    "tech": "XLK",       # Technology Select Sector SPDR
    "energy": "XLE",     # Energy Select Sector SPDR
    "financials": "XLF", # Financial Select Sector SPDR
    "healthcare": "XLV", # Health Care Select Sector SPDR
    "consumer": "XLY",   # Consumer Discretionary Select Sector SPDR
    "industrials": "XLI", # Industrial Select Sector SPDR
    "materials": "XLB",  # Materials Select Sector SPDR
    "utilities": "XLU",  # Utilities Select Sector SPDR
    "commodities": "DBC", # Invesco DB Commodity Index
}


@dataclass
class SectorRegime:
    sector: str
    regime: str  # "risk_on" | "risk_off" | "cautious"
    trend: str   # "bullish" | "bearish" | "neutral"
    above_sma50: bool | None = None
    above_sma200: bool | None = None
    relative_strength: float | None = None  # vs SPX


def detect_sector_regime(sector: str) -> SectorRegime | None:
    """
    Detect regime for a specific sector using its ETF proxy.
    Returns None if sector not recognized or data unavailable.
    """
    etf = SECTOR_ETFS.get(sector.lower())
    if not etf:
        return None
    
    df = fetch_ohlcv(etf, period="250d")
    if df is None or len(df) < 50:
        return None
    
    close = df["Close"]
    current = float(close.iloc[-1])
    
    # SMA 50 and 200
    sma50 = float(close.rolling(50).mean().iloc[-1])
    above_50 = current > sma50
    
    above_200 = None
    if len(close) >= 200:
        sma200 = float(close.rolling(200).mean().iloc[-1])
        above_200 = current > sma200
    
    # Trend determination
    if above_200 is not None:
        if current > sma50 and current > sma200:
            trend = "bullish"
        elif current < sma50 and current < sma200:
            trend = "bearish"
        else:
            trend = "neutral"
    else:
        trend = "bullish" if above_50 else "bearish"
    
    # Relative strength vs SPX (20-day return comparison)
    rel_strength = None
    spx_df = fetch_ohlcv("^GSPC", period="30d")
    if spx_df is not None and len(spx_df) >= 20 and len(df) >= 20:
        sector_ret = (current / float(close.iloc[-20])) - 1
        spx_ret = (float(spx_df["Close"].iloc[-1]) / float(spx_df["Close"].iloc[-20])) - 1
        rel_strength = round(sector_ret - spx_ret, 4)
    
    # Regime: combine trend with relative strength
    if trend == "bullish" and (rel_strength is None or rel_strength > -0.02):
        regime = "risk_on"
    elif trend == "bearish" and (rel_strength is None or rel_strength < 0.02):
        regime = "risk_off"
    else:
        regime = "cautious"
    
    return SectorRegime(
        sector=sector,
        regime=regime,
        trend=trend,
        above_sma50=above_50,
        above_sma200=above_200,
        relative_strength=rel_strength,
    )


def detect_all_sector_regimes() -> dict[str, SectorRegime]:
    """Detect regime for all tracked sectors. Returns dict of sector -> SectorRegime."""
    results = {}
    for sector in SECTOR_ETFS:
        sr = detect_sector_regime(sector)
        if sr:
            results[sector] = sr
    return results


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
    sector_regimes: dict[str, SectorRegime] = field(default_factory=dict)
    has_divergence_warning: bool = False
    dxy: DxyMacro = field(default_factory=DxyMacro)

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

    def regime_adjustment(self) -> int:
        """SPX/VIX bucket only: risk_on +5, cautious 0, risk_off -10."""
        if self.regime.regime == "risk_on":
            return 5
        if self.regime.regime == "risk_off":
            return -10
        return 0

    def divergence_adjustment(self) -> int:
        """Extra haircut when cross-asset pairs are diverging (-5 or 0)."""
        return -5 if self.has_divergence_warning else 0

    def confidence_adjustment(self) -> int:
        """
        Adjustment to confidence score based on market context.
        risk_on: +5, cautious: 0, risk_off: -10.
        Divergence warnings: additional -5.
        """
        return self.regime_adjustment() + self.divergence_adjustment()

    def min_confidence_threshold(self) -> int:
        """
        Minimum confidence to include a recommendation.
        Priority: 1) Adaptive thresholds (if enabled), 2) User-configured thresholds, 3) Defaults.
        """
        from app.services.adaptive_thresholds import get_adaptive_threshold, is_adaptive_enabled
        
        # First try adaptive thresholds (based on empirical hit rates)
        if is_adaptive_enabled():
            adaptive = get_adaptive_threshold(self.regime.regime)
            if adaptive is not None:
                return adaptive
        
        # Then try user-configured thresholds
        from app.core.preferences import get_confidence_thresholds
        thresholds = get_confidence_thresholds()
        return thresholds.get(self.regime.regime, 35)

    def max_recommendations_per_category(self) -> int:
        """
        Max length of the ranked candidate list produced **per category** (before taking the top 5
        for the batch). Must be large enough that correlation/budget skips can still fill slots.
        """
        return 15

    def describe(self) -> str:
        parts = [self.regime.description]
        if self.dxy.available and self.dxy.summary:
            parts.append(self.dxy.summary)
        for cs in self.correlation_signals:
            if cs.diverging:
                parts.append(f"WARNING: {cs.pair_name} divergence ({cs.asset_a} {cs.a_direction}, {cs.asset_b} {cs.b_direction})")
        return " | ".join(parts)

    def dxy_confidence_delta(self, category: str, asset_id: str, direction: str) -> int:
        """
        Small confidence nudge from USD strength vs asset (FX + commodities).
        Does not replace SPX/VIX regime; scales with policy elsewhere.
        """
        if not self.dxy.available:
            return 0
        tr = self.dxy.trend
        if tr == "neutral":
            return 0
        d = (direction or "long").lower()
        aid = (asset_id or "").strip().upper().replace(" ", "")

        # FX majors (category other)
        if category == "other":
            if aid in ("EURUSD", "GBPUSD"):
                if tr == "bullish":
                    return -3 if d == "long" else 2
                return 2 if d == "long" else -3
            if aid == "USDJPY":
                if tr == "bullish":
                    return 2 if d == "long" else -3
                return -3 if d == "long" else 2

        # Precious metals & oil (dollar bid often a headwind for longs)
        if category == "commodities":
            metals = {"XAUUSD", "GOLD", "SILVER", "GC=F", "SI=F"}
            oilish = {"WTI", "BRENT", "CL=F", "BZ=F"}
            if aid in metals:
                if tr == "bullish":
                    return -4 if d == "long" else 2
                return 3 if d == "long" else -2
            if aid in oilish:
                if tr == "bullish":
                    return -2 if d == "long" else 1
                return 2 if d == "long" else -1

        return 0

    def dxy_note_for_asset(self, category: str, asset_id: str, direction: str) -> str:
        """One-line hint when ``dxy_confidence_delta`` is non-zero."""
        if self.dxy_confidence_delta(category, asset_id, direction) == 0:
            return ""
        if not self.dxy.available:
            return ""
        tr = self.dxy.trend
        return f"USD macro ({tr}): confidence nudge vs {direction} {asset_id}"


def build_market_context(include_sectors: bool = True) -> MarketContext:
    """Build full market context: regime + DXY + correlations + sector regimes. Called once per batch."""
    regime = detect_market_regime()
    dxy = detect_dxy_macro()

    corr_signals = check_cross_asset_signals()
    has_div = any(cs.diverging for cs in corr_signals)

    sector_regimes = {}
    if include_sectors:
        sector_regimes = detect_all_sector_regimes()

    return MarketContext(
        regime=regime,
        correlation_signals=corr_signals,
        sector_regimes=sector_regimes,
        has_divergence_warning=has_div,
        dxy=dxy,
    )


def get_sector_for_asset(asset_id: str, category: str) -> str | None:
    """
    Map an asset to its sector for sector-specific regime lookup.
    Returns sector name or None if not mapped.
    """
    # Tech stocks
    tech_stocks = {"AAPL", "MSFT", "GOOGL", "GOOG", "META", "NVDA", "AMD", "INTC", "TSLA", "AMZN", "NFLX", "CRM", "ORCL", "ADBE", "CSCO"}
    # Energy stocks and commodities
    energy_assets = {"XOM", "CVX", "COP", "SLB", "OXY", "CL=F", "BZ=F", "NG=F", "Brent", "WTI", "Crude"}
    # Financials
    financials = {"JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "SCHW", "AXP"}
    # Healthcare
    healthcare = {"JNJ", "UNH", "PFE", "ABBV", "MRK", "LLY", "TMO", "ABT", "BMY"}
    # Consumer
    consumer = {"WMT", "HD", "MCD", "NKE", "SBUX", "TGT", "LOW", "COST"}
    # Industrials
    industrials = {"CAT", "BA", "HON", "UPS", "GE", "MMM", "LMT", "RTX"}
    # Materials / precious metals
    materials = {"GC=F", "SI=F", "Gold", "Silver", "HG=F", "Copper", "NEM", "FCX"}
    
    asset_upper = asset_id.upper()
    
    if asset_upper in tech_stocks or "^IXIC" in asset_upper or "NDX" in asset_upper:
        return "tech"
    if asset_upper in energy_assets or any(x in asset_upper for x in ["OIL", "CRUDE", "BRENT", "WTI"]):
        return "energy"
    if asset_upper in financials:
        return "financials"
    if asset_upper in healthcare:
        return "healthcare"
    if asset_upper in consumer:
        return "consumer"
    if asset_upper in industrials:
        return "industrials"
    if asset_upper in materials or any(x in asset_upper for x in ["GOLD", "SILVER", "COPPER"]):
        return "materials"
    
    # Category-based fallback
    if category == "commodities":
        return "commodities"

    return None


# Direct asset → sector ETF mapping (Yahoo Finance symbols).
# Used for per-stock Relative Strength calculation vs its own sector.
SECTOR_ETF_DIRECT: dict[str, str] = {
    # Tech
    "AAPL": "XLK", "MSFT": "XLK", "GOOGL": "XLK", "GOOG": "XLK",
    "META": "XLK", "NVDA": "XLK", "AMD": "XLK", "INTC": "XLK",
    "TSLA": "XLK", "AMZN": "XLK", "NFLX": "XLK", "CRM": "XLK",
    "ORCL": "XLK", "ADBE": "XLK", "CSCO": "XLK",
    # Energy
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "SLB": "XLE",
    "OXY": "XLE", "BP": "XLE", "SHEL": "XLE",
    # Financials
    "JPM": "XLF", "BAC": "XLF", "WFC": "XLF", "GS": "XLF",
    "MS": "XLF", "C": "XLF", "BLK": "XLF", "SCHW": "XLF", "AXP": "XLF",
    # Healthcare
    "JNJ": "XLV", "UNH": "XLV", "PFE": "XLV", "ABBV": "XLV",
    "MRK": "XLV", "LLY": "XLV", "TMO": "XLV", "ABT": "XLV", "BMY": "XLV",
    # Consumer
    "WMT": "XLY", "HD": "XLY", "MCD": "XLY", "NKE": "XLY",
    "SBUX": "XLY", "TGT": "XLY", "LOW": "XLY", "COST": "XLY",
    # Industrials
    "CAT": "XLI", "BA": "XLI", "HON": "XLI", "UPS": "XLI",
    "GE": "XLI", "MMM": "XLI", "LMT": "XLI", "RTX": "XLI",
    # Materials/Mining stocks
    "NEM": "XLB", "FCX": "XLB",
}


def get_sector_etf_for_asset(asset_id: str) -> Optional[str]:
    """
    Return the sector ETF Yahoo Finance symbol for a given stock ticker.
    First checks the direct map; falls back through get_sector_for_asset → SECTOR_ETFS.
    Returns None for assets with no mapping (commodities, crypto, FX, indices).
    """
    etf = SECTOR_ETF_DIRECT.get(asset_id.upper())
    if etf:
        return etf
    sector = get_sector_for_asset(asset_id, "stocks")
    if sector:
        return SECTOR_ETFS.get(sector)
    return None
