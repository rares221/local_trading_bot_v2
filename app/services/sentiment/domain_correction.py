"""
Domain-aware sentiment correction for macro-linked assets using keyword matching.

Keyword matching is deterministic, fast, offline, and sufficient for the narrow
vocabulary of macro asset classes (energy supply disruptions, safe-haven metals,
FX rate direction). No external LLM call is required.

Called at aggregation time (aggregate.py) after computing weighted VADER averages.
Raw feed_items scores are never modified.

Flow:
    aggregate.py collects article titles per tag
    → apply_domain_correction() called for macro-linked tags
    → keywords matched against article titles (case-insensitive substring)
    → bullish/bearish hit ratio → domain score in [-1, 1]
    → corrected_avg = raw_avg * 0.60 + domain_score * 0.40
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Blend weight: fraction of keyword score applied vs raw VADER avg.
# 0.40 = 40% keyword domain score, 60% raw VADER — conservative to start.
CORRECTION_BLEND_WEIGHT: float = 0.40

# Minimum number of articles required to trigger keyword classification.
# Below this the article mix is too sparse to derive a reliable narrative.
MIN_ARTICLES_FOR_CORRECTION: int = 3

# Maps asset tag → asset class key used to look up KEYWORD_REGISTRY.
# Only tags present here will trigger domain correction.
MACRO_LINKED_TAGS: dict[str, str] = {
    # Energy
    "oil":    "energy",
    "WTI":    "energy",
    "Brent":  "energy",
    "gas":    "energy",
    "crude":  "energy",
    "CL=F":   "energy",
    "BZ=F":   "energy",
    "NG=F":   "energy",
    # Precious metals
    "gold":   "precious_metals",
    "silver": "precious_metals",
    "XAUUSD": "precious_metals",
    "GC=F":   "precious_metals",
    "SI=F":   "precious_metals",
    # USD-bullish FX
    "USDJPY=X": "usd_bullish_fx",
    "USDCAD=X": "usd_bullish_fx",
    "DXY":      "usd_bullish_fx",
    # Risk-currency FX
    "EURUSD=X": "risk_currency_fx",
    "GBPUSD=X": "risk_currency_fx",
    "AUDUSD=X": "risk_currency_fx",
}

# Keyword registry: per asset class, lists of bullish and bearish keyword phrases.
# Matching is case-insensitive substring — "sanction" matches "new sanctions imposed".
# Extend these lists to improve coverage; no code changes elsewhere required.
KEYWORD_REGISTRY: dict[str, dict[str, list[str]]] = {
    "energy": {
        "bullish": [
            "blockade", "sanction", "production cut", "output cut", "opec cut",
            "supply disruption", "supply shock", "supply outage",
            "refinery fire", "refinery outage", "refinery attack",
            "pipeline attack", "pipeline disruption", "pipeline explosion",
            "tanker attack", "tanker seizure", "tanker u-turn", "tanker divert",
            "shortage", "hurricane", "tropical storm",
            "conflict", "war", "strike", "inventory draw", "drawdown",
        ],
        "bearish": [
            "oversupply", "production increase", "output increase", "supply increase",
            "opec+ increase", "opec hike", "inventory build", "stockpile build",
            "demand falls", "demand slump", "demand weakness", "demand drop",
            "recession", "slowdown", "economic contraction", "glut", "surplus",
        ],
    },
    "precious_metals": {
        "bullish": [
            "war", "conflict", "crisis", "geopolit", "invasion",
            "inflation", "stagflation", "safe-haven", "safe haven",
            "sanction", "default", "bank failure", "bank run",
            "dollar weakness", "dollar falls", "usd weakens",
            "rate cut", "easing", "dovish",
            "recession fear", "uncertainty", "risk-off",
        ],
        "bearish": [
            "rate hike", "hawkish", "tightening", "fed hike",
            "dollar strength", "dollar rises", "usd strengthens",
            "yields rise", "risk-on", "strong jobs", "nfp beat",
            "inflation falls", "disinflation",
        ],
    },
    "usd_bullish_fx": {
        "bullish": [
            "rate hike", "hawkish", "tightening", "fed hike",
            "strong jobs", "nfp beat", "cpi beat",
            "dollar strength", "usd strengthens",
        ],
        "bearish": [
            "rate cut", "dovish", "easing", "fed cut",
            "weak jobs", "nfp miss", "recession",
            "dollar weakness", "usd weakens",
        ],
    },
    "risk_currency_fx": {
        "bullish": [
            "rate hike", "hawkish", "ecb hike", "boe hike", "rba hike",
            "strong growth", "gdp beat", "trade surplus", "risk-on",
            "china growth", "global growth",
        ],
        "bearish": [
            "rate cut", "dovish", "ecb cut", "boe cut", "rba cut",
            "recession", "weak growth", "gdp miss", "trade war",
            "risk-off", "slowdown", "china slowdown",
        ],
    },
}


def apply_domain_correction(
    tag: str,
    raw_avg: float,
    titles: list[str],
    blend_weight: float = CORRECTION_BLEND_WEIGHT,
) -> tuple[float, bool]:
    """
    Apply domain-aware sentiment correction to a raw VADER weighted average.

    Scans article titles for bullish/bearish keywords specific to the asset class.
    Falls back gracefully to the raw average if the tag is not macro-linked,
    has too few articles, or no keyword matches are found.

    Args:
        tag: Asset tag (e.g. "oil", "XAUUSD", "EURUSD=X")
        raw_avg: Weighted VADER sentiment average from aggregate.py
        titles: List of article title+description strings contributing to this tag
        blend_weight: Fraction of keyword score to blend in (default 0.40)

    Returns:
        (corrected_avg, correction_applied)
        Returns (raw_avg, False) if no correction was applied.
    """
    asset_class = MACRO_LINKED_TAGS.get(tag)
    if asset_class is None:
        return raw_avg, False

    if len(titles) < MIN_ARTICLES_FOR_CORRECTION:
        logger.debug(
            "[DOMAIN_CORRECTION] tag=%s skipped — only %d articles (min %d)",
            tag, len(titles), MIN_ARTICLES_FOR_CORRECTION,
        )
        return raw_avg, False

    keyword_score = _keyword_classify(asset_class, titles)
    if keyword_score is None:
        logger.debug("[DOMAIN_CORRECTION] tag=%s no keyword matches — passthrough", tag)
        return raw_avg, False

    corrected = raw_avg * (1.0 - blend_weight) + keyword_score * blend_weight
    logger.info(
        "[DOMAIN_CORRECTION] tag=%s raw=%.3f keyword_score=%.3f blended=%.3f",
        tag, raw_avg, keyword_score, corrected,
    )
    return corrected, True


def _keyword_classify(asset_class: str, titles: list[str]) -> Optional[float]:
    """
    Scan article titles for bullish/bearish keyword matches for an asset class.

    Each title is lowercased and checked for every keyword phrase (substring match).
    A single title can contribute multiple hits if it contains multiple keywords.

    Score formula:
        (bullish_hits - bearish_hits) / (bullish_hits + bearish_hits)

    This produces a value in [-1, 1]:
        +1.0  all matches are bullish keywords
        -1.0  all matches are bearish keywords
         0.0  equal bullish and bearish matches
        None  no keyword matched at all → caller treats as no correction

    Args:
        asset_class: Key into KEYWORD_REGISTRY (e.g. "energy", "precious_metals")
        titles: List of article title+description strings

    Returns:
        Float in [-1, 1] or None if no keywords matched.
    """
    registry = KEYWORD_REGISTRY.get(asset_class)
    if not registry:
        return None

    bullish_kws = registry.get("bullish", [])
    bearish_kws = registry.get("bearish", [])

    bullish_hits = 0
    bearish_hits = 0

    for title in titles:
        lower = title.lower()
        for kw in bullish_kws:
            if kw in lower:
                bullish_hits += 1
        for kw in bearish_kws:
            if kw in lower:
                bearish_hits += 1

    total = bullish_hits + bearish_hits
    if total == 0:
        return None

    return (bullish_hits - bearish_hits) / total
