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

# Keywords: tag name -> trigger words (lowercase). Tags align with recommendation engine where possible.
ASSET_KEYWORDS: dict[str, list[str]] = {
    # Umbrella tags (broad)
    "oil": ["oil", "crude", "opec", "barrel", "petroleum"],
    "gold": ["gold", "xau", "gld", "precious", "miner"],
    "gas": ["gas", "natural gas", "lng", "henry hub", "energy"],
    # Engine assets (stocks)
    "XOM": ["xom", "exxon"],
    "CVX": ["cvx", "chevron"],
    "BP": ["bp", "british petroleum"],
    "SHEL": ["shel", "shell"],
    "NEM": ["nem", "newmont", "barrick"],
    # Engine assets (commodities)
    "WTI": ["wti", "west texas", "cl="],
    "Brent": ["brent", "brent crude", "bz="],
    "XAUUSD": ["xau", "gold", "gc="],
    "Henry Hub": ["henry hub", "natural gas", "ng="],
    "Silver": ["silver", "si="],
    # Other (forex/crypto) – keep generic so we can map to our list
    "BTC": ["btc", "bitcoin"],
    "ETH": ["eth", "ethereum"],
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
