"""
Sentiment and asset tagging. Phase 1: VADER on title/description; simple keyword → asset_tags.
"""
import re
from typing import Optional

# Optional: vaderSentiment is in requirements
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _analyzer = SentimentIntensityAnalyzer()
except ImportError:
    _analyzer = None

# Keywords for oil/gold/gas (extend as needed)
ASSET_KEYWORDS: dict[str, list[str]] = {
    "oil": ["oil", "wti", "brent", "crude", "opec", "barrel", "xom", "cvx", "chevron", "exxon"],
    "gold": ["gold", "xau", "gld", "precious", "miner", "newmont", "barrick"],
    "gas": ["gas", "natural gas", "henry hub", "lng", "energy"],
}


def score_sentiment(text: str) -> Optional[float]:
    """
    Return compound sentiment score in [-1, 1] or None if VADER not installed.
    Uses title + description in pipeline.
    """
    if not text or not _analyzer:
        return None
    scores = _analyzer.polarity_scores(text)
    return scores.get("compound")


def tag_assets(text: str) -> list[str]:
    """Return asset_tags (e.g. ['oil','gold']) from keyword match on text."""
    if not text:
        return []
    lower = text.lower()
    tags = []
    for asset, keywords in ASSET_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            tags.append(asset)
    return tags
