"""
Hallucination detection helpers for the LLM feedback loop.

verify_news_references  — P11: cross-ref cited news vs feed_items DB + context headlines
verify_metrics_claims   — P12: extract numeric indicator claims, compare to metrics_at_entry
"""
from __future__ import annotations

import difflib
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


# ── Regex patterns ────────────────────────────────────────────────────────────

_NEWS_REPORTING = re.compile(
    r"\b(?:announced|reported|said|confirmed|revealed|stated|disclosed|"
    r"following|after\s+\w+(?:\s+\w+)?\s+(?:announced|reported|said))\b",
    re.IGNORECASE,
)

_RSI_CLAIM   = re.compile(r"\bRSI\b.*?(?:at|of|is|around|near|stands?\s+at)\s+(\d{1,3}(?:\.\d{1,2})?)", re.IGNORECASE)
_ADX_CLAIM   = re.compile(r"\bADX\b.*?(?:at|of|is|above|below|around|near|stands?\s+at)\s+(\d{1,3}(?:\.\d{1,2})?)", re.IGNORECASE)
_ATR_CLAIM   = re.compile(r"\bATR\b.*?(?:at|of|is|around|near|stands?\s+at)\s+(\d{1,3}(?:\.\d{1,4})?)(?:\s*%)?", re.IGNORECASE)
_MACD_BULL   = re.compile(r"\bMACD\b.{0,60}?\b(positive|bullish|above\s+(?:zero|signal)|histogram\s+(?:positive|rising))\b", re.IGNORECASE)
_MACD_BEAR   = re.compile(r"\bMACD\b.{0,60}?\b(negative|bearish|below\s+(?:zero|signal)|histogram\s+(?:negative|falling))\b", re.IGNORECASE)

_NUMERIC_TOL = 0.15   # ±15% tolerance for indicator value comparison
_FUZZY_CTX   = 0.35   # fuzzy match threshold vs context headlines (lax)
_FUZZY_DB    = 0.40   # fuzzy match threshold vs DB feed titles


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fuzzy_match(text: str, candidates: list[str], threshold: float) -> bool:
    t = text.lower()
    for c in candidates:
        if difflib.SequenceMatcher(None, t, c.lower()).ratio() >= threshold:
            return True
    return False


def _within_tol(actual: float, cited: float) -> bool:
    if actual == 0:
        return abs(cited) < 1.0
    return abs(cited - actual) / abs(actual) <= _NUMERIC_TOL


# ── P11: News cross-reference ─────────────────────────────────────────────────

async def verify_news_references(
    rationale_text: str,
    context_headlines: list[str],
    session: AsyncSession,
    asset_id: str,
    hours_back: int = 72,
) -> dict[str, Any]:
    """
    Extract news citations from rationale and verify against:
      1. Headlines provided in context (should always match if LLM behaved).
      2. Recent feed_items in DB for this asset (detects training-memory leakage).

    Returns:
      {
        "verified": bool,           # True = all citations found in context headlines
        "citations_found": int,     # how many citing sentences were extracted
        "missing_from_context": int,# cited but not in context headlines
        "found_in_db_only": int,    # exists in DB but wasn't in context (context leak)
        "pure_hallucinations": int, # nowhere — pure invention
        "detail": [...]             # per-citation breakdown
      }
    """
    from datetime import datetime, timezone, timedelta
    from app.models.feed_item import FeedItem

    sentences = re.split(r"(?<=[.!?])\s+", rationale_text)
    citing_sentences = [s for s in sentences if _NEWS_REPORTING.search(s)]

    if not citing_sentences:
        return {
            "verified": True,
            "citations_found": 0,
            "missing_from_context": 0,
            "found_in_db_only": 0,
            "pure_hallucinations": 0,
            "detail": [],
        }

    # Fetch recent feed_items for this asset from DB
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    q = (
        select(FeedItem.title)
        .where(
            FeedItem.fetched_at >= cutoff,
            FeedItem.asset_tags.contains([asset_id]),
        )
        .limit(200)
    )
    db_titles: list[str] = [row[0] for row in (await session.execute(q)).all() if row[0]]

    detail: list[dict] = []
    missing_from_context = 0
    found_in_db_only = 0
    pure_hallucinations = 0

    for sentence in citing_sentences:
        in_context = _fuzzy_match(sentence, context_headlines, _FUZZY_CTX)
        in_db      = _fuzzy_match(sentence, db_titles, _FUZZY_DB) if db_titles else False

        if in_context:
            status = "verified"
        elif in_db:
            status = "found_in_db_only"
            found_in_db_only += 1
            missing_from_context += 1
        else:
            status = "hallucinated"
            pure_hallucinations += 1
            missing_from_context += 1

        detail.append({"sentence": sentence[:200], "status": status})

    verified = missing_from_context == 0

    return {
        "verified": verified,
        "citations_found": len(citing_sentences),
        "missing_from_context": missing_from_context,
        "found_in_db_only": found_in_db_only,
        "pure_hallucinations": pure_hallucinations,
        "detail": detail,
    }


