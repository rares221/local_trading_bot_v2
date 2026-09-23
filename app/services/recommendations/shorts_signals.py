"""
Shorts engine — breakdown + fade-top signal generator.

Isolated from the long path. Two trigger families (breakdown: confirmed-weakness setups
below EMA200 in a bear regime; fade-top: exhausted-strength setups above EMA200 when the
tape is deteriorating), each scored and leveled, gated by confidence + relative-strength +
horizon-matched R:R. See docs/superpowers/specs/2026-09-17-shorts-signal-design.md and the
Phase 1 redesign notes on TRIGGER_BASE_OFFSET / BREAKDOWN_RS_MAX / FADE_TOP_RS_MIN /
_HORIZON_STRETCH_DAYS below — the original design (flag-disabled) failed an honest
walk-forward; Phase 1 replaces the blind trigger-precision ranking and TP-stretch-to-fixed-
R:R with data-evidenced gates before shorts are considered for re-enabling.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, Optional

import pandas as pd

from app.services.technicals import (
    TechnicalSnapshot,
    compute_atr_levels,
    calc_risk_reward,
)
from app.services.recommendations.scoring import score_candidate

# Phase 1 redesign (2026-09-18): the original per-trigger "precision ordering" was an
# unevidenced guess — a Tier-1 walk-forward diagnosis found the highest-ranked trigger
# (failed_retest, +8) was empirically the WORST performer (0% OOS win rate, n=67), while a
# lower-ranked one (distribution) was the least-bad. Rather than re-rank based on that same
# spent dataset (re-fitting to noise), all triggers within a family are flattened to an
# equal offset; the RS-gate, volume veto, and horizon-matched sizing below do the real
# discriminating work instead of a hand-guessed ranking.
TRIGGER_BASE_OFFSET: dict[str, int] = {
    "failed_retest": 5,
    "failed_breakout": 5,
    "support_breakdown": 5,
    "distribution": 5,
    "exhaustion": 5,
}

# Volume confirmation floor shared by all three breakdown triggers (Phase 1 redesign):
# a breakdown/failed-breakout/failed-retest without above-average volume on the confirming
# bar is the textbook "bull trap" profile research flags as the main false-positive source.
_BREAKDOWN_VOLUME_MIN = 1.0

# Relative-strength-vs-market gates (Phase 1 redesign). Reuses the long engine's own
# calibrated RS scale (compute_relative_strength; long soft-penalties use 0.8/1.2 as
# weak/strong) rather than inventing new numbers. Per O'Neil's Cardinal Rule: short a
# CONFIRMED LOSER, not a mirror-image winner — breakdown shorts require the symbol already
# underperforming the market; fade-top shorts require the OPPOSITE (a genuine former
# leader/momentum name completing a real top, not a laggard). Both fail closed (block) when
# RS data is unavailable rather than silently letting the short through.
BREAKDOWN_RS_MAX = 0.90   # breakdown: require rs_20d < this (confirmed relative weakness)
FADE_TOP_RS_MIN = 1.10    # fade-top: require rs_60d > this (confirmed prior outperformance)

# Horizon-matched target sizing (Phase 1 redesign). The prior design stretched TP to hit a
# fixed R:R floor regardless of whether that distance was reachable in the 14-day outcome
# window — the Tier-1 diagnosis found this was the dominant failure mode (72% IS / 55% OOS
# expiry rate, even when direction was right). Cap the stretch at what avg_daily_move implies
# is reachable within the horizon (leaving a buffer inside the 14-day window), and lower the
# R:R floor itself (was 2.0, blindly copied from the long engine) to one shorts can actually
# clear within that reachable distance.
_HORIZON_STRETCH_DAYS = 10

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

# Minimum reward:risk for a short to be emitted. Phase 1 redesign: lowered from 2.0 (blindly
# copied from the long engine's floor) to 1.5 (breakeven WR 40%) — paired with the
# horizon-matched cap above, so the target is required to be both reachable AND at least
# this favorable, rather than forced to an unreachable distance to hit a higher ratio.
_SHORT_MIN_RR = 1.5

# Phase 2 redesign (confirmation-bar entry delay): entering on the trigger bar itself risks
# catching the "trap" leg of a failed breakout/exhaustion signal that reverses instead of
# following through (research + our own Phase 1 data both point here — see
# docs/superpowers/plans/2026-09-18-shorts-engine.md §5). Arm on the trigger day; only enter
# once a subsequent day's close breaks below its own short-term EMA, confirming the move
# actually continued. Give it a few days to follow through, then give up.
_CONFIRMATION_EMA_SPAN = 10
_CONFIRMATION_WINDOW_DAYS = 3


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


@dataclass
class ArmedShort:
    """A trigger that fired and is waiting for confirmation (Phase 2 two-stage entry) — no
    signal yet. `family` and `trigger` are recorded at arm time; `armed_date` anchors the
    confirmation window. Confidence/levels are NOT computed here — see finalize_short_signal,
    which uses the confirmation day's fresh data, not the arm day's."""
    symbol: str
    category: str
    family: str
    trigger: str
    armed_date: date


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
    """Latest close < established support (by margin) AND volume_ratio > _BREAKDOWN_VOLUME_MIN."""
    if bars is None or len(bars) < 2:
        return False
    if snap.volume_ratio is None or snap.volume_ratio <= _BREAKDOWN_VOLUME_MIN:
        return False
    prev_close = float(bars["Close"].iloc[-2])
    support = _resolve_support(snap, bars, prev_close)
    if support is None:
        return False
    close = float(bars["Close"].iloc[-1])
    return close < support * (1 - _BREAK_MARGIN)


def failed_breakout(snap: TechnicalSnapshot, bars: pd.DataFrame) -> bool:
    """
    Within the last ~10 bars a high pierced resistance, but the latest close is back below
    it, AND volume_ratio > _BREAKDOWN_VOLUME_MIN (Phase 1 redesign — without this the pattern
    can fire on a single quiet down day with no real distribution behind the failure, the
    textbook "bull trap you're on the wrong side of" false positive).
    """
    if bars is None or len(bars) < 2:
        return False
    if snap.volume_ratio is None or snap.volume_ratio <= _BREAKDOWN_VOLUME_MIN:
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
    within 1% of support; latest close < support. Also requires volume_ratio >
    _BREAKDOWN_VOLUME_MIN on the rejection bar (Phase 1 redesign — this was empirically the
    worst-performing trigger with no volume confirmation at all; an unconfirmed rejection is
    the highest-risk entry per short-selling research, most exposed to a reclaim/squeeze).
    """
    if bars is None or len(bars) < 3:
        return False
    if snap.volume_ratio is None or snap.volume_ratio <= _BREAKDOWN_VOLUME_MIN:
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

