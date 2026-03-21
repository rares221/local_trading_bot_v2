"""
Adaptive confidence thresholds based on empirical hit rates from calibration data.
Dynamically adjusts min_confidence_threshold to target a desired win rate.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Target hit rate for recommendations (e.g., 0.55 = 55% win rate)
TARGET_HIT_RATE = 0.55

# Cached adaptive thresholds per regime (updated when calibration data changes)
_adaptive_thresholds: dict[str, int] = {}


def compute_adaptive_threshold(
    by_bucket: dict[str, dict],
    target_hit_rate: float = TARGET_HIT_RATE,
) -> int:
    """
    Compute adaptive confidence threshold based on empirical hit rates per bucket.
    Returns the minimum confidence score that achieves >= target_hit_rate.
    
    Args:
        by_bucket: Dict of {bucket_name: {n, hit_tp, hit_sl, expired, hit_rate}}
        target_hit_rate: Desired minimum hit rate (default 0.55 = 55%)
    
    Returns:
        Minimum confidence score (0-100) to achieve target hit rate.
        Falls back to 35 if no data or all buckets below target.
    """
    # Bucket midpoints for mapping bucket -> confidence score
    bucket_midpoints = {
        "low": 20,        # 0-40 range
        "medium": 50,     # 40-60 range
        "high": 70,       # 60-80 range
        "very_high": 90,  # 80-100 range
    }
    
    # Find buckets that meet or exceed target hit rate
    qualifying_buckets = []
    for bucket_name in ["low", "medium", "high", "very_high"]:
        info = by_bucket.get(bucket_name, {})
        n = info.get("n", 0)
        hit_rate = info.get("hit_rate")
        
        if n >= 5 and hit_rate is not None and hit_rate >= target_hit_rate:
            qualifying_buckets.append((bucket_name, hit_rate, bucket_midpoints[bucket_name]))
    
    if not qualifying_buckets:
        logger.info(
            "[ADAPTIVE] No bucket meets target hit rate %.2f; using default threshold 35",
            target_hit_rate,
        )
        return 35
    
    # Return the lowest confidence score among qualifying buckets
    qualifying_buckets.sort(key=lambda x: x[2])  # sort by confidence midpoint
    best_bucket, best_rate, best_conf = qualifying_buckets[0]
    
    logger.info(
        "[ADAPTIVE] Adaptive threshold: %d (bucket=%s, hit_rate=%.2f, target=%.2f)",
        best_conf, best_bucket, best_rate, target_hit_rate,
    )
    return best_conf


def update_adaptive_thresholds(by_bucket: dict[str, dict]) -> None:
    """
    Update cached adaptive thresholds for all regimes based on calibration data.
    In future, could compute regime-specific thresholds if outcome data includes regime.
    For now, uses a single global threshold.
    """
    global _adaptive_thresholds
    
    threshold = compute_adaptive_threshold(by_bucket)
    _adaptive_thresholds = {
        "risk_on": max(30, threshold - 5),    # slightly lower in risk_on
        "cautious": threshold,                 # baseline
        "risk_off": min(50, threshold + 5),   # slightly higher in risk_off
    }
    
    logger.info(
        "[ADAPTIVE] Updated thresholds: risk_on=%d, cautious=%d, risk_off=%d",
        _adaptive_thresholds.get("risk_on", 35),
        _adaptive_thresholds.get("cautious", 35),
        _adaptive_thresholds.get("risk_off", 40),
    )


def get_adaptive_threshold(regime: str = "cautious") -> Optional[int]:
    """
    Get cached adaptive threshold for a given regime.
    Returns None if not yet computed (use default thresholds).
    """
    return _adaptive_thresholds.get(regime)


def is_adaptive_enabled() -> bool:
    """Check if adaptive thresholds have been computed."""
    return len(_adaptive_thresholds) > 0
