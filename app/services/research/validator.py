"""
LLM rationale validator — V1–V5, V7, V4a–V4d, V8.

Each rule returns a ValidationFlag (or None if the text passes).
Blocking rules trigger a retry; flag-only rules are logged but don't block.

V1   — length check (< 50 chars)
V2   — truncation heuristic (text doesn't end with sentence terminator)
V3   — direction alignment (rationale argues opposite direction)
V4a  — price hallucination (invented price level > 15% from any known price)
V4b  — indicator value hallucination (RSI/ADX/ATR cited value > 15% from actual)
V4c  — news/event hallucination (cited news not in provided headlines) [flag-only]
V4d  — temporal hallucination ("yesterday"/"last week" but headlines are older) [flag-only]
V5   — specificity score (< 2 technical terms)
V7   — gap advice word count (> 30 words)
V8   — horizon consistency (near/far timeframe mismatch)
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any


# ── Rule codes ────────────────────────────────────────────────────────────────

BLOCKING_RULES = {"V1", "V2", "V3", "V4a", "V4b", "V5", "V7", "V8"}
FLAG_ONLY_RULES = {"V4c", "V4d"}

MAX_RETRIES = 2

# Minimum character length for a valid rationale.
_MIN_RATIONALE_CHARS = 50

# Tolerance for numeric comparisons (±15%).
_NUMERIC_TOLERANCE = 0.15

# Technical terms that count toward specificity.
_TECHNICAL_TERMS = {
    "rsi", "macd", "atr", "support", "resistance", "vpoc", "volume profile",
    "adx", "bollinger", "fibonacci", "ema", "sma", "momentum", "breakout",
    "reversal", "sentiment", "value area", "vah", "val", "stochastic",
    "moving average", "trend", "divergence", "overbought", "oversold",
    "volatility", "vix", "yield", "dxy",
}

# Phrases that signal a bullish conclusion (used to catch direction conflicts on SHORT).
_BULLISH_CONCLUSION = re.compile(
    r"\b(likely to rise|expect(?:ed)? (?:an? )?(?:upward|upside|increase|gain)|"
    r"bullish outlook|price will increase|upside potential is (?:the )?(?:primary|main|dominant)|"
    r"poised to gain|set to rally|expect(?:ing)? (?:a )?rally)\b",
    re.IGNORECASE,
)

# Phrases that signal a bearish conclusion (used to catch direction conflicts on LONG).
_BEARISH_CONCLUSION = re.compile(
    r"\b(likely to fall|likely to decline|expect(?:ed)? (?:a )?(?:downward|decline|drop|decrease)|"
    r"bearish (?:pressure )?(?:is )?(?:primary|dominant|main)|price will (?:fall|decline|decrease)|"
    r"poised to fall|set to drop|downside (?:is )?(?:the )?(?:primary|main) risk)\b",
    re.IGNORECASE,
)

# Words that indicate the sentence is acknowledging the opposing view, not asserting it.
_CONCESSION_MARKERS = re.compile(
    r"\b(despite|although|even though|while|however|notwithstanding|"
    r"contrary to|in spite of)\b",
    re.IGNORECASE,
)

# Price-level context keywords: numbers near these words are candidate price levels.
_PRICE_CONTEXT = re.compile(
    r"(?:at|near|around|above|below|support|resistance|level|target|price|"
    r"entry|stop|vpoc|vah|val)\s*\$?\s*(\d{1,7}(?:\.\d{1,4})?)",
    re.IGNORECASE,
)

# Indicator value patterns.
_RSI_PATTERN = re.compile(
    r"\bRSI\b.*?(?:at|of|is|around|near|reading|stands?\s+at)\s+(\d{1,3}(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_ADX_PATTERN = re.compile(
    r"\bADX\b.*?(?:at|of|is|above|below|around|near|reading|stands?\s+at)\s+(\d{1,3}(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_ATR_PATTERN = re.compile(
    r"\bATR\b.*?(?:at|of|is|around|near|stands?\s+at)\s+(\d{1,3}(?:\.\d{1,4})?)(?:\s*%)?",
    re.IGNORECASE,
)

# News-reporting verbs that indicate the LLM is citing a specific event.
_NEWS_REPORTING = re.compile(
    r"\b(?:announced|reported|said|confirmed|revealed|stated|disclosed|"
    r"following|after\s+\w+(?:\s+\w+)?\s+(?:announced|reported|said))\b",
    re.IGNORECASE,
)

# Temporal expressions that imply recency.
_TEMPORAL_RECENT = re.compile(
    r"\b(yesterday|last\s+(?:night|week|monday|tuesday|wednesday|thursday|friday)|"
    r"this\s+morning|earlier\s+today|just\s+(?:announced|reported|released)|"
    r"hours?\s+ago|minutes?\s+ago)\b",
    re.IGNORECASE,
)

# Horizon-mismatched language.
_NEAR_HORIZON_VIOLATIONS = re.compile(
    r"\b(long.?term|over\s+the\s+(?:months|quarters|years?)|"
    r"fundamentally|structural(?:ly)?|in\s+the\s+coming\s+(?:months|quarters))\b",
    re.IGNORECASE,
)
_FAR_HORIZON_VIOLATIONS = re.compile(
    r"\b(this\s+week|by\s+(?:friday|monday|tuesday|wednesday|thursday)|"
    r"intraday|today['']?s?\s+(?:session|move|candle)|short.?term\s+(?:play|trade|move))\b",
    re.IGNORECASE,
)


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class ValidationFlag:
    code: str
    message: str           # logged internally
    retry_message: str     # injected into the next LLM call
    is_blocking: bool = True


@dataclass
class ValidationResult:
    passed: bool                          # True if no blocking flags
    flags: list[str] = field(default_factory=list)          # all flag codes triggered
    retry_message: str | None = None      # message for first blocking flag (for retry)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _within_tolerance(actual: float, cited: float, tol: float = _NUMERIC_TOLERANCE) -> bool:
    """True if cited value is within ±tol of actual (relative)."""
    if actual == 0:
        return abs(cited) < 1.0
    return abs(cited - actual) / abs(actual) <= tol


def _fuzzy_match(text: str, candidates: list[str], threshold: float = 0.5) -> bool:
    """True if text fuzzy-matches any candidate above threshold."""
    text_lower = text.lower()
    for c in candidates:
        ratio = difflib.SequenceMatcher(None, text_lower, c.lower()).ratio()
        if ratio >= threshold:
            return True
    return False


# ── Individual rule checks ────────────────────────────────────────────────────

def _check_v1_length(text: str) -> ValidationFlag | None:
    if len(text.strip()) < _MIN_RATIONALE_CHARS:
        return ValidationFlag(
            code="V1_too_short",
            message=f"Rationale too short ({len(text.strip())} chars < {_MIN_RATIONALE_CHARS})",
            retry_message=(
                "Response too short. Provide 2-4 complete sentences with specific "
                "technical justification for the trade direction and main risk."
            ),
        )
    return None


def _check_v2_truncation(text: str) -> ValidationFlag | None:
    """
    Heuristic: a complete rationale ends with a sentence terminator.
    If the text is long but ends mid-sentence, it was likely truncated.
    Only fires when text is substantial (> 80 chars) to avoid false positives on short answers.
    """
    stripped = text.strip()
    if len(stripped) > 80 and not stripped[-1] in ".!?\"'":
        return ValidationFlag(
            code="V2_truncated",
            message="Rationale appears truncated (no sentence terminator at end)",
            retry_message=(
                "Your previous response was cut off. Please provide the complete rationale "
                "in 2-4 sentences, ending with a full stop."
            ),
        )
    return None


def _check_v3_direction(text: str, context: dict[str, Any]) -> ValidationFlag | None:
    direction = (context.get("direction") or "long").lower()

    # Split into sentences for per-sentence analysis.
    sentences = re.split(r"(?<=[.!?])\s+", text)

    for sentence in sentences:
        # Skip sentences that are concessive ("despite bullish sentiment, we are short...")
        if _CONCESSION_MARKERS.search(sentence):
            continue

        if direction == "short" and _BULLISH_CONCLUSION.search(sentence):
            return ValidationFlag(
                code="V3_direction_conflict",
                message=f"SHORT rationale contains bullish conclusion: '{sentence[:120]}'",
                retry_message=(
                    "CRITICAL: Direction is SHORT. Your rationale argued the price will RISE — "
                    "that is the opposite of the required direction. "
                    "Rewrite to explain why the price will DECLINE."
                ),
            )
        if direction == "long" and _BEARISH_CONCLUSION.search(sentence):
            return ValidationFlag(
                code="V3_direction_conflict",
                message=f"LONG rationale contains bearish conclusion: '{sentence[:120]}'",
                retry_message=(
                    "CRITICAL: Direction is LONG. Your rationale argued the price will FALL — "
                    "that is the opposite of the required direction. "
                    "Rewrite to explain why the price will RISE."
                ),
            )
    return None


def _check_v4a_price_hallucination(text: str, context: dict[str, Any]) -> ValidationFlag | None:
    """
    Detect invented price levels. Only fires on numbers appearing in price-level context
    (preceded by 'support at', 'resistance near', '$', etc.) that don't match any known
    price from context. Skips if fewer than 2 known prices are available.
    """
    known_prices: list[float] = []
    for key in ("buy_in_price", "stop_loss", "take_profit", "vpoc", "vah", "val"):
        v = context.get(key)
        if v is not None:
            try:
                known_prices.append(float(v))
            except (TypeError, ValueError):
                pass

    if len(known_prices) < 2:
        return None  # not enough reference data to validate

    cited_prices: list[float] = []
    for m in _PRICE_CONTEXT.finditer(text):
        try:
            cited_prices.append(float(m.group(1)))
        except (ValueError, IndexError):
            pass

    if not cited_prices:
        return None

    # A cited price is hallucinated if it's > 15% away from ALL known prices.
    for cited in cited_prices:
        # Skip numbers that look like RSI/ADX/percent (< 200 absolute, < 10% of min known price)
        if cited < 1.0:
            continue
        min_known = min(known_prices)
        if cited < min_known * 0.05:
            continue  # looks like a % or small indicator value

        if not any(_within_tolerance(known, cited) for known in known_prices):
            return ValidationFlag(
                code="V4a_price_hallucination",
                message=f"Cited price {cited} not within 15% of any known price ({known_prices})",
                retry_message=(
                    "Do not invent price levels. Only reference prices explicitly provided "
                    "in the data (entry price, stop loss, take profit, VPOC, VAH, VAL)."
                ),
            )
    return None


def _check_v4b_indicator_hallucination(text: str, context: dict[str, Any]) -> ValidationFlag | None:
    """
    Detect hallucinated indicator values. Only fires when context has the actual value
    AND the LLM cites a specific numeric value that differs by > 15%.
    """
    checks = [
        ("rsi",           _RSI_PATTERN,  "RSI"),
        ("adx",           _ADX_PATTERN,  "ADX"),
        ("atr_pct",       _ATR_PATTERN,  "ATR"),
    ]

    for ctx_key, pattern, label in checks:
        actual = context.get(ctx_key)
        if actual is None:
            continue
        try:
            actual_f = float(actual)
        except (TypeError, ValueError):
            continue

        m = pattern.search(text)
        if not m:
            continue
        try:
            cited_f = float(m.group(1))
        except (ValueError, IndexError):
            continue

        if not _within_tolerance(actual_f, cited_f):
            return ValidationFlag(
                code="V4b_indicator_hallucination",
                message=(
                    f"{label} cited as {cited_f} but actual is {actual_f} "
                    f"(delta {abs(cited_f - actual_f) / max(abs(actual_f), 0.001):.0%})"
                ),
                retry_message=(
                    f"ACCURACY ERROR: You cited {label}={cited_f:.1f} but the actual value "
                    f"from the data is {label}={actual_f:.1f}. "
                    "Use only the exact indicator values provided. Do not approximate or invent values."
                ),
            )
    return None


def _check_v4c_news_hallucination(text: str, context: dict[str, Any]) -> ValidationFlag | None:
    """
    Flag-only: detect news citations not present in provided headlines.
    Only fires on sentences with reporting verbs (announced, reported, etc.).
    """
    headlines: list[str] = context.get("headlines") or []
    if not headlines:
        return None  # no headlines provided — can't verify

    sentences = re.split(r"(?<=[.!?])\s+", text)
    for sentence in sentences:
        if not _NEWS_REPORTING.search(sentence):
            continue
        # Check if this sentence fuzzy-matches any provided headline
        if not _fuzzy_match(sentence, headlines, threshold=0.35):
            return ValidationFlag(
                code="V4c_news_hallucination",
                message=f"News reference not found in provided headlines: '{sentence[:120]}'",
                retry_message="",  # flag-only: no retry
                is_blocking=False,
            )
    return None


def _check_v4d_temporal_hallucination(text: str, context: dict[str, Any]) -> ValidationFlag | None:
    """
    Flag-only: detect temporal expressions implying very recent events
    when the actual headlines are older than the implied timeframe.
    """
    if not _TEMPORAL_RECENT.search(text):
        return None

    headline_ages: list[float] = [
        age for age in (context.get("headline_ages") or [])
        if age is not None
    ]
    if not headline_ages:
        return None

    min_age_hours = min(headline_ages)

    # "yesterday" / "last night" → expect at least one headline < 48h
    # "last week" → expect at least one headline < 168h
    m = _TEMPORAL_RECENT.search(text)
    if not m:
        return None
    expr = m.group(1).lower()

    threshold = 48.0  # default: yesterday / this morning
    if "week" in expr:
        threshold = 168.0

    if min_age_hours > threshold:
        return ValidationFlag(
            code="V4d_temporal_hallucination",
            message=(
                f"Temporal expression '{expr}' used but most recent headline is "
                f"{min_age_hours:.0f}h old (threshold: {threshold:.0f}h)"
            ),
            retry_message="",  # flag-only: no retry
            is_blocking=False,
        )
    return None


def _check_v5_specificity(text: str) -> ValidationFlag | None:
    lower = text.lower()
    found = sum(1 for term in _TECHNICAL_TERMS if term in lower)
    if found < 2:
        return ValidationFlag(
            code="V5_too_generic",
            message=f"Rationale too generic — only {found} technical term(s) found",
            retry_message=(
                "Response too generic. Reference at least 2 specific technical indicators "
                "(e.g. RSI, MACD, ATR, VPOC, ADX, Bollinger Bands) with their values "
                "from the provided data."
            ),
        )
    return None


def _check_v7_gap_word_count(text: str) -> ValidationFlag | None:
    """Only applied to gap advice prompts (caller must opt in via prompt_type='gap')."""
    word_count = len(text.split())
    if word_count > 30:
        return ValidationFlag(
            code="V7_gap_too_long",
            message=f"Gap advice too long ({word_count} words > 30)",
            retry_message=(
                "Response too long. Maximum 25 words. Give ONE direct, specific recommendation only. "
                "No explanations, no caveats."
            ),
        )
    return None


def _check_v8_horizon_consistency(text: str, context: dict[str, Any]) -> ValidationFlag | None:
    """
    Detect timeframe language that contradicts the trade horizon.
    Near horizon (≤14d): should not contain long-term / months / structural language.
    Far horizon (>14d): should not contain intraday / this-week / by-Friday language.
    """
    horizon = (context.get("horizon") or "near").lower()
    horizon_days = context.get("expiry_days") or 14

    if horizon == "near":
        m = _NEAR_HORIZON_VIOLATIONS.search(text)
        if m:
            return ValidationFlag(
                code="V8_horizon_mismatch",
                message=f"Near-horizon rationale contains long-term language: '{m.group()}'",
                retry_message=(
                    f"This is a SHORT-TERM trade ({horizon_days} days). "
                    "Remove long-term / multi-month / fundamental language. "
                    "Focus on near-term price action, momentum, and upcoming events only."
                ),
            )
    elif horizon == "far":
        m = _FAR_HORIZON_VIOLATIONS.search(text)
        if m:
            return ValidationFlag(
                code="V8_horizon_mismatch",
                message=f"Far-horizon rationale contains short-term language: '{m.group()}'",
                retry_message=(
                    f"This is a LONGER-TERM trade ({horizon_days} days). "
                    "Remove intraday / this-week language. "
                    "Focus on trend, fundamentals, and macro cycle instead."
                ),
            )
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def validate_rationale(
    text: str,
    context: dict[str, Any],
    *,
    prompt_type: str = "rationale",  # "rationale" | "gap"
) -> ValidationResult:
    """
    Run all applicable validation rules against an LLM rationale.
    Returns a ValidationResult with passed=True if no blocking rules fired.

    prompt_type="gap" additionally runs V7 (word count cap).
    """
    if not text or not text.strip():
        if prompt_type == "gap":
            return ValidationResult(passed=False, flags=["V7_gap_too_long"], retry_message=(
                "Empty response. Give ONE direct, specific recommendation in maximum 25 words."
            ))
        return ValidationResult(
            passed=False,
            flags=["V1_too_short"],
            retry_message=(
                "Empty response. Provide 2-4 complete sentences explaining the trade direction and main risk."
            ),
        )

    # Gap advice: only enforce the word-count cap (V7). V1/V2/V3/V5 are designed
    # for full rationales and will produce false positives on 25-word responses.
    if prompt_type == "gap":
        flag = _check_v7_gap_word_count(text)
        triggered = [flag] if flag is not None else []
        all_flags = [f.code for f in triggered]
        return ValidationResult(
            passed=len(triggered) == 0,
            flags=all_flags,
            retry_message=triggered[0].retry_message if triggered else None,
        )

    checks = [
        _check_v1_length(text),
        _check_v2_truncation(text),
        _check_v3_direction(text, context),
        _check_v4a_price_hallucination(text, context),
        _check_v4b_indicator_hallucination(text, context),
        _check_v4c_news_hallucination(text, context),
        _check_v4d_temporal_hallucination(text, context),
        _check_v5_specificity(text),
        _check_v8_horizon_consistency(text, context),
    ]

    triggered = [flag for flag in checks if flag is not None]
    blocking = [flag for flag in triggered if flag.is_blocking and flag.code in BLOCKING_RULES]
    all_flags = [flag.code for flag in triggered]

    return ValidationResult(
        passed=len(blocking) == 0,
        flags=all_flags,
        retry_message=blocking[0].retry_message if blocking else None,
    )
