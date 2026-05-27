"""
Tests for backtesting metrics computation.
"""
import pytest
from app.services.backtesting.metrics import compute_metrics, _bucket, _sharpe, _max_drawdown


class TestBucket:
    def test_below_40(self):
        assert _bucket(30) == "<40"
        assert _bucket(0) == "<40"
        assert _bucket(39) == "<40"

    def test_40_to_49(self):
        assert _bucket(40) == "40-49"
        assert _bucket(49) == "40-49"

    def test_50_to_59(self):
        assert _bucket(50) == "50-59"
        assert _bucket(59) == "50-59"

    def test_60_to_69(self):
        assert _bucket(60) == "60-69"

    def test_70_to_79(self):
        assert _bucket(70) == "70-79"

    def test_80_plus(self):
        assert _bucket(80) == "80+"
        assert _bucket(100) == "80+"


class TestSharpe:
    def test_returns_none_for_single_trade(self):
        assert _sharpe([1.5]) is None

    def test_returns_none_for_empty(self):
        assert _sharpe([]) is None

    def test_positive_for_positive_returns(self):
        result = _sharpe([1.0, 1.5, 2.0, 1.2, 0.8])
        assert result is not None
        assert result > 0

    def test_negative_for_negative_returns(self):
        result = _sharpe([-1.0, -1.0, -1.0, -1.0, -1.0])
        # All same negative → std=0 → None
        assert result is None

    def test_zero_std_returns_none(self):
        assert _sharpe([1.0, 1.0, 1.0, 1.0]) is None


class TestMaxDrawdown:
    def test_zero_for_empty(self):
        assert _max_drawdown([]) == 0.0

    def test_no_drawdown_on_monotone_positive(self):
        assert _max_drawdown([1.0, 1.0, 1.0]) == 0.0

    def test_simple_drawdown(self):
        # cumulative: 1 → 2 → 1 → 0 → peak=2, trough=0, dd=2
        dd = _max_drawdown([1.0, 1.0, -1.0, -1.0])
        assert dd == pytest.approx(2.0, abs=0.01)

    def test_partial_recovery(self):
        # cumulative: 2 → 1 (dd=1), then 3 (new peak), then 1 (dd=2)
        dd = _max_drawdown([2.0, -1.0, 2.0, -2.0])
        assert dd == pytest.approx(2.0, abs=0.01)