# ── P12: Metrics claims verification ─────────────────────────────────────────

def verify_metrics_claims(
    rationale_text: str,
    metrics_at_entry: dict[str, Any],
) -> dict[str, Any]:
    """
    Extract numeric indicator claims from rationale_text and compare against
    actual values captured at trade entry (metrics_at_entry).

    Returns:
      {
        "claims": [{"indicator", "cited", "actual", "delta_pct", "is_hallucination"}],
        "accuracy_score": float,   # 0.0–1.0
        "hallucinated_indicators": [str],
      }
    """
    if not metrics_at_entry:
        return {"claims": [], "accuracy_score": None, "hallucinated_indicators": []}

    checks: list[tuple[str, re.Pattern, str]] = [
        ("rsi",     _RSI_CLAIM, "rsi"),
        ("adx",     _ADX_CLAIM, "adx"),
        ("atr_pct", _ATR_CLAIM, "atr_pct"),
    ]

    claims: list[dict] = []
    hallucinated: list[str] = []

    for label, pattern, key in checks:
        actual = metrics_at_entry.get(key)
        if actual is None:
            continue
        m = pattern.search(rationale_text)
        if not m:
            continue
        try:
            cited = float(m.group(1))
        except (ValueError, IndexError):
            continue

        actual_f = float(actual)
        ok = _within_tol(actual_f, cited)
        delta = abs(cited - actual_f) / max(abs(actual_f), 0.001)

        claims.append({
            "indicator": label,
            "cited": cited,
            "actual": actual_f,
            "delta_pct": round(delta * 100, 1),
            "is_hallucination": not ok,
        })
        if not ok:
            hallucinated.append(label)

    # MACD direction verification (qualitative)
    macd_hist = metrics_at_entry.get("macd_histogram")
    if macd_hist is not None:
        is_positive = float(macd_hist) > 0
        cited_bull = bool(_MACD_BULL.search(rationale_text))
        cited_bear = bool(_MACD_BEAR.search(rationale_text))
        if cited_bull or cited_bear:
            ok = (cited_bull and is_positive) or (cited_bear and not is_positive)
            claims.append({
                "indicator": "macd_direction",
                "cited": "bullish" if cited_bull else "bearish",
                "actual": "bullish" if is_positive else "bearish",
                "delta_pct": 0.0 if ok else 100.0,
                "is_hallucination": not ok,
            })
            if not ok:
                hallucinated.append("macd_direction")

    if not claims:
        return {"claims": [], "accuracy_score": None, "hallucinated_indicators": []}

    correct = sum(1 for c in claims if not c["is_hallucination"])
    accuracy_score = round(correct / len(claims), 3)

    return {
        "claims": claims,
        "accuracy_score": accuracy_score,
        "hallucinated_indicators": hallucinated,
    }
