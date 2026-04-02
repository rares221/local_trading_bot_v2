"""
Optional FinBERT-based financial sentiment. Use in addition to VADER when
transformers and torch are installed (pip install transformers torch).
Returns score in [-1, 1] from positive/negative probabilities.
"""
from typing import Optional

_finbert_pipeline = None


def _get_finbert_pipeline():
    """Lazy-load FinBERT pipeline. Returns None if dependencies missing."""
    global _finbert_pipeline
    if _finbert_pipeline is not None:
        return _finbert_pipeline
    try:
        from transformers import pipeline
        _finbert_pipeline = pipeline(
            "sentiment-analysis",
            model="ProsusAI/finbert",
            truncation=True,
            max_length=512,
        )
        return _finbert_pipeline
    except Exception:
        _finbert_pipeline = False  # mark as attempted
        return None


def score_sentiment_finbert(text: str) -> Optional[float]:
    """
    FinBERT sentiment score in [-1, 1]. Positive -> positive, Negative -> negative,
    Neutral -> 0. Returns None if model or dependencies not available.
    """
    if not text or not text.strip():
        return None
    pipe = _get_finbert_pipeline()
    if not pipe:
        return None
    try:
        # Truncate to avoid token limit; model expects a single segment
        segment = text.strip()[:2000]
        out = pipe(segment)
        if not out:
            return None
        # out is e.g. [{"label": "positive", "score": 0.98}]
        label = out[0].get("label", "").lower()
        score = out[0].get("score", 0.5)
        if label == "positive":
            return float(score)
        if label == "negative":
            return -float(score)
        return 0.0  # neutral
    except Exception:
        return None


def is_finbert_available() -> bool:
    """True if FinBERT model can be loaded."""
    return _get_finbert_pipeline() is not None
