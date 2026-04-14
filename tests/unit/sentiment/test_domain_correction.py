"""
Unit tests for domain_correction.py (keyword-based approach).

No mocking required — keyword matching is a pure function.
All tests are synchronous.
"""
import pytest

from app.services.sentiment.domain_correction import (
    CORRECTION_BLEND_WEIGHT,
    KEYWORD_REGISTRY,
    MACRO_LINKED_TAGS,
    MIN_ARTICLES_FOR_CORRECTION,
    apply_domain_correction,
    _keyword_classify,
)


# ---------------------------------------------------------------------------
# _keyword_classify — pure function tests
# ---------------------------------------------------------------------------

def test_keyword_classify_all_bullish_returns_positive():
    """All titles containing bullish energy keywords → score near +1."""
    titles = [
        "Major blockade of oil shipping lane",
        "Refinery attack disrupts supply",
        "Tanker seizure causes shortage fears",
    ]
    score = _keyword_classify("energy", titles)
    assert score is not None
    assert score > 0


def test_keyword_classify_all_bearish_returns_negative():
    """All titles containing bearish energy keywords → score near -1."""
    titles = [
        "Global oil glut worsens as oversupply builds",
        "Inventory build signals demand weakness",
        "Recession fears drive demand slump",
    ]
    score = _keyword_classify("energy", titles)
    assert score is not None
    assert score < 0


def test_keyword_classify_no_matches_returns_none():
    """Titles with no matching keywords → None (no correction applied)."""
    titles = [
        "CEO resigns from board",
        "Earnings beat expectations",
        "Stock price moves sideways",
    ]
    score = _keyword_classify("energy", titles)
    assert score is None


def test_keyword_classify_mixed_returns_between_minus_one_and_one():
    """Mixed bullish and bearish keywords → score between -1 and 1."""
    titles = [
        "Sanctions imposed on oil producer",   # bullish energy
        "Demand slump weighs on crude",         # bearish energy
    ]
    score = _keyword_classify("energy", titles)
    assert score is not None
    assert -1.0 < score < 1.0


def test_keyword_classify_pure_bullish_score_is_plus_one():
    """Single bullish hit, zero bearish hits → score = +1.0."""
    titles = ["Blockade of the strait disrupts shipping"]
    score = _keyword_classify("energy", titles)
    assert score == pytest.approx(1.0)


def test_keyword_classify_pure_bearish_score_is_minus_one():
    """Single bearish hit, zero bullish hits → score = -1.0."""
    titles = ["Crude glut grows as oversupply persists"]
    score = _keyword_classify("energy", titles)
    assert score == pytest.approx(-1.0)


def test_keyword_classify_precious_metals_war_is_bullish():
    """War/crisis language should be bullish for precious metals."""
    titles = ["War escalates in eastern region", "Gold seen as safe haven amid crisis"]
    score = _keyword_classify("precious_metals", titles)
    assert score is not None
    assert score > 0


def test_keyword_classify_precious_metals_rate_hike_is_bearish():
    """Fed rate hike language should be bearish for precious metals."""
    titles = ["Fed signals hawkish stance", "Rate hike expected next meeting"]
    score = _keyword_classify("precious_metals", titles)
    assert score is not None
    assert score < 0


def test_keyword_classify_usd_bullish_fx_rate_hike_is_bullish():
    """Rate hike language is bullish for USD-bullish FX pairs."""
    titles = ["Fed delivers rate hike", "Hawkish tone lifts dollar"]
    score = _keyword_classify("usd_bullish_fx", titles)
    assert score is not None
    assert score > 0


def test_keyword_classify_unknown_asset_class_returns_none():
    """An asset class not in the registry → None."""
    score = _keyword_classify("nonexistent_class", ["some headline"])
    assert score is None


def test_keyword_classify_case_insensitive():
    """Keywords match regardless of title casing."""
    titles = ["BLOCKADE OF STRAIT CONFIRMED"]
    score = _keyword_classify("energy", titles)
    assert score is not None
    assert score > 0