def family_of_trigger(trigger: str) -> str:
    """Which family a trigger name belongs to — used by callers building an ArmedShort."""
    return "breakdown" if trigger in _BREAKDOWN_TRIGGERS else "fade_top"


def _breakdown_family_active(
    snap: TechnicalSnapshot, regime: Optional[str], rs_vs_market: Optional[float],
) -> bool:
    """
    Breakdown family structural gate: `bear` regime, price BELOW EMA200 (confirmed weakness),
    AND rs_vs_market confirms relative weakness (< BREAKDOWN_RS_MAX) — O'Neil's Cardinal Rule:
    short a confirmed loser, not a mirror-image winner. Fails closed (False) when RS data is
    unavailable — missing data is not treated as permission. Reused at both arm time (via
    detect_short_triggers) and confirmation time (via check_confirmation, re-checked against
    that day's fresh data so a setup that's no longer valid expires rather than confirming
    stale).
    """
    below_ema = snap.ema200 is None or snap.price < snap.ema200
    rs_ok = rs_vs_market is not None and rs_vs_market < BREAKDOWN_RS_MAX
    return regime == "bear" and below_ema and rs_ok


def _fade_top_family_active(
    snap: TechnicalSnapshot, tape_deteriorating: bool, rs_prior: Optional[float],
) -> bool:
    """
    Fade-top family structural gate: the tape is deteriorating (Option 1), price ABOVE EMA200
    (you only fade genuine strength at a high), AND rs_prior confirms this was a genuine former
    leader (> FADE_TOP_RS_MIN) — shorting exhaustion only makes sense on prior big winners
    completing a real top, not laggards. Fails closed when RS data is unavailable. See
    _breakdown_family_active for the arm/confirm reuse rationale.
    """
    above_ema = snap.ema200 is not None and snap.price > snap.ema200
    rs_ok = rs_prior is not None and rs_prior > FADE_TOP_RS_MIN
    return tape_deteriorating and above_ema and rs_ok


