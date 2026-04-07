"""
LLM rationale validator — V1–V5, V7.

Each rule returns a ValidationFlag (or None if the text passes).
Blocking rules trigger a retry; flag-only rules are logged but don't block.

V1  — length check (< 50 chars)
V2  — truncation heuristic (text doesn't end with sentence terminator)
V3  — direction alignment (rationale argues opposite direction)
V5  — specificity score (< 2 technical terms)
V7  — gap advice word count (> 30 words)

V4a–V4d (hallucination detection) and V8 (horizon consistency) are planned for P10.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ── Rule codes ────────────────────────────────────────────────────────────────

BLOCKING_RULES = {"V1", "V2", "V3", "V5", "V7"}
FLAG_ONLY_RULES: set[str] = set()   # V4c, V4d will go here in P10

MAX_RETRIES = 2

# Minimum character length for a valid rationale.
_MIN_RATIONALE_CHARS = 50

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
    corrective_instruction = context.get("_corrective_instruction")

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
        _check_v5_specificity(text),
    ]

    triggered = [flag for flag in checks if flag is not None]
    blocking = [flag for flag in triggered if flag.is_blocking and flag.code in BLOCKING_RULES]
    all_flags = [flag.code for flag in triggered]

    return ValidationResult(
        passed=len(blocking) == 0,
        flags=all_flags,
        retry_message=blocking[0].retry_message if blocking else None,
    )
