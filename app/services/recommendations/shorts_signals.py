"""
Shorts engine — breakdown family signal generator (Phase 5a).

Isolated from the long path. Detects three "confirmed weakness" breakdown setups and scores
them with the shared, validated scorer (score_candidate with direction="short"). See
docs/superpowers/specs/2026-09-17-shorts-signal-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd

from app.services.technicals import (
    TechnicalSnapshot,
    compute_atr_levels,
    calc_risk_reward,
)
from app.services.recommendations.scoring import score_candidate

# Per-trigger base confidence offset (walk-forward-tunable), ordered by precision.
TRIGGER_BASE_OFFSET: dict[str, int] = {
    "failed_retest": 8,
    "failed_breakout": 5,
    "support_breakdown": 3,
    "distribution": 8,
    "exhaustion": 5,
}

_BREAKDOWN_TRIGGERS = ("support_breakdown", "failed_breakout", "failed_retest")
_FADE_TOP_TRIGGERS = ("distribution", "exhaustion")

_NEAR_HIGH_FRAC = 0.98  # within 2% of the 20-bar high counts as "near the high" (fade-top)

# Shorts require higher conviction than longs (historically lower WR).
SHORT_CONFIDENCE_FLOOR: int = 60

# Regimes where NO position (long or short) may open — hard block, mirrors the long engine.
_BLOCKED_REGIMES = ("crisis", "high_vol")

_LOOKBACK_BREAKOUT = 10   # bars to look back for a failed breakout
_LOOKBACK_RETEST = 15     # bars to look back for a broken-support retest
_SWING_WINDOW = 20        # fallback swing high/low window
_BREAK_MARGIN = 0.001     # 0.1% — close must clear the level by this to count as a break

# Minimum reward:risk for a short to be emitted (mirrors the long floor; tune in backtest).
_SHORT_MIN_RR = 2.0


@dataclass
class ShortSignal:
    symbol: str
    trigger: str
    direction: str  # always "short"
    confidence: int
    entry: float
    stop_loss: float
    take_profit: float
    risk_reward: Optional[float]


# ── Level helpers ─────────────────────────────────────────────────────────────

def _resolve_support(snap: TechnicalSnapshot, bars: pd.DataFrame, prev_close: float) -> Optional[float]:
    """Nearest swing support below prev_close; fall back to the 20-bar low (excluding last bar)."""
    below = [s for s in (snap.supports or []) if s < prev_close]
    if below:
        return max(below)  # nearest below
    if bars is not None and len(bars) > _SWING_WINDOW:
        return float(bars["Low"].iloc[-(_SWING_WINDOW + 1):-1].min())
    return None


def _resolve_resistance(snap: TechnicalSnapshot, bars: pd.DataFrame, prev_close: float) -> Optional[float]:
    """Nearest swing resistance above prev_close; fall back to the pre-window 20-bar high."""
    above = [r for r in (snap.resistances or []) if r > prev_close]
    if above:
        return min(above)  # nearest above
    if bars is not None and len(bars) > (_SWING_WINDOW + _LOOKBACK_BREAKOUT):
        pre = bars["High"].iloc[-(_SWING_WINDOW + _LOOKBACK_BREAKOUT):-_LOOKBACK_BREAKOUT]
        return float(pre.max())
    return None


# ── Trigger detectors ─────────────────────────────────────────────────────────

def support_breakdown(snap: TechnicalSnapshot, bars: pd.DataFrame) -> bool:
    """Latest close < established support (by margin) AND volume_ratio > 1.0."""
    if bars is None or len(bars) < 2:
        return False
    if snap.volume_ratio is None or snap.volume_ratio <= 1.0:
        return False
    prev_close = float(bars["Close"].iloc[-2])
    support = _resolve_support(snap, bars, prev_close)
    if support is None:
        return False
    close = float(bars["Close"].iloc[-1])
    return close < support * (1 - _BREAK_MARGIN)


def failed_breakout(snap: TechnicalSnapshot, bars: pd.DataFrame) -> bool:
    """Within the last ~10 bars a high pierced resistance, but the latest close is back below it."""
    if bars is None or len(bars) < 2:
        return False
    prev_close = float(bars["Close"].iloc[-2])
    resistance = _resolve_resistance(snap, bars, prev_close)
    if resistance is None:
        return False
    recent_high = float(bars["High"].iloc[-_LOOKBACK_BREAKOUT:].max())
    close = float(bars["Close"].iloc[-1])
    return recent_high > resistance and close < resistance


def failed_retest(snap: TechnicalSnapshot, bars: pd.DataFrame) -> bool:
    """
    Support broken within ~15 bars, price rallied back to it (now resistance), latest close
    rejected below. Precise: some close in the window < support; a subsequent high came back
    within 1% of support; latest close < support.
    """
    if bars is None or len(bars) < 3:
        return False
    window = bars.iloc[-_LOOKBACK_RETEST:].reset_index(drop=True)
    # The broken level sits below the retest rally high (not below the latest close, which is
    # already back under it) — resolve support against the window high.
    window_high = float(window["High"].max())
    support = _resolve_support(snap, bars, window_high)
    if support is None:
        return False
    below = window["Close"] < support * (1 - _BREAK_MARGIN)
    if not below.any():
        return False
    # The retest rally must come AFTER the first break — otherwise pre-break bars that were simply
    # above support would count as a "retest" and misfire on a plain breakdown.
    first_break = int(below.idxmax())
    after = window.iloc[first_break + 1:]
    retested = bool((after["High"] >= support * (1 - 0.01)).any()) if len(after) else False
    rejected = float(window["Close"].iloc[-1]) < support * (1 - _BREAK_MARGIN)
    return retested and rejected


# ── Fade-top family (Phase 5b) — short exhausted strength at highs ─────────────

def _tape_deteriorating(vix: pd.Series, spx: pd.Series, vix_lookback: int = 5, spx_ma: int = 20) -> bool:
    """
    Option-1 fade-top activation: risk turning up while price is still high —
    VIX rising over vix_lookback days AND SPX below its spx_ma moving average.
    Market-level; the caller computes it once per day and passes the bool in.
    """
    if vix is None or spx is None or len(vix) <= vix_lookback or len(spx) < spx_ma:
        return False
    vix_rising = float(vix.iloc[-1]) > float(vix.iloc[-1 - vix_lookback])
    spx_below = float(spx.iloc[-1]) < float(spx.iloc[-spx_ma:].mean())
    return vix_rising and spx_below


def exhaustion(snap: TechnicalSnapshot, bars: pd.DataFrame) -> bool:
    """
    RSI overbought (>70) is the required anchor, plus at least ONE rollover confirmation:
    MACD histogram rolling over OR volume fading over the last ~3 bars. (Requiring both
    simultaneously almost never co-occurs, so this uses the standard 2-of-anchor+confirm form.)
    """
    if snap.rsi is None or snap.rsi <= 70:
        return False
    macd_rolling = snap.macd_hist_slope is not None and snap.macd_hist_slope < 0
    volume_fading = False
    if bars is not None and len(bars) >= 4 and "Volume" in bars.columns:
        v = bars["Volume"].iloc[-3:]
        volume_fading = bool(v.iloc[-1] < v.iloc[0])
    return macd_rolling or volume_fading


def distribution(snap: TechnicalSnapshot, bars: pd.DataFrame) -> bool:
    """Price holding near its 20-bar high while OBV slope has rolled negative (lower highs)."""
    if snap.obv is None or snap.obv[1] >= 0:
        return False
    if bars is None or len(bars) < _SWING_WINDOW:
        return False
    high_20 = float(bars["High"].iloc[-_SWING_WINDOW:].max())
    close = float(bars["Close"].iloc[-1])
    return close >= _NEAR_HIGH_FRAC * high_20


def top_quality_score(snap: TechnicalSnapshot) -> int:
    """
    Bespoke fade-top quality (0-100): rewards the *extremity of the top*, not bearish momentum
    (the shared trend scorer penalizes high-RSI shorts). OBV/MACD kept binary (slope sign) —
    their magnitudes aren't normalized across symbols.
    """
    pts = 0.0
    if snap.rsi is not None:
        pts += min(30.0, max(0.0, snap.rsi - 70.0) * 2.0)   # RSI 85 -> 30
    if snap.macd_hist_slope is not None and snap.macd_hist_slope < 0:
        pts += 20.0                                          # momentum rolling over
    if snap.obv is not None and snap.obv[1] < 0:
        pts += 20.0                                          # distribution
    if snap.volume_ratio is not None:
        pts += 20.0 if snap.volume_ratio < 0.8 else (10.0 if snap.volume_ratio < 1.0 else 0.0)
    if snap.stochastic is not None and snap.stochastic[0] > 80:
        pts += 10.0                                          # stochastic confirm
    return int(min(100.0, pts))


# ── Aggregator + pre-gates ────────────────────────────────────────────────────

def detect_short_triggers(
    snap: TechnicalSnapshot,
    bars: pd.DataFrame,
    regime: Optional[str],
    tape_deteriorating: bool = False,
) -> list[str]:
    """
    Family-aware trigger detection with per-family activation + structural gate:
      - blocked entirely in crisis / high_vol.
      - Breakdown family: active in `bear` regime, price BELOW EMA200 (confirmed weakness).
      - Fade-top family: active when the tape is deteriorating (Option 1), price ABOVE EMA200
        (you only fade genuine strength at a high).
    """
    if regime in _BLOCKED_REGIMES:
        return []
    matched: list[str] = []
    below_ema = snap.ema200 is None or snap.price < snap.ema200
    above_ema = snap.ema200 is not None and snap.price > snap.ema200

    if regime == "bear" and below_ema:
        if support_breakdown(snap, bars):
            matched.append("support_breakdown")
        if failed_breakout(snap, bars):
            matched.append("failed_breakout")
        if failed_retest(snap, bars):
            matched.append("failed_retest")

    if tape_deteriorating and above_ema:
        if distribution(snap, bars):
            matched.append("distribution")
        if exhaustion(snap, bars):
            matched.append("exhaustion")

    return matched


# ── Top-level signal generation ───────────────────────────────────────────────

def generate_short_signal(
    symbol: str,
    snap: TechnicalSnapshot,
    bars: pd.DataFrame,
    *,
    regime_id: Optional[str],
    category: str,
    risk_tier: str,
    preferred_risk: str,
    sentiment_avg: Optional[float],
    sentiment_count: int,
    today: date,
    tape_deteriorating: bool = False,
) -> Optional[ShortSignal]:
    """Full short pipeline: detect -> score (family-aware) -> level -> gate. None if unqualified."""
    triggers = detect_short_triggers(snap, bars, regime_id, tape_deteriorating)
    if not triggers:
        return None
    # Highest-precision trigger wins when several match.
    trigger = max(triggers, key=lambda t: TRIGGER_BASE_OFFSET[t])

    # Family-aware base: fade-tops use the bespoke top-quality scorer (the shared trend scorer
    # penalizes high-RSI shorts); breakdowns use the shared score_candidate(direction="short").
    if trigger in _FADE_TOP_TRIGGERS:
        base = top_quality_score(snap)
    else:
        base = score_candidate(
            snap, "short", regime_id, category, preferred_risk,
            sentiment_avg, sentiment_count, today=today,
        ).confidence
    confidence = min(100, base + TRIGGER_BASE_OFFSET[trigger])
    if confidence < SHORT_CONFIDENCE_FLOOR:
        return None

    sl, tp = compute_atr_levels(snap.price, risk_tier, snapshot=snap, direction="short")
    # Base ATR R:R for a short is ~1.4 (and short SL snapping only widens the stop), so stretch the
    # TP down to meet the floor — mirrors how the long engine stretches levels. Full short R:R
    # redesign (tighter stops, squeeze-exit) is deferred to Phase 6 per the spec.
    risk = sl - snap.price  # short: stop is above entry
    if risk > 0:
        stretched_tp = snap.price - _SHORT_MIN_RR * risk
        tp = min(tp, stretched_tp)  # only push TP further down, never up
    rr = calc_risk_reward(snap.price, sl, tp)
    if rr is None or rr < _SHORT_MIN_RR:
        return None

    return ShortSignal(
        symbol=symbol, trigger=trigger, direction="short", confidence=confidence,
        entry=round(snap.price, 4), stop_loss=round(sl, 4), take_profit=round(tp, 4),
        risk_reward=round(rr, 4),
    )