def detect_short_triggers(
    snap: TechnicalSnapshot,
    bars: pd.DataFrame,
    regime: Optional[str],
    tape_deteriorating: bool = False,
    rs_vs_market: Optional[float] = None,
    rs_prior: Optional[float] = None,
) -> list[str]:
    """
    Family-aware trigger detection with per-family activation + structural gate (see
    _breakdown_family_active / _fade_top_family_active) — blocked entirely in crisis/high_vol.
    """
    if regime in _BLOCKED_REGIMES:
        return []
    matched: list[str] = []

    if _breakdown_family_active(snap, regime, rs_vs_market):
        if support_breakdown(snap, bars):
            matched.append("support_breakdown")
        if failed_breakout(snap, bars):
            matched.append("failed_breakout")
        if failed_retest(snap, bars):
            matched.append("failed_retest")

    if _fade_top_family_active(snap, tape_deteriorating, rs_prior):
        if distribution(snap, bars):
            matched.append("distribution")
        if exhaustion(snap, bars):
            matched.append("exhaustion")

    return matched


# ── Top-level signal generation (Phase 2: two-stage entry) ───────────────────
# Replaces the old one-shot generate_short_signal (detect -> score -> level -> emit, entering
# on the trigger bar itself) with arm -> wait for confirmation -> finalize. Entering
# immediately risked catching the "trap" leg of a failed breakout/exhaustion signal that
# reverses instead of following through — see the module docstring and the Phase 2 notes on
# _CONFIRMATION_EMA_SPAN / _CONFIRMATION_WINDOW_DAYS above.

def try_arm_short(
    snap: TechnicalSnapshot,
    bars: pd.DataFrame,
    regime: Optional[str],
    tape_deteriorating: bool = False,
    rs_vs_market: Optional[float] = None,
    rs_prior: Optional[float] = None,
) -> Optional[str]:
    """
    Detect whether a NEW short setup should arm today. Returns the winning trigger name if a
    family's gates + pattern match (see detect_short_triggers), else None. Does NOT score or
    level — that happens at confirmation (finalize_short_signal), using the confirmation day's
    fresh data, not today's arm-day data.
    """
    triggers = detect_short_triggers(snap, bars, regime, tape_deteriorating, rs_vs_market, rs_prior)
    if not triggers:
        return None
    # Triggers are flattened to equal offsets within each family (Phase 1 redesign — see
    # TRIGGER_BASE_OFFSET); when several match, order is arbitrary among equals.
    return max(triggers, key=lambda t: TRIGGER_BASE_OFFSET[t])


