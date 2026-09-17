"""
Canonical market-regime taxonomy — single source of truth.

Before this module, three taxonomies disagreed:
  - live 3-label    MarketRegime.regime          {risk_on, risk_off, cautious}
  - live 5-label    MarketRegime.extended_regime {crisis, high_vol, bull, bear, neutral}
  - backtest        regime.day_regime            {crisis, high_vol, bull, bear, neutral}

The live and backtest `bear` rules were structurally INVERTED on VIX (live bear required
VIX < 25; backtest bear requires VIX >= 20), so the same day was short-blocked in one path
and short-eligible in the other. This module defines ONE classification rule (the standard
quant approach from backtesting/regime.py) and ONE set of scoring profiles, and everything
else delegates here.

Canonical labels (priority order, first match wins):
  crisis    VIX >= 40
  high_vol  VIX >= 30
  bear      SPX below 200-SMA  AND  VIX >= 20
  bull      SPX above 200-SMA  AND  VIX <  20
  neutral   everything else (incl. any missing input)

Scoring profiles — 5 labels map to 4 weight buckets. `crisis`/`high_vol` are hard-blocked
before scoring elsewhere, so they reuse the most-defensive `bear` bucket as a safety net.
  bull              -> risk_on  (inherited verbatim from the old risk_on profile)
  neutral           -> neutral  (NEW: balanced, slight mean-reversion lean)
  bear              -> bear     (NEW: stricter than the old risk_off — longs need tech AND news)
  crisis / high_vol -> bear

The exact decimals are calibration starting points; the structure (bear stricter than
neutral stricter than risk_on for longs) is the contract, and the walk-forward tunes the
numbers.
"""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Classification thresholds (industry standard — mirror backtesting/regime.py)
# ---------------------------------------------------------------------------
VIX_CRISIS = 40.0    # systemic fear (2020 COVID, 2008 GFC)
VIX_HIGH_VOL = 30.0  # elevated volatility
VIX_ELEVATED = 20.0  # start of risk-off

CANONICAL_LABELS = ("crisis", "high_vol", "bear", "bull", "neutral")


def classify_canonical_regime(
    vix: Optional[float],
    spx_vs_sma200: Optional[float],
) -> str:
    """
    Map raw VIX level and SPX-vs-200SMA fraction to a canonical regime label.

    Args:
        vix:           latest VIX close, or None if unavailable.
        spx_vs_sma200: (SPX close - 200SMA) / 200SMA as a signed fraction, or None
                       if unavailable (e.g. < 200 bars of history).

    Returns:
        One of CANONICAL_LABELS. Any missing input collapses to 'neutral' (the safe,
        balanced bucket) — matching the fall-through behavior of backtesting/regime.py.
    """
    if vix is not None:
        if vix >= VIX_CRISIS:
            return "crisis"
        if vix >= VIX_HIGH_VOL:
            return "high_vol"

    if spx_vs_sma200 is not None and vix is not None:
        if spx_vs_sma200 < 0 and vix >= VIX_ELEVATED:
            return "bear"
        if spx_vs_sma200 > 0 and vix < VIX_ELEVATED:
            return "bull"

    return "neutral"


# ---------------------------------------------------------------------------
# Scoring profiles — keyed on canonical label, 5 labels -> 4 buckets
# ---------------------------------------------------------------------------
# Each bucket is a dict of the four tuples consumed by technicals._regime_* helpers.
#   direction: (sentiment_w, technical_w)
#   tech:      (trend_mult, mean_reversion_mult)
#   rsi:       (bull_bar, bear_bar, overbought, oversold)
#   stoch:     (bull_bar, bear_bar, overbought, oversold)

_PROFILE_RISK_ON = {
    "direction": (0.20, 0.80),
    "tech": (1.2, 0.85),
    "rsi": (52.0, 48.0, 72.0, 28.0),
    "stoch": (52.0, 48.0, 85.0, 15.0),
}

_PROFILE_NEUTRAL = {
    "direction": (0.30, 0.70),
    "tech": (0.95, 1.10),
    "rsi": (55.0, 45.0, 70.0, 30.0),
    "stoch": (55.0, 45.0, 80.0, 20.0),
}

_PROFILE_BEAR = {
    "direction": (0.32, 0.68),
    "tech": (0.78, 1.28),
    "rsi": (63.0, 37.0, 60.0, 40.0),
    "stoch": (63.0, 37.0, 72.0, 28.0),
}

# 5 canonical labels -> profile bucket. crisis/high_vol reuse bear (most defensive).
_LABEL_TO_PROFILE = {
    "bull": _PROFILE_RISK_ON,
    "neutral": _PROFILE_NEUTRAL,
    "bear": _PROFILE_BEAR,
    "crisis": _PROFILE_BEAR,
    "high_vol": _PROFILE_BEAR,
}


def _profile(label: Optional[str]) -> dict:
    """Resolve a label to its profile bucket; unknown/None -> neutral (balanced default)."""
    return _LABEL_TO_PROFILE.get(label or "", _PROFILE_NEUTRAL)


def direction_weights(label: Optional[str] = None) -> tuple[float, float]:
    """(sentiment_w, technical_w) for direction scoring."""
    return _profile(label)["direction"]


def tech_weights(label: Optional[str] = None) -> tuple[float, float]:
    """(trend_mult, mean_reversion_mult) technical-component multipliers."""
    return _profile(label)["tech"]


def rsi_thresholds(label: Optional[str] = None) -> tuple[float, float, float, float]:
    """(bull_bar, bear_bar, overbought, oversold) RSI thresholds."""
    return _profile(label)["rsi"]


def stoch_thresholds(label: Optional[str] = None) -> tuple[float, float, float, float]:
    """(bull_bar, bear_bar, overbought, oversold) Stochastic thresholds."""
    return _profile(label)["stoch"]
