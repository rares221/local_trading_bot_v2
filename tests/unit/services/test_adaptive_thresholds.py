"""
Tests for adaptive confidence threshold computation.
"""
import pytest
import app.services.adaptive_thresholds as adaptive_module
from app.services.adaptive_thresholds import (
    compute_adaptive_threshold,
    update_adaptive_thresholds,
    get_adaptive_threshold,
    is_adaptive_enabled,
    TARGET_HIT_RATE,
)


@pytest.fixture(autouse=True)
def reset_thresholds():
    """Reset module-level cached thresholds before each test."""
    adaptive_module._adaptive_thresholds = {}
    yield
    adaptive_module._adaptive_thresholds = {}


def _bucket(n: int, hit_rate: float) -> dict:
    return {"n": n, "hit_rate": hit_rate}


class TestComputeAdaptiveThreshold:
    def test_no_data_returns_default_35(self):
        result = compute_adaptive_threshold({})
        assert result == 35

    def test_all_buckets_below_target_returns_35(self):
        by_bucket = {
            "low": _bucket(10, 0.40),
            "medium": _bucket(10, 0.45),
            "high": _bucket(10, 0.50),
            "very_high": _bucket(10, 0.54),
        }
        result = compute_adaptive_threshold(by_bucket, target_hit_rate=0.55)
        assert result == 35

    def test_qualifying_bucket_returns_its_midpoint(self):
        by_bucket = {
            "high": _bucket(10, 0.60),
        }
        result = compute_adaptive_threshold(by_bucket, target_hit_rate=0.55)
        assert result == 70  # "high" midpoint

    def test_returns_lowest_qualifying_bucket(self):
        # Both medium (50) and high (70) qualify; should return medium's midpoint (50)
        by_bucket = {
            "medium": _bucket(10, 0.60),
            "high": _bucket(10, 0.70),
        }
        result = compute_adaptive_threshold(by_bucket, target_hit_rate=0.55)
        assert result == 50

    def test_insufficient_sample_size_disqualifies_bucket(self):
        # n=4 < 5 minimum sample → not qualifying
        by_bucket = {
            "high": _bucket(4, 0.80),
        }
        result = compute_adaptive_threshold(by_bucket, target_hit_rate=0.55)
        assert result == 35

    def test_bucket_with_none_hit_rate_disqualifies(self):
        by_bucket = {
            "high": {"n": 10, "hit_rate": None},
        }
        result = compute_adaptive_threshold(by_bucket, target_hit_rate=0.55)
        assert result == 35

    def test_very_high_bucket_qualifies(self):
        by_bucket = {
            "very_high": _bucket(20, 0.80),
        }
        result = compute_adaptive_threshold(by_bucket, target_hit_rate=0.55)
        assert result == 90  # "very_high" midpoint


class TestUpdateAdaptiveThresholds:
    def test_sets_all_three_regimes(self):
        by_bucket = {"high": _bucket(10, 0.65)}
        update_adaptive_thresholds(by_bucket)
        assert get_adaptive_threshold("risk_on") is not None
        assert get_adaptive_threshold("cautious") is not None
        assert get_adaptive_threshold("risk_off") is not None

    def test_risk_on_is_5_lower_than_cautious(self):
        by_bucket = {"high": _bucket(10, 0.65)}
        update_adaptive_thresholds(by_bucket)
        risk_on = get_adaptive_threshold("risk_on")
        cautious = get_adaptive_threshold("cautious")
        # risk_on = max(30, threshold - 5)
        expected = max(30, cautious - 5)
        assert risk_on == expected

    def test_risk_off_is_5_higher_than_cautious_capped_at_50(self):
        by_bucket = {"high": _bucket(10, 0.65)}
        update_adaptive_thresholds(by_bucket)
        cautious = get_adaptive_threshold("cautious")
        risk_off = get_adaptive_threshold("risk_off")
        expected = min(50, cautious + 5)
        assert risk_off == expected


class TestGetAdaptiveThreshold:
    def test_before_update_returns_none(self):
        assert get_adaptive_threshold("cautious") is None

    def test_after_update_returns_int(self):
        update_adaptive_thresholds({"high": _bucket(10, 0.65)})
        val = get_adaptive_threshold("cautious")
        assert isinstance(val, int)

    def test_unknown_regime_returns_none(self):
        update_adaptive_thresholds({"high": _bucket(10, 0.65)})
        assert get_adaptive_threshold("unknown_regime") is None


class TestIsAdaptiveEnabled:
    def test_false_before_update(self):
        assert is_adaptive_enabled() is False

    def test_true_after_update(self):
        update_adaptive_thresholds({"high": _bucket(10, 0.65)})
        assert is_adaptive_enabled() is True
