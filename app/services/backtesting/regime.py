"""
Market Regime Classifier for walk-forward backtesting.

Classifies each trading day into one of five regimes using SPX vs 200-day SMA
and VIX level — the standard quant approach for regime-aware performance analysis.

Regimes:
  crisis    — VIX >= 40 (systemic stress: COVID crash, GFC)
  high_vol  — VIX >= 30 (elevated risk-off: bear market conditions)
  bear      — SPX below 200-SMA and VIX >= 20 (trending down, risk-off)
  bull      — SPX above 200-SMA and VIX < 20  (trending up, risk-on)
  neutral   — everything else (sideways / transitional)

Known historical regimes in the data window:
  - Mar–May 2020: crisis   (COVID crash, VIX > 80)
  - Jun–Dec 2020: bull     (V-shaped recovery)
  - 2022:         bear     (Fed tightening, SPX -20%)
  - 2023–2024:    bull     (AI bull run, SPX +50%)
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# VIX thresholds (industry standard)
_VIX_CRISIS = 40.0    # systemic fear (2020 COVID, 2008 GFC)
_VIX_HIGH_VOL = 30.0  # elevated volatility
_VIX_ELEVATED = 20.0  # start of risk-off

# SPX 200-day SMA window
_SPX_SMA_WINDOW = 200


def classify_regime(
    sim_date: date,
    ohlcv_data: dict[str, pd.DataFrame],
) -> str:
    """
    Classify the market regime on a given date using SPX and VIX data.

    Args:
        sim_date: The date to classify (walk-forward safe — only uses data up to this date).
        ohlcv_data: Dict of {symbol: DataFrame} pre-fetched by data_provider.

    Returns:
        One of: 'crisis', 'high_vol', 'bear', 'bull', 'neutral'
    """
    vix_level = _get_vix_level(sim_date, ohlcv_data)
    spx_vs_sma = _get_spx_vs_sma(sim_date, ohlcv_data)

    # Priority: crisis > high_vol > bear > bull > neutral
    if vix_level is not None:
        if vix_level >= _VIX_CRISIS:
            return "crisis"
        if vix_level >= _VIX_HIGH_VOL:
            return "high_vol"

    if spx_vs_sma is not None and vix_level is not None:
        if spx_vs_sma < 0 and vix_level >= _VIX_ELEVATED:
            return "bear"
        if spx_vs_sma > 0 and vix_level < _VIX_ELEVATED:
            return "bull"

    return "neutral"


def _get_vix_level(sim_date: date, ohlcv_data: dict[str, pd.DataFrame]) -> Optional[float]:
    """Return VIX close on or before sim_date."""
    df = ohlcv_data.get("^VIX")
    if df is None or df.empty or "Close" not in df.columns:
        return None
    cutoff = pd.Timestamp(sim_date)
    sliced = df[df.index <= cutoff]
    if sliced.empty:
        return None
    return float(sliced["Close"].iloc[-1])


def _get_spx_vs_sma(sim_date: date, ohlcv_data: dict[str, pd.DataFrame]) -> Optional[float]:
    """
    Return (SPX close - 200-SMA) / 200-SMA as a fraction.
    Positive = above 200-SMA (bullish structure), negative = below (bearish structure).
    """
    df = ohlcv_data.get("^GSPC")
    if df is None or df.empty or "Close" not in df.columns:
        return None
    cutoff = pd.Timestamp(sim_date)
    sliced = df[df.index <= cutoff]
    if len(sliced) < _SPX_SMA_WINDOW:
        return None
    sma200 = float(sliced["Close"].iloc[-_SPX_SMA_WINDOW:].mean())
    if sma200 == 0:
        return None
    current_close = float(sliced["Close"].iloc[-1])
    return (current_close - sma200) / sma200


def build_regime_calendar(
    trading_days: list[date],
    ohlcv_data: dict[str, pd.DataFrame],
) -> dict[date, str]:
    """
    Pre-compute regime for every trading day in bulk (more efficient than per-signal).
    Returns a dict {date: regime_string}.
    """
    calendar: dict[date, str] = {}

    # Pre-build VIX and SPX series sliced for each date using vectorized ops
    vix_df = ohlcv_data.get("^VIX")
    spx_df = ohlcv_data.get("^GSPC")

    vix_series: Optional[pd.Series] = None
    spx_series: Optional[pd.Series] = None
    spx_sma200: Optional[pd.Series] = None

    if vix_df is not None and not vix_df.empty and "Close" in vix_df.columns:
        vix_series = vix_df["Close"]

    if spx_df is not None and not spx_df.empty and "Close" in spx_df.columns:
        spx_series = spx_df["Close"]
        spx_sma200 = spx_series.rolling(window=_SPX_SMA_WINDOW, min_periods=_SPX_SMA_WINDOW).mean()

    for d in trading_days:
        cutoff = pd.Timestamp(d)

        vix_level: Optional[float] = None
        spx_vs_sma: Optional[float] = None

        if vix_series is not None:
            past = vix_series[vix_series.index <= cutoff]
            if not past.empty:
                vix_level = float(past.iloc[-1])

        if spx_series is not None and spx_sma200 is not None:
            sma_past = spx_sma200[spx_sma200.index <= cutoff]
            spx_past = spx_series[spx_series.index <= cutoff]
            if not sma_past.empty and not spx_past.empty and not pd.isna(sma_past.iloc[-1]):
                sma_val = float(sma_past.iloc[-1])
                spx_val = float(spx_past.iloc[-1])
                if sma_val != 0:
                    spx_vs_sma = (spx_val - sma_val) / sma_val

        # Classify
        if vix_level is not None and vix_level >= _VIX_CRISIS:
            calendar[d] = "crisis"
        elif vix_level is not None and vix_level >= _VIX_HIGH_VOL:
            calendar[d] = "high_vol"
        elif spx_vs_sma is not None and vix_level is not None and spx_vs_sma < 0 and vix_level >= _VIX_ELEVATED:
            calendar[d] = "bear"
        elif spx_vs_sma is not None and vix_level is not None and spx_vs_sma > 0 and vix_level < _VIX_ELEVATED:
            calendar[d] = "bull"
        else:
            calendar[d] = "neutral"

    return calendar