def check_confirmation(
    armed: ArmedShort,
    snap: TechnicalSnapshot,
    bars: pd.DataFrame,
    regime: Optional[str],
    tape_deteriorating: bool,
    rs_vs_market: Optional[float],
    rs_prior: Optional[float],
    days_since_armed: int,
) -> Literal["confirmed", "pending", "expired"]:
    """
    Called once per trading day after arming, with THAT day's fresh data (not the arm day's).
    Re-checks the armed family's structural gate first — a setup that's no longer valid
    (regime flipped, RS recovered, EMA200 side flipped) expires rather than confirming on a
    stale read, regardless of the EMA criterion below. If the gate still holds and today's
    close is below its `_CONFIRMATION_EMA_SPAN`-day EMA, the move has continued: confirmed.
    Otherwise stays "pending" until `_CONFIRMATION_WINDOW_DAYS` elapses, then expires unused.
    """
    if regime in _BLOCKED_REGIMES:
        return "expired"
    family_active = (
        _breakdown_family_active(snap, regime, rs_vs_market) if armed.family == "breakdown"
        else _fade_top_family_active(snap, tape_deteriorating, rs_prior)
    )
    if not family_active:
        return "expired"
    if bars is not None and len(bars) >= _CONFIRMATION_EMA_SPAN:
        ema = float(bars["Close"].ewm(span=_CONFIRMATION_EMA_SPAN, adjust=False).mean().iloc[-1])
        close = float(bars["Close"].iloc[-1])
        if close < ema:
            return "confirmed"
    if days_since_armed >= _CONFIRMATION_WINDOW_DAYS:
        return "expired"
    return "pending"


def finalize_short_signal(
    armed: ArmedShort,
    snap: TechnicalSnapshot,
    bars: pd.DataFrame,
    *,
    regime_id: Optional[str],
    risk_tier: str,
    preferred_risk: str,
    sentiment_avg: Optional[float],
    sentiment_count: int,
    today: date,
) -> Optional[ShortSignal]:
    """
    Build the actual ShortSignal on the CONFIRMATION day — call only after check_confirmation()
    returns "confirmed". Entry/SL/TP/confidence all come from TODAY's snapshot (the
    confirmation day), not the arm day's; `armed.trigger` supplies which pattern originally
    fired so scoring routes to the right family scorer.
    """
    trigger = armed.trigger
    # Family-aware base: fade-tops use the bespoke top-quality scorer (the shared trend scorer
    # penalizes high-RSI shorts); breakdowns use the shared score_candidate(direction="short").
    if trigger in _FADE_TOP_TRIGGERS:
        base = top_quality_score(snap)
    else:
        base = score_candidate(
            snap, "short", regime_id, armed.category, preferred_risk,
            sentiment_avg, sentiment_count, today=today,
        ).confidence
    confidence = min(100, base + TRIGGER_BASE_OFFSET[trigger])
    if confidence < SHORT_CONFIDENCE_FLOOR:
        return None

    sl, tp = compute_atr_levels(snap.price, risk_tier, snapshot=snap, direction="short")
    # Phase 1 redesign: cap the TP stretch at what's actually reachable within the 14-day
    # outcome window (avg_daily_move x _HORIZON_STRETCH_DAYS), then require the result clears
    # the (lowered) R:R floor — rather than blindly stretching to hit a fixed ratio regardless
    # of reachability. The prior design's dominant failure mode was a 55-72% expiry rate: the
    # target was structurally too far to reach against the market's upward drift in 14 days.
    risk = sl - snap.price  # short: stop is above entry
    if risk > 0:
        target_distance = _SHORT_MIN_RR * risk
        if snap.avg_daily_move and snap.avg_daily_move > 0:
            reachable_distance = snap.price * snap.avg_daily_move * _HORIZON_STRETCH_DAYS
            target_distance = min(target_distance, reachable_distance)
        stretched_tp = snap.price - target_distance
        tp = min(tp, stretched_tp)  # only push TP further down, never up
    rr = calc_risk_reward(snap.price, sl, tp)
    if rr is None or rr < _SHORT_MIN_RR:
        return None

    return ShortSignal(
        symbol=armed.symbol, trigger=trigger, direction="short", confidence=confidence,
        entry=round(snap.price, 4), stop_loss=round(sl, 4), take_profit=round(tp, 4),
        risk_reward=round(rr, 4),
    )