class TestComputeMetrics:
    def _make_trade(self, outcome, pnl_r, confidence=60, rr=2.0, symbol="AAPL", direction="long"):
        return {
            "outcome": outcome,
            "pnl_r": pnl_r,
            "confidence_score": confidence,
            "risk_reward": rr,
            "symbol": symbol,
            "direction": direction,
        }

    def test_empty_trades_returns_error(self):
        result = compute_metrics([])
        assert "error" in result

    def test_basic_win_rate(self):
        trades = [
            self._make_trade("hit_tp", 2.0),
            self._make_trade("hit_tp", 2.0),
            self._make_trade("hit_sl", -1.0),
            self._make_trade("hit_sl", -1.0),
        ]
        result = compute_metrics(trades)
        assert result["win_rate"] == pytest.approx(0.5, abs=0.001)

    def test_all_wins(self):
        trades = [self._make_trade("hit_tp", 2.0) for _ in range(5)]
        result = compute_metrics(trades)
        assert result["win_rate"] == pytest.approx(1.0)
        assert result["wins"] == 5
        assert result["losses"] == 0

    def test_all_losses(self):
        trades = [self._make_trade("hit_sl", -1.0) for _ in range(4)]
        result = compute_metrics(trades)
        assert result["win_rate"] == pytest.approx(0.0)
        assert result["losses"] == 4

    def test_expired_counted_in_totals(self):
        trades = [
            self._make_trade("hit_tp", 2.0),
            self._make_trade("expired", 0.5),
        ]
        result = compute_metrics(trades)
        assert result["total_trades"] == 2
        assert result["expired"] == 1

    def test_avg_pnl_r_calculated(self):
        trades = [
            self._make_trade("hit_tp", 2.0),
            self._make_trade("hit_sl", -1.0),
        ]
        result = compute_metrics(trades)
        assert result["avg_pnl_r"] == pytest.approx(0.5, abs=0.01)

    def test_by_confidence_bucket_populated(self):
        trades = [
            self._make_trade("hit_tp", 2.0, confidence=75),
            self._make_trade("hit_sl", -1.0, confidence=75),
        ]
        result = compute_metrics(trades)
        buckets = result["by_confidence_bucket"]
        assert "70-79" in buckets
        assert buckets["70-79"]["total"] == 2
        assert buckets["70-79"]["wins"] == 1

    def test_by_direction_populated(self):
        trades = [
            self._make_trade("hit_tp", 2.0, direction="long"),
            self._make_trade("hit_sl", -1.0, direction="short"),
        ]
        result = compute_metrics(trades)
        assert "long" in result["by_direction"]
        assert "short" in result["by_direction"]

    def test_by_direction_full_quality_fields(self):
        """All new short-diagnostic fields must be present and correct."""
        trades = [
            self._make_trade("hit_tp", 2.0, direction="short"),
            self._make_trade("hit_tp", 3.0, direction="short"),
            self._make_trade("hit_sl", -1.0, direction="short"),
        ]
        result = compute_metrics(trades)
        sd = result["by_direction"]["short"]
        assert sd["total"] == 3
        assert sd["wins"] == 2
        assert sd["losses"] == 1
        assert sd["win_rate"] == pytest.approx(2 / 3, abs=0.001)
        assert sd["resolved_win_rate"] == pytest.approx(2 / 3, abs=0.001)
        assert sd["hit_tp_rate"] == pytest.approx(2 / 3, abs=0.001)
        assert sd["hit_sl_rate"] == pytest.approx(1 / 3, abs=0.001)
        assert sd["avg_pnl_r"] == pytest.approx((2.0 + 3.0 - 1.0) / 3, abs=0.001)
        assert sd["avg_win_r"] == pytest.approx((2.0 + 3.0) / 2, abs=0.001)
        assert sd["avg_loss_r"] == pytest.approx(-1.0, abs=0.001)

    def test_by_direction_sl_heavy_short_cohort(self):
        """SL-heavy short cohort: 70% SL rate, negative expectancy flagged correctly."""
        trades = [
            self._make_trade("hit_sl", -1.0, direction="short"),
            self._make_trade("hit_sl", -1.0, direction="short"),
            self._make_trade("hit_sl", -1.0, direction="short"),
            self._make_trade("hit_sl", -1.0, direction="short"),
            self._make_trade("hit_sl", -1.0, direction="short"),
            self._make_trade("hit_sl", -1.0, direction="short"),
            self._make_trade("hit_sl", -1.0, direction="short"),
            self._make_trade("hit_tp", 2.0, direction="short"),
            self._make_trade("hit_tp", 2.0, direction="short"),
            self._make_trade("hit_tp", 2.0, direction="short"),
        ]
        result = compute_metrics(trades)
        sd = result["by_direction"]["short"]
        assert sd["hit_sl_rate"] == pytest.approx(0.7, abs=0.001)
        assert sd["hit_tp_rate"] == pytest.approx(0.3, abs=0.001)
        assert sd["avg_pnl_r"] < 0  # negative expectancy

    def test_by_direction_internal_accumulators_not_in_output(self):
        """Accumulator keys must be stripped from the output dict."""
        trades = [self._make_trade("hit_tp", 2.0, direction="short")]
        result = compute_metrics(trades)
        sd = result["by_direction"]["short"]
        for k in ("pnl_r_sum", "win_pnl_r_sum", "loss_pnl_r_sum", "pnl_r_count"):
            assert k not in sd, f"accumulator key '{k}' leaked into output"

    def test_by_direction_avg_win_loss_none_when_no_trades(self):
        """avg_win_r is None when there are no wins; avg_loss_r is None when no losses."""
        trades = [self._make_trade("hit_sl", -1.0, direction="short")]
        result = compute_metrics(trades)
        sd = result["by_direction"]["short"]
        assert sd["avg_win_r"] is None
        assert sd["avg_loss_r"] == pytest.approx(-1.0, abs=0.001)

    def test_by_direction_long_and_short_independent(self):
        """Long and short stats must not bleed into each other."""
        trades = [
            self._make_trade("hit_tp", 3.0, direction="long"),
            self._make_trade("hit_tp", 3.0, direction="long"),
            self._make_trade("hit_sl", -1.0, direction="short"),
            self._make_trade("hit_sl", -1.0, direction="short"),
        ]
        result = compute_metrics(trades)
        ld = result["by_direction"]["long"]
        sd = result["by_direction"]["short"]
        assert ld["win_rate"] == pytest.approx(1.0)
        assert sd["win_rate"] == pytest.approx(0.0)
        assert ld["avg_pnl_r"] == pytest.approx(3.0, abs=0.001)
        assert sd["avg_pnl_r"] == pytest.approx(-1.0, abs=0.001)

    def test_by_symbol_populated(self):
        trades = [
            self._make_trade("hit_tp", 2.0, symbol="AAPL"),
            self._make_trade("hit_sl", -1.0, symbol="TSLA"),
        ]
        result = compute_metrics(trades)
        assert "AAPL" in result["by_symbol"]
        assert "TSLA" in result["by_symbol"]

    def test_total_r_is_sum_of_pnl(self):
        trades = [
            self._make_trade("hit_tp", 2.0),
            self._make_trade("hit_sl", -1.0),
            self._make_trade("hit_tp", 1.5),
        ]
        result = compute_metrics(trades)
        assert result["total_r"] == pytest.approx(2.5, abs=0.01)

    def test_sharpe_none_for_single_trade(self):
        trades = [self._make_trade("hit_tp", 2.0)]
        result = compute_metrics(trades)
        assert result["sharpe_ratio"] is None

    def test_max_drawdown_present(self):
        trades = [
            self._make_trade("hit_tp", 2.0),
            self._make_trade("hit_sl", -1.0),
            self._make_trade("hit_sl", -1.0),
        ]
        result = compute_metrics(trades)
        assert result["max_drawdown_r"] >= 0
