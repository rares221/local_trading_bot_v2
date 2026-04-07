"""
Tests for ticker detection and company name resolution.
"""
import pytest
from unittest.mock import patch
from app.services.sentiment.ticker_detection import (
    is_likely_ticker,
    detect_tickers,
    _extract_proper_nouns,
    COMPANY_TO_TICKER,
    _DEFAULT_BLOCKLIST,
)


class TestIsLikelyTicker:
    def test_valid_ticker_returns_true(self):
        # AAPL is not in blocklist and is a valid format
        assert is_likely_ticker("AAPL") is True

    def test_blocklisted_token_returns_false(self):
        assert is_likely_ticker("USA") is False
        assert is_likely_ticker("CEO") is False
        assert is_likely_ticker("ETF") is False

    def test_too_short_returns_false(self):
        assert is_likely_ticker("A") is False

    def test_too_long_returns_false(self):
        assert is_likely_ticker("TOOLNG") is False

    def test_lowercase_returns_false(self):
        assert is_likely_ticker("aapl") is False

    def test_mixed_case_returns_false(self):
        assert is_likely_ticker("Aapl") is False

    def test_contains_digit_returns_false(self):
        assert is_likely_ticker("AA1L") is False

    def test_empty_returns_false(self):
        assert is_likely_ticker("") is False

    def test_two_letter_not_in_blocklist(self):
        # GB is not in blocklist
        assert is_likely_ticker("GB") is True

    def test_five_letter_valid(self):
        assert is_likely_ticker("TSLA") is True


class TestDetectTickers:
    def test_hardcoded_company_name_apple(self):
        tickers = detect_tickers("Apple reports record revenue in Q4")
        assert "AAPL" in tickers

    def test_hardcoded_company_name_tesla(self):
        tickers = detect_tickers("Tesla cuts prices in Europe")
        assert "TSLA" in tickers

    def test_hardcoded_company_name_nvidia(self):
        tickers = detect_tickers("Nvidia earnings beat expectations")
        assert "NVDA" in tickers

    def test_all_caps_ticker_detected(self):
        # MSFT is a valid ticker not in blocklist
        tickers = detect_tickers("MSFT stock rises after strong quarterly results")
        assert "MSFT" in tickers

    def test_blocklisted_token_not_in_tickers(self):
        tickers = detect_tickers("The CEO of a major US company resigned")
        assert "CEO" not in tickers
        assert "USA" not in tickers

    def test_empty_text_returns_empty(self):
        assert detect_tickers("") == []

    def test_whitespace_text_returns_empty(self):
        assert detect_tickers("   ") == []

    def test_no_duplicates_in_result(self):
        tickers = detect_tickers("Apple AAPL stock surges; Apple beats estimates")
        assert len([t for t in tickers if t == "AAPL"]) == 1

    def test_multiple_companies_detected(self):
        tickers = detect_tickers("Apple and Microsoft report earnings this week")
        assert "AAPL" in tickers
        assert "MSFT" in tickers


class TestExtractProperNouns:
    def test_two_word_proper_noun(self):
        nouns = _extract_proper_nouns("Goldman Sachs reports record profits")
        assert "goldman sachs" in nouns

    def test_three_word_proper_noun(self):
        nouns = _extract_proper_nouns("Bank of America misses estimates")
        assert "bank of america" in nouns

    def test_single_word_ignored(self):
        # Single words below 4 chars or just one word are not multi-word proper nouns
        nouns = _extract_proper_nouns("Apple beats estimates")
        # "Apple" alone might match but must be >= 4 chars; "Apple" = 5 chars but is 1 word
        # The pattern requires 2+ words, so Apple alone won't appear
        assert "apple" not in nouns

    def test_empty_string_returns_empty(self):
        assert _extract_proper_nouns("") == []

    def test_no_duplicates(self):
        nouns = _extract_proper_nouns("Goldman Sachs report. Goldman Sachs gains.")
        assert len([n for n in nouns if n == "goldman sachs"]) == 1
