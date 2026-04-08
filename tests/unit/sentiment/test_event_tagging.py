"""
Tests for event type tagging from feed text.
"""
import pytest
from app.services.sentiment.event_tagging import tag_event_types


class TestTagEventTypes:
    def test_earnings_keyword(self):
        tags = tag_event_types("Apple beats Q3 earnings estimates, EPS exceeds guidance")
        assert "earnings" in tags

    def test_fed_keyword_fomc(self):
        tags = tag_event_types("FOMC meeting minutes signal rate hike ahead")
        assert "fed" in tags

    def test_fed_keyword_powell(self):
        tags = tag_event_types("Powell hints at rate cut in upcoming Fed meeting")
        assert "fed" in tags

    def test_geopolitics_war(self):
        tags = tag_event_types("War escalates in Eastern Europe amid sanctions")
        assert "geopolitics" in tags

    def test_geopolitics_tariff(self):
        tags = tag_event_types("New tariff on Chinese goods announced in trade war")
        assert "geopolitics" in tags

    def test_macro_cpi(self):
        tags = tag_event_types("CPI inflation rises to 3.5% above expectations")
        assert "macro" in tags

    def test_macro_nfp(self):
        tags = tag_event_types("Jobs report shows NFP adds 200k positions")
        assert "macro" in tags

    def test_multiple_event_types(self):
        tags = tag_event_types(
            "Q2 earnings beat as FOMC signals rate cut; CPI data also released today"
        )
        assert "earnings" in tags
        assert "fed" in tags
        assert "macro" in tags

    def test_empty_string_returns_empty(self):
        assert tag_event_types("") == []

    def test_no_match_returns_empty(self):
        tags = tag_event_types("The sun rose over the mountains this morning.")
        assert tags == []

    def test_guidance_triggers_earnings(self):
        tags = tag_event_types("Management lowers full-year revenue guidance")
        assert "earnings" in tags

    def test_election_triggers_geopolitics(self):
        tags = tag_event_types("Presidential election results cause market volatility")
        assert "geopolitics" in tags
