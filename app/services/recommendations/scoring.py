"""
Pure, IO-free signal-scoring adjustments shared across the batch engine, the backtest
replay, and live scoring.

Phase 1 of the engine-parity plan extracts the confidence-adjustment pipeline out of
`engine._build_candidate` into this module so all three paths score identically. Everything
here is a pure function of its arguments — no DB, no network, no clock. Callers that need
"today" pass it in (so the turn-of-month bonus stays deterministic and testable).

Behavior is preserved byte-for-byte from the original inline engine code; see
`tests/unit/services/test_engine_build_candidate_golden.py` for the characterization net.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from app.services.technicals import TechnicalSnapshot, compute_confidence

# Stock cap-tier categories — mirrors engine._STOCK_CATEGORIES (kept local to stay IO-free).
_STOCK_CATEGORIES: tuple[str, ...] = ("stocks_large", "stocks_mid", "stocks_small")


def compute_soft_penalties(
    snap: Optional[TechnicalSnapshot],
    direction: str,
    category: str,
    preferred_risk: str,
    vel_confidence_penalty: int,
) -> tuple[int, list[str]]:
    """
    Pool all confidence-reducing "soft" signals so no combination of weak signals can stack
    into an invisible hard gate. Returns (pooled_penalty capped at 25, itemized notes).

    `vel_confidence_penalty` (sentiment-velocity fade) is computed by the caller from DB
    sentiment and passed in — everything else is a pure function of the snapshot/direction.
    """
    adx_penalty = 0
    if snap and snap.adx is not None:
        if snap.adx[0] < 20:
            adx_penalty = 8

    macd_alignment_penalty = 0
    if snap and snap.macd is not None:
        if direction == "long" and snap.macd_bearish:
            macd_alignment_penalty = 8
        elif direction == "short" and snap.macd_bullish:
            macd_alignment_penalty = 8

    vol_floor = 0.80 if preferred_risk == "conservative" else (0.65 if preferred_risk == "aggressive" else 0.75)
    volume_penalty = 0
    if snap and snap.volume_ratio is not None:
        if category in _STOCK_CATEGORIES and snap.volume_ratio < vol_floor:
            volume_penalty = 6

    obv_penalty = 0
    if snap:
        if direction == "long" and snap.obv_confirming_down:
            obv_penalty = 5
        elif direction == "short" and snap.obv_confirming_up:
            obv_penalty = 5

    macd_decay_penalty = 0
    if snap and snap.macd is not None and snap.macd_hist_slope is not None:
        if direction == "long" and snap.macd[2] > 0 and snap.macd_hist_slope < 0:
            macd_decay_penalty = 5
        elif direction == "short" and snap.macd[2] < 0 and snap.macd_hist_slope > 0:
            macd_decay_penalty = 5

    rsi_slope_penalty = 0
    if snap and snap.rsi is not None and snap.rsi_slope is not None:
        if direction == "long":
            if snap.rsi_slope < -4.0:
                rsi_slope_penalty = 8
            elif snap.rsi_slope < -2.0:
                rsi_slope_penalty = 4
        elif direction == "short":
            if snap.rsi_slope > 2.0:
                rsi_slope_penalty = 4

    cap_tier_penalty = 0
    if category == "stocks_mid":
        cap_tier_penalty = 4
    elif category == "stocks_small":
        cap_tier_penalty = 6

    total = min(
        adx_penalty + obv_penalty + vel_confidence_penalty + macd_decay_penalty
        + rsi_slope_penalty + macd_alignment_penalty + volume_penalty + cap_tier_penalty,
        25,
    )

    notes: list[str] = []
    if adx_penalty > 0:
        notes.append(f"adx_weak_trend=-{adx_penalty}")
    if obv_penalty > 0:
        notes.append(f"obv_distribution=-{obv_penalty}")
    if vel_confidence_penalty > 0:
        notes.append(f"sentiment_fading=-{vel_confidence_penalty}")
    if macd_decay_penalty > 0:
        notes.append(f"macd_momentum_decay=-{macd_decay_penalty}")
    if rsi_slope_penalty > 0:
        notes.append(f"rsi_slope=-{rsi_slope_penalty}")
    if macd_alignment_penalty > 0:
        notes.append(f"macd_alignment=-{macd_alignment_penalty}")
    if volume_penalty > 0:
        notes.append(f"volume_low=-{volume_penalty}")
    if cap_tier_penalty > 0:
        notes.append(f"cap_tier=-{cap_tier_penalty}")

    return total, notes


@dataclass
class ScoreResult:
    """Result of the shared confidence composition (base → soft-penalties → bonuses)."""
    confidence: int
    base_confidence: int
    soft_penalty: int
    penalty_notes: list[str] = field(default_factory=list)
    bonus_notes: list[str] = field(default_factory=list)


def score_candidate(
    snap: Optional[TechnicalSnapshot],
    direction: str,
    regime_id: Optional[str],
    category: str,
    preferred_risk: str,
    sentiment_avg: Optional[float],
    sentiment_count: int,
    *,
    today: date,
    vel_confidence_penalty: int = 0,
    sentiment_signal_scale: float = 1.0,
    indicator_conflict_multiplier: float = 1.0,
) -> ScoreResult:
    """
    Canonical confidence composition shared by the batch engine, replay, and live:
    base indicator score (`compute_confidence`) → pooled soft-penalties → signal bonuses.

    This is the pipeline order all three paths use; extracting it here guarantees they cannot
    drift. Hard gates (RSI-exhaustion, OFI) and path-specific adjustments (market context,
    earnings, conviction, etc.) remain the caller's responsibility — their placement differs
    per path — but the core confidence number is computed identically everywhere.
    """
    base = compute_confidence(
        sentiment_avg,
        sentiment_count,
        snap,
        direction,
        regime=regime_id,
        sentiment_signal_scale=sentiment_signal_scale,
        indicator_conflict_multiplier=indicator_conflict_multiplier,
    )
    confidence = base
    penalty, penalty_notes = compute_soft_penalties(
        snap, direction, category, preferred_risk, vel_confidence_penalty
    )
    if penalty > 0:
        confidence = max(0, confidence - penalty)
    confidence, bonus_notes = apply_signal_bonuses(confidence, snap, direction, today)
    return ScoreResult(
        confidence=confidence,
        base_confidence=base,
        soft_penalty=penalty,
        penalty_notes=penalty_notes,
        bonus_notes=bonus_notes,
    )


def rsi_exhaustion_ceiling(
    regime_id: Optional[str],
    preferred_risk: str,
) -> tuple[int, int]:
    """
    Regime + preference-aware RSI exhaustion bounds. Returns (long_ceiling, short_floor).

    regime_id is the canonical 5-label: bull widens the long ceiling by 5; the defensive
    regimes (bear/high_vol/crisis) cap it at 60. Preference: aggressive +5, conservative -2.
    Final ceiling is clamped to [58, 75]; the short floor mirrors it (100 - ceiling).
    """
    ceiling = 65
    if regime_id == "bull":
        ceiling += 5
    if preferred_risk == "aggressive":
        ceiling += 5
    elif preferred_risk == "conservative":
        ceiling -= 2
    if regime_id in ("bear", "high_vol", "crisis"):
        ceiling = min(ceiling, 60)
    ceiling = max(58, min(75, ceiling))
    return ceiling, 100 - ceiling


def is_rsi_exhausted(
    snap: Optional[TechnicalSnapshot],
    direction: str,
    regime_id: Optional[str],
    preferred_risk: str,
) -> bool:
    """True when RSI is at a mean-reversion extreme against the trade (→ hard skip)."""
    if not (snap and snap.rsi is not None):
        return False
    ceiling, floor = rsi_exhaustion_ceiling(regime_id, preferred_risk)
    if direction == "long" and snap.rsi > ceiling:
        return True
    if direction == "short" and snap.rsi < floor:
        return True
    return False


def apply_ofi_gate(
    confidence: int,
    snap: Optional[TechnicalSnapshot],
    direction: str,
) -> tuple[int, bool]:
    """
    Order Flow Imbalance gate + soft adjustment. Returns (confidence, skip).

    Strong distribution on a long (OFI < 0.35) or strong accumulation on a short (OFI > 0.65)
    forces a hard skip (skip=True). Milder imbalance nudges confidence ±.
    """
    if snap and snap.ofi_ratio is not None:
        ofi = snap.ofi_ratio
        if direction == "long":
            if ofi < 0.35:
                return confidence, True
            elif ofi < 0.4:
                confidence = max(0, confidence - 5)
            elif ofi > 0.6:
                confidence = min(100, confidence + 3)
        elif direction == "short":
            if ofi > 0.65:
                return confidence, True
            elif ofi > 0.6:
                confidence = max(0, confidence - 5)
            elif ofi < 0.4:
                confidence = min(100, confidence + 3)
    return confidence, False


def apply_signal_bonuses(
    confidence: int,
    snap: Optional[TechnicalSnapshot],
    direction: str,
    today: date,
) -> tuple[int, list[str]]:
    """
    Reward high-conviction setups (the "Arc 5" bonus signals). Returns the adjusted
    confidence and an itemized list of applied-bonus notes (for logging/UI).

    Each bonus is applied stepwise with a 0..100 clamp, exactly as the engine did inline,
    so the result is identical even as confidence approaches the ceiling.
    """
    bonus_parts: list[str] = []

    # Item 3 — MACD + RSI dual alignment bonus (+6 pts):
    # Both indicators agree on direction with RSI in a healthy (non-extreme) zone.
    if snap and snap.macd is not None and snap.rsi is not None:
        if direction == "long" and snap.macd[2] > 0 and 45 <= snap.rsi <= 65:
            confidence = min(100, confidence + 6)
            bonus_parts.append("macd_rsi_aligned=+6")
        elif direction == "short" and snap.macd[2] < 0 and 35 <= snap.rsi <= 55:
            confidence = min(100, confidence + 6)
            bonus_parts.append("macd_rsi_aligned_short=+6")

    # Item 4 — Volume surge bonus (+5 pts): volume_ratio > 1.5 = genuine buying pressure.
    if snap and snap.volume_ratio is not None and snap.volume_ratio > 1.5:
        confidence = min(100, confidence + 5)
        bonus_parts.append(f"volume_surge=+5 ({snap.volume_ratio:.2f}x)")

    # Item 5 — RSI directional momentum bonus (+4 pts):
    if snap and snap.rsi is not None and snap.rsi_slope is not None:
        if direction == "long" and snap.rsi_slope > 0 and snap.rsi < 65:
            confidence = min(100, confidence + 4)
            bonus_parts.append(f"rsi_momentum=+4 (slope={snap.rsi_slope:+.1f})")
        elif direction == "short" and snap.rsi_slope < 0 and snap.rsi > 35:
            confidence = min(100, confidence + 4)
            bonus_parts.append(f"rsi_momentum_short=+4 (slope={snap.rsi_slope:+.1f})")

    # Item 5b — Triple-confluence bonus (+5 pts): all three indicators aligned with direction.
    if snap and snap.macd is not None and snap.rsi is not None and snap.volume_ratio is not None:
        if direction == "long" and snap.macd[2] > 0 and snap.rsi > 50 and snap.volume_ratio > 1.0:
            confidence = min(100, confidence + 5)
            bonus_parts.append("triple_confluence=+5")
        elif direction == "short" and snap.macd[2] < 0 and snap.rsi < 50 and snap.volume_ratio > 1.0:
            confidence = min(100, confidence + 5)
            bonus_parts.append("triple_confluence_short=+5")

    # Item 5c — ATR percentile regime: high-vol (>75th pct) penalizes; low-vol (<25th pct) rewards.
    if snap and snap.atr_percentile is not None:
        if snap.atr_percentile > 0.75:
            confidence = max(0, confidence - 3)
            bonus_parts.append(f"atr_regime=-3 (high_vol pct={snap.atr_percentile:.2f})")
        elif snap.atr_percentile < 0.25:
            confidence = min(100, confidence + 3)
            bonus_parts.append(f"atr_regime=+3 (low_vol pct={snap.atr_percentile:.2f})")

    # Turn-of-month bonus (+2 pts): first 3 / last 2 calendar days.
    _last_dom = calendar.monthrange(today.year, today.month)[1]
    if today.day <= 3 or today.day >= _last_dom - 1:
        confidence = min(100, confidence + 2)
        bonus_parts.append("turn_of_month=+2")

    # Item 6 — 52-week high proximity bonus (+8 / +4 pts): momentum near the highs (longs only).
    if direction == "long" and snap and snap.pct_from_52w_high is not None:
        vol_ok = snap.volume_ratio is None or snap.volume_ratio >= 1.0
        if snap.pct_from_52w_high <= 0.03 and vol_ok:
            confidence = min(100, confidence + 8)
            bonus_parts.append(f"near_52w_high=+8 ({snap.pct_from_52w_high*100:.1f}% below)")
        elif snap.pct_from_52w_high <= 0.08 and vol_ok:
            confidence = min(100, confidence + 4)
            bonus_parts.append(f"near_52w_high=+4 ({snap.pct_from_52w_high*100:.1f}% below)")

    # Item 7 — EMA200 distance sweet spot (+3 / -3 pts):
    if snap and snap.ema200 is not None and snap.ema200 > 0:
        if direction == "long":
            ema_dist = (snap.price - snap.ema200) / snap.ema200
            if 0.02 <= ema_dist <= 0.10:
                confidence = min(100, confidence + 3)
                bonus_parts.append(f"ema200_sweet_spot=+3 ({ema_dist*100:.1f}% above)")
            elif ema_dist > 0.15:
                confidence = max(0, confidence - 3)
                bonus_parts.append(f"ema200_overextended=-3 ({ema_dist*100:.1f}% above)")
        elif direction == "short":
            ema_dist = (snap.ema200 - snap.price) / snap.ema200
            if 0.02 <= ema_dist <= 0.10:
                confidence = min(100, confidence + 3)
                bonus_parts.append(f"ema200_sweet_spot_short=+3 ({ema_dist*100:.1f}% below)")
            elif ema_dist > 0.15:
                confidence = max(0, confidence - 3)
                bonus_parts.append(f"ema200_overextended_short=-3 ({ema_dist*100:.1f}% below)")

    return confidence, bonus_parts
