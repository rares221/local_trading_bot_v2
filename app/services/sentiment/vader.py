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
    # ── Commodities (umbrella) ───────────────────────────────────
    "oil": ["oil", "crude", "opec", "barrel", "petroleum", "opec+"],
    "gold": ["gold", "xau", "gld", "precious", "bullion"],
    "gas": ["natural gas", "lng", "henry hub"],
    "silver": ["silver", "si="],
    "copper": ["copper", "hg="],
    "iron ore": ["iron ore", "iron-ore"],
    "wheat": ["wheat", "grain"],
    "corn": ["corn", "maize"],
    # ── Energy stocks ────────────────────────────────────────────
    "XOM": ["xom", "exxon", "exxonmobil"],
    "CVX": ["cvx", "chevron"],
    "BP": ["bp", "british petroleum"],
    "SHEL": ["shel", "shell", "royal dutch"],
    "TTE": ["totalenergies", "total energies"],
    # ── Mining / metals ──────────────────────────────────────────
    "NEM": ["nem", "newmont"],
    "GOLD": ["barrick", "barrick gold"],
    "RIO": ["rio tinto"],
    "BHP": ["bhp", "bhp group"],
    "VALE": ["vale"],
    "FCX": ["freeport", "freeport-mcmoran"],
    # ── Commodities (futures) ────────────────────────────────────
    "WTI": ["wti", "west texas", "cl="],
    "Brent": ["brent", "brent crude", "bz="],
    "XAUUSD": ["xau", "gold price", "gc="],
    "Henry Hub": ["henry hub", "ng="],
    # ── US indices ───────────────────────────────────────────────
    "SPX": ["s&p 500", "s&p500", "sp500"],
    "DJI": ["dow jones", "djia"],
    "NDX": ["nasdaq", "nasdaq 100"],
    "RUT": ["russell 2000", "small cap index"],
    # ── European indices & macro ─────────────────────────────────
    "FTSE": ["ftse", "ftse 100", "london stock exchange"],
    "DAX": ["dax", "german index", "xetra"],
    "CAC": ["cac 40", "paris bourse"],
    "STOXX": ["euro stoxx", "stoxx 50", "stoxx 600"],
    "ECB": ["ecb", "european central bank", "lagarde"],
    # ── Asian indices & macro ────────────────────────────────────
    "Nikkei": ["nikkei", "nikkei 225", "topix", "tokyo stock"],
    "HangSeng": ["hang seng", "hsi", "hong kong stock"],
    "Shanghai": ["shanghai", "sse composite", "a-shares"],
    "KOSPI": ["kospi", "korea stock"],
    "Sensex": ["sensex", "bse", "nifty", "nse india"],
    "ASX": ["asx", "asx 200", "australian stock"],
    # ── Central banks / macro ────────────────────────────────────
    "Fed": ["federal reserve", "fed rate", "fomc", "powell", "fed funds"],
    "BOJ": ["bank of japan", "boj", "kuroda", "ueda"],
    "BOE": ["bank of england", "boe", "bailey"],
    "PBOC": ["pboc", "people's bank of china", "chinese central bank"],
    # ── Forex ────────────────────────────────────────────────────
    "EURUSD": ["eur/usd", "eurusd", "euro dollar"],
    "GBPUSD": ["gbp/usd", "gbpusd", "cable", "pound dollar"],
    "USDJPY": ["usd/jpy", "usdjpy", "dollar yen"],
    "DXY": ["dollar index", "dxy", "us dollar index"],
    # ── Crypto ───────────────────────────────────────────────────
    "BTC": ["btc", "bitcoin"],
    "ETH": ["eth", "ethereum"],
    "XRP": ["xrp", "ripple"],
}


def score_sentiment(text: str) -> Optional[float]:
    """
    Return compound sentiment score in [-1, 1]. Uses VADER; when FinBERT is
    available (optional: pip install transformers torch), blends 0.5*VADER + 0.5*FinBERT.
    Returns None if VADER not installed.
    """
    if not text:
        return None
    vader_score: Optional[float] = None
    if _analyzer:
        scores = _analyzer.polarity_scores(text)
        vader_score = scores.get("compound")
    try:
        from app.services.sentiment.finbert import score_sentiment_finbert
        finbert_score = score_sentiment_finbert(text)
        if finbert_score is not None and vader_score is not None:
            return round(0.5 * vader_score + 0.5 * finbert_score, 4)
        if finbert_score is not None:
            return round(finbert_score, 4)
    except Exception:
        pass
    return vader_score


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
