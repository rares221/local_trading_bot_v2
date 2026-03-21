"""
Market regime transition monitoring and alerts.
Detects when regime changes (risk_on <-> cautious <-> risk_off) and flags
active trades that may need review or adjustment.
"""
import logging
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Last known regime (persisted in memory; could be stored in DB for persistence across restarts)
_last_regime: Optional[str] = None
_last_regime_at: Optional[datetime] = None


@dataclass
class RegimeTransition:
    """Represents a detected regime change."""
    from_regime: str
    to_regime: str
    detected_at: datetime
    severity: str  # "minor" (risk_on <-> cautious) or "major" (any <-> risk_off)
    
    def description(self) -> str:
        return f"Regime transition: {self.from_regime} → {self.to_regime} (severity: {self.severity})"


def detect_regime_transition(current_regime: str) -> Optional[RegimeTransition]:
    """
    Detect if regime has changed since last check.
    Returns RegimeTransition if changed, None otherwise.
    Updates internal state to track current regime.
    """
    global _last_regime, _last_regime_at
    
    if _last_regime is None:
        # First run: initialize without triggering transition
        _last_regime = current_regime
        _last_regime_at = datetime.now(timezone.utc)
        logger.info("[REGIME] Initialized regime tracking: %s", current_regime)
        return None
    
    if _last_regime == current_regime:
        # No change
        return None
    
    # Regime changed
    now = datetime.now(timezone.utc)
    
    # Determine severity
    severity = "minor"
    if _last_regime == "risk_off" or current_regime == "risk_off":
        severity = "major"
    
    transition = RegimeTransition(
        from_regime=_last_regime,
        to_regime=current_regime,
        detected_at=now,
        severity=severity,
    )
    
    logger.warning(
        "[REGIME] %s (previous regime lasted %.1f hours)",
        transition.description(),
        (now - _last_regime_at).total_seconds() / 3600 if _last_regime_at else 0,
    )
    
    # Update state
    _last_regime = current_regime
    _last_regime_at = now
    
    return transition


def get_current_regime_state() -> dict:
    """
    Get current regime tracking state (for API/UI display).
    """
    return {
        "current_regime": _last_regime,
        "regime_since": _last_regime_at.isoformat() if _last_regime_at else None,
    }


def should_skip_recommendations_on_transition(transition: Optional[RegimeTransition]) -> bool:
    """
    Decide if new recommendations should be skipped during a regime transition.
    Conservative approach: skip major transitions (to/from risk_off) for safety.
    """
    if transition is None:
        return False
    
    # Skip recommendations on major transitions (to/from risk_off)
    if transition.severity == "major":
        logger.info("[REGIME] Skipping new recommendations due to major regime transition")
        return True
    
    return False


def get_trades_needing_review(
    active_trades: list,
    transition: Optional[RegimeTransition],
) -> list[dict]:
    """
    Identify active trades that may need review due to regime transition.
    
    Rules:
    - Major transition (to/from risk_off): flag all open trades
    - Minor transition (risk_on <-> cautious): flag trades with signal_flip_count > 0
    
    Returns list of dicts with {trade_id, asset_id, reason}.
    """
    if transition is None:
        return []
    
    flagged = []
    
    for trade in active_trades:
        trade_id = trade.get("id")
        asset_id = trade.get("asset_id")
        phase = trade.get("trade_phase", "open")
        signal_flip_count = trade.get("signal_flip_count", 0)
        
        # Skip already closed trades
        if phase and phase.startswith("closed"):
            continue
        
        reason = None
        
        if transition.severity == "major":
            # Major transition: flag all open trades
            if transition.to_regime == "risk_off":
                reason = f"Major regime shift to risk_off — consider tightening stops or reducing exposure"
            else:
                reason = f"Major regime shift from risk_off to {transition.to_regime} — review if trade thesis still valid"
        
        elif transition.severity == "minor" and signal_flip_count > 0:
            # Minor transition + already weakening signal: flag for review
            reason = f"Minor regime shift ({transition.from_regime} → {transition.to_regime}) + signal weakening (flip count: {signal_flip_count})"
        
        if reason:
            flagged.append({
                "trade_id": trade_id,
                "asset_id": asset_id,
                "reason": reason,
            })
    
    if flagged:
        logger.info(
            "[REGIME] %d active trades flagged for review due to %s",
            len(flagged), transition.description(),
        )
    
    return flagged


def reset_regime_tracking() -> None:
    """Reset regime tracking (for testing or manual override)."""
    global _last_regime, _last_regime_at
    _last_regime = None
    _last_regime_at = None
    logger.info("[REGIME] Regime tracking reset")
