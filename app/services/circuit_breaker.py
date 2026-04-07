"""
Circuit Breaker: protects the trading account from drawdown spirals by
computing the current risk mode (normal / defensive / paused) from recent
closed trade outcomes.

Three independent gates — any one can trigger defensive or paused mode:
  1. rolling_loss_gate  — R-multiples lost in the last 24h (hit_sl × fraction)
  2. consecutive_gate  — streak of hit_sl; only hit_tp breaks it (expired/manual are neutral)
  3. drawdown_gate     — peak-to-trough on the cumulative R-curve (all history)

Modes:
  normal    — everything as usual
  defensive — confidence threshold raised (+10), position sizing reduced (×0.7)
  paused    — no new signals generated; existing trades still managed

Design notes:
  - expired / manual outcomes are NEUTRAL in the streak: they don't extend a losing streak
    (they're not losses) but they also don't reset it (they're not confirmed wins).
    Only hit_tp resets the streak.
  - Rolling 24h window for daily loss gate (not calendar day) so a loss at 23:55 carries
    into the next morning instead of evaporating at midnight.
  - Drawdown is computed over all closed history so long runs of losses are captured even
    if they started days ago.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_PAUSED_DAILY_LOSS_MULTIPLIER = 2.0   # e.g. if max_rolling_loss_r=3 → pause at 6R
_ROLLING_WINDOW_HOURS = 24


@dataclass
class CircuitBreakerState:
    mode: str = "normal"             # normal | defensive | paused
    rolling_loss_r: float = 0.0      # R-multiples lost in last 24h
    consecutive_losses: int = 0      # current losing streak (reset only on hit_tp)
    drawdown_r: float = 0.0          # peak-to-trough on cumulative R-curve
    triggered_by: list[str] = field(default_factory=list)

    @property
    def is_paused(self) -> bool:
        return self.mode == "paused"

    @property
    def is_defensive(self) -> bool:
        return self.mode == "defensive"

    def confidence_penalty(self) -> int:
        """Extra confidence penalty to apply in defensive mode."""
        return 10 if self.mode == "defensive" else 0

    def sizing_multiplier(self) -> float:
        """Position sizing multiplier (applied to suggested_risk_pct)."""
        return 0.7 if self.mode == "defensive" else 1.0


def _to_utc(dt) -> Optional[datetime]:
    """Normalize outcome_at to UTC datetime."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def compute_state(
    db_trades: list,
    max_consecutive_losses: int = 5,
    max_daily_loss_r: float = 3.0,
    drawdown_gate_r: float = 8.0,
) -> CircuitBreakerState:
    """
    Compute circuit breaker state from a list of closed ActiveTrade-like objects.

    Each trade must have:
      - outcome: str  (hit_sl | hit_tp | expired | manual | None)
      - outcome_at: datetime or None
      - position_fraction: float  (1.0 for vanilla, <1.0 for R-ladder legs)

    Streak semantics:
      hit_sl  → extends streak
      hit_tp  → resets streak to 0 (confirmed win)
      expired → neutral: skipped; streak continues as-is
      manual  → neutral: skipped; streak continues as-is
    """
    if not db_trades:
        return CircuitBreakerState()

    now_utc = datetime.now(timezone.utc)
    rolling_cutoff = now_utc - timedelta(hours=_ROLLING_WINDOW_HOURS)

    # Only closed outcomes with a timestamp, sorted newest-first
    closed = [
        t for t in db_trades
        if getattr(t, "outcome", None) in ("hit_sl", "hit_tp", "expired", "manual")
        and _to_utc(getattr(t, "outcome_at", None)) is not None
    ]
    closed.sort(key=lambda t: _to_utc(t.outcome_at), reverse=True)

    # ── Gate 1: rolling 24h loss in R-multiples ──────────────────────────────
    rolling_loss_r = 0.0
    for t in closed:
        oa = _to_utc(t.outcome_at)
        if oa and oa >= rolling_cutoff and t.outcome == "hit_sl":
            rolling_loss_r += float(getattr(t, "position_fraction", 1.0))

    # ── Gate 2: consecutive losses (only hit_tp breaks the streak) ───────────
    # Walk from newest to oldest; expired/manual are transparent (skip, keep counting).
    consecutive = 0
    for t in closed:
        outcome = t.outcome
        if outcome == "hit_sl":
            consecutive += 1
        elif outcome == "hit_tp":
            break          # confirmed win — streak reset
        # expired / manual: neutral, continue scanning older trades

    # ── Gate 3: cumulative R-curve drawdown (full history) ───────────────────
    # Ascending order (oldest first) for the equity curve.
    # expired / manual contribute 0 to the curve — they're not wins or losses in R terms.
    trades_asc = list(reversed(closed))
    r_curve: list[float] = []
    cumulative = 0.0
    for t in trades_asc:
        frac = float(getattr(t, "position_fraction", 1.0))
        if t.outcome == "hit_tp":
            cumulative += frac
        elif t.outcome == "hit_sl":
            cumulative -= frac
        # expired / manual: no R contribution
        r_curve.append(cumulative)

    drawdown_r = 0.0
    if r_curve:
        peak = r_curve[0]
        for val in r_curve[1:]:
            if val > peak:
                peak = val
            dd = peak - val
            if dd > drawdown_r:
                drawdown_r = dd

    # ── Determine mode ────────────────────────────────────────────────────────
    triggered_by: list[str] = []
    mode = "normal"

    pause_loss_threshold = max_daily_loss_r * _PAUSED_DAILY_LOSS_MULTIPLIER
    pause_consec_threshold = max_consecutive_losses + 2

    # Hard stops → paused
    if rolling_loss_r >= pause_loss_threshold:
        triggered_by.append(
            f"rolling_loss_r={rolling_loss_r:.1f}R ≥ pause={pause_loss_threshold:.1f}R (24h)"
        )
        mode = "paused"
    elif drawdown_r >= drawdown_gate_r:
        triggered_by.append(f"drawdown_r={drawdown_r:.1f}R ≥ {drawdown_gate_r:.1f}R")
        mode = "paused"
    elif consecutive >= pause_consec_threshold:
        triggered_by.append(
            f"consecutive_losses={consecutive} ≥ pause={pause_consec_threshold}"
        )
        mode = "paused"

    # Soft stops → defensive
    if mode == "normal":
        if rolling_loss_r >= max_daily_loss_r:
            triggered_by.append(
                f"rolling_loss_r={rolling_loss_r:.1f}R ≥ {max_daily_loss_r:.1f}R (24h)"
            )
            mode = "defensive"
        elif consecutive >= max_consecutive_losses:
            triggered_by.append(
                f"consecutive_losses={consecutive} ≥ {max_consecutive_losses}"
            )
            mode = "defensive"
        elif drawdown_r >= drawdown_gate_r * 0.75:
            triggered_by.append(
                f"drawdown_r={drawdown_r:.1f}R ≥ {drawdown_gate_r * 0.75:.1f}R (75% gate)"
            )
            mode = "defensive"

    if triggered_by:
        logger.warning("[CIRCUIT_BREAKER] mode=%s triggered_by=%s", mode, triggered_by)

    return CircuitBreakerState(
        mode=mode,
        rolling_loss_r=round(rolling_loss_r, 2),
        consecutive_losses=consecutive,
        drawdown_r=round(drawdown_r, 2),
        triggered_by=triggered_by,
    )