# ---------------------------------------------------------------------------
# apply_domain_correction — integration of classify + blend
# ---------------------------------------------------------------------------

def test_non_macro_tag_returns_raw_unchanged():
    """Tags not in MACRO_LINKED_TAGS are never corrected."""
    raw = -0.4
    corrected, applied = apply_domain_correction("AAPL", raw, ["some headline"] * 5)
    assert applied is False
    assert corrected == raw


def test_too_few_articles_returns_raw_unchanged():
    """Correction skipped when fewer than MIN_ARTICLES_FOR_CORRECTION articles."""
    raw = -0.3
    titles = ["headline"] * (MIN_ARTICLES_FOR_CORRECTION - 1)
    corrected, applied = apply_domain_correction("oil", raw, titles)
    assert applied is False
    assert corrected == raw


def test_no_keyword_matches_returns_raw_unchanged():
    """If no keywords match, raw avg is returned and applied=False."""
    raw = -0.3
    titles = ["CEO resigns from board", "Earnings season begins", "Stocks rally"] * 3
    corrected, applied = apply_domain_correction("oil", raw, titles)
    assert applied is False
    assert corrected == raw


def test_bullish_keywords_raise_corrected_avg():
    """Bullish keyword match should pull corrected_avg above raw_avg."""
    raw = -0.5
    titles = ["Major oil blockade declared", "Refinery fire disrupts supply", "Tanker seizure"] * 3
    corrected, applied = apply_domain_correction("oil", raw, titles)
    assert applied is True
    assert corrected > raw


def test_bearish_keywords_lower_corrected_avg():
    """Bearish keyword match should pull corrected_avg below raw_avg."""
    raw = 0.5
    titles = ["Oil glut grows as oversupply builds", "Demand slump intensifies", "Inventory build"] * 3
    corrected, applied = apply_domain_correction("oil", raw, titles)
    assert applied is True
    assert corrected < raw


def test_blend_formula_exact_value():
    """corrected = raw * (1 - blend) + keyword_score * blend."""
    # Use all-bullish titles so keyword_score = +1.0
    raw = -0.4
    blend = CORRECTION_BLEND_WEIGHT
    expected_keyword_score = 1.0
    expected = raw * (1.0 - blend) + expected_keyword_score * blend

    titles = ["Blockade of the strait disrupts shipping"] * MIN_ARTICLES_FOR_CORRECTION
    corrected, applied = apply_domain_correction("oil", raw, titles)
    assert applied is True
    assert corrected == pytest.approx(expected)


def test_custom_blend_weight_applied():
    """Higher blend_weight moves corrected_avg further from raw_avg."""
    raw = -0.5
    titles = ["Blockade of the strait disrupts shipping"] * MIN_ARTICLES_FOR_CORRECTION

    corrected_low, _ = apply_domain_correction("oil", raw, titles, blend_weight=0.20)
    corrected_high, _ = apply_domain_correction("oil", raw, titles, blend_weight=0.80)

    assert abs(corrected_high - raw) > abs(corrected_low - raw)


def test_all_macro_tags_recognized():
    """Every tag in MACRO_LINKED_TAGS must not be skipped at the tag-lookup stage."""
    # Use titles that contain a keyword guaranteed to match each class so correction fires
    keyword_by_class = {
        "energy": "blockade",
        "precious_metals": "war",
        "usd_bullish_fx": "rate hike",
        "risk_currency_fx": "rate hike",
    }
    for tag, asset_class in MACRO_LINKED_TAGS.items():
        kw = keyword_by_class[asset_class]
        titles = [f"Headline about {kw}"] * MIN_ARTICLES_FOR_CORRECTION
        corrected, applied = apply_domain_correction(tag, 0.0, titles)
        assert applied is True, f"Expected correction to fire for tag={tag}"
