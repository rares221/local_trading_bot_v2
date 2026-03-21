"""
Confidence calibration using historical outcomes. Platt scaling (logistic regression)
or isotonic regression to map raw confidence -> calibrated probability.
"""
from typing import Optional
import math
import logging

logger = logging.getLogger(__name__)

# Fitted calibration parameters (Platt scaling: logistic A*conf + B)
# Updated by fit_calibration() when enough outcomes exist.
_platt_a: Optional[float] = None
_platt_b: Optional[float] = None


def fit_platt_calibration(confidences: list[float], outcomes: list[float]) -> tuple[float, float]:
    """
    Fit Platt scaling (logistic regression) on (confidence/100, outcome 0/1).
    Returns (coef, intercept) such that calibrated_prob = 1 / (1 + exp(-(coef*conf + intercept))).
    Uses sklearn LogisticRegression if available; falls back to identity mapping.
    """
    if len(confidences) < 10:
        logger.warning("[CALIBRATION] Not enough data (n=%d < 10) to fit Platt calibration", len(confidences))
        return (0.0, 0.0)
    
    try:
        from sklearn.linear_model import LogisticRegression
        import numpy as np
        
        X = np.array(confidences).reshape(-1, 1)
        y = np.array(outcomes)
        
        # Fit logistic regression with no regularization (C=1e10) for Platt scaling
        lr = LogisticRegression(C=1e10, max_iter=1000, solver='lbfgs')
        lr.fit(X, y)
        
        coef = float(lr.coef_[0][0])
        intercept = float(lr.intercept_[0])
        
        logger.info("[CALIBRATION] Platt calibration fitted: coef=%.4f, intercept=%.4f (n=%d)", coef, intercept, len(confidences))
        return (coef, intercept)
    
    except ImportError:
        logger.warning("[CALIBRATION] sklearn not available; Platt calibration disabled. Install: pip install scikit-learn")
        return (0.0, 0.0)
    except Exception as e:
        logger.error("[CALIBRATION] Failed to fit Platt calibration: %s", e, exc_info=True)
        return (0.0, 0.0)


def apply_platt_calibration(raw_confidence: int) -> float:
    """
    Apply Platt calibration to raw confidence (0-100) -> calibrated probability [0,1].
    If not fitted yet, returns raw_confidence / 100.
    Formula: calibrated_prob = 1 / (1 + exp(-(coef*conf + intercept)))
    """
    if _platt_a is None or _platt_b is None:
        return raw_confidence / 100.0
    conf_norm = raw_confidence / 100.0
    z = _platt_a * conf_norm + _platt_b
    # Clamp z to avoid overflow in exp
    z = max(-500, min(500, z))
    return 1.0 / (1.0 + math.exp(-z))


def set_platt_params(a: float, b: float) -> None:
    """Set fitted Platt parameters (called after fit_calibration)."""
    global _platt_a, _platt_b
    _platt_a = a
    _platt_b = b


def get_platt_params() -> tuple[Optional[float], Optional[float]]:
    """Get current Platt parameters (None if not fitted)."""
    return (_platt_a, _platt_b)
