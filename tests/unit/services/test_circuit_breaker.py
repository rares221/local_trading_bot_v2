"""
Tests for the circuit breaker: normal / defensive / paused modes.
"""
import pytest
from datetime import datetime, timedelta, timezone
from app.services.circuit_breaker import compute_state, CircuitBreakerState


def _make_trade(outcome: str, hours_ago: float = 1.0, position_fraction: float = 1.0):
    """Helper: create a simple namespace object matching what compute_state expects."""
    class Trade:
        pass

    t = Trade()
    t.outcome = outcome
    t.outcome_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    t.position_fraction = position_fraction
    return t


class TestNormalMode:
    def test_no_trades_returns_normal(self):
        state = compute_state([])
        assert state.mode == "normal"

    def test_few_losses_returns_normal(self):
        trades = [_make_trade("hit_sl") for _ in range(2)]
        state = compute_state(trades, max_consecutive_losses=5, max_daily_loss_r=3.0)
        assert state.mode == "normal"

    def test_wins_only_returns_normal(self):
        trades = [_make_trade("hit_tp") for _ in range(10)]
        state = compute_state(trades)
        assert state.mode == "normal"


class TestDefensiveMode:
    def test_consecutive_losses_triggers_defensive(self):
        trades = [_make_trade("hit_sl") for _ in range(5)]
        state = compute_state(trades, max_consecutive_losses=5, max_daily_loss_r=10.0)
        assert state.mode == "defensive"

    def test_rolling_loss_triggers_defensive(self):
        # 3 x hit_sl within 24h = 3R loss; threshold=3R triggers defensive
        trades = [_make_trade("hit_sl", hours_ago=1) for _ in range(3)]
        state = compute_state(trades, max_consecutive_losses=100, max_daily_loss_r=3.0)
        assert state.mode == "defensive"

    def test_confidence_penalty_is_10_in_defensive(self):
        trades = [_make_trade("hit_sl") for _ in range(5)]
        state = compute_state(trades, max_consecutive_losses=5, max_daily_loss_r=10.0)
        assert state.confidence_penalty() == 10

    def test_sizing_multiplier_is_07_in_defensive(self):
        trades = [_make_trade("hit_sl") for _ in range(5)]
        state = compute_state(trades, max_consecutive_losses=5, max_daily_loss_r=10.0)
        assert state.sizing_multiplier() == pytest.approx(0.7)

    def test_75pct_drawdown_gate_triggers_defensive(self):
        # drawdown_gate_r=8 → 75%=6R triggers defensive
        # need 6R cumulative drawdown: 8 wins then 6 losses = peak at 8, trough at 2 → dd=6
        trades = [_make_trade("hit_tp", hours_ago=24)] * 8 + [_make_trade("hit_sl", hours_ago=0.5)] * 6
        state = compute_state(trades, max_consecutive_losses=100, max_daily_loss_r=100.0, drawdown_gate_r=8.0)
        assert state.mode in ("defensive", "paused")


class TestPausedMode:
    def test_many_consecutive_losses_triggers_paused(self):
        # pause threshold = max_consecutive_losses + 2 = 7
        trades = [_make_trade("hit_sl") for _ in range(7)]
        state = compute_state(trades, max_consecutive_losses=5, max_daily_loss_r=10.0)
        assert state.mode == "paused"

    def test_large_rolling_loss_triggers_paused(self):
        # pause_loss = max_daily_loss_r * 2.0 = 6R; 6 losses = 6R in 24h
        trades = [_make_trade("hit_sl", hours_ago=1) for _ in range(6)]
        state = compute_state(trades, max_consecutive_losses=100, max_daily_loss_r=3.0)
        assert state.mode == "paused"

    def test_is_paused_property(self):
        trades = [_make_trade("hit_sl") for _ in range(7)]
        state = compute_state(trades, max_consecutive_losses=5)
        assert state.is_paused is True
        assert state.is_defensive is False


class TestStreakSemantics:
    def test_hit_tp_resets_streak(self):
        # 4 losses, 1 win, 1 more loss — streak should be 1, not 5
        trades = (
            [_make_trade("hit_sl", hours_ago=5)] * 4
            + [_make_trade("hit_tp", hours_ago=3)]
            + [_make_trade("hit_sl", hours_ago=1)]
        )
        state = compute_state(trades, max_consecutive_losses=5, max_daily_loss_r=100.0)
        assert state.consecutive_losses == 1
        assert state.mode == "normal"

    def test_expired_is_neutral_does_not_reset_streak(self):
        # 3 losses, 2 expired — streak should still be 3
        trades = (
            [_make_trade("hit_sl", hours_ago=5)] * 3
            + [_make_trade("expired", hours_ago=2)] * 2
        )
        state = compute_state(trades, max_consecutive_losses=5, max_daily_loss_r=100.0)
        assert state.consecutive_losses == 3

    def test_old_losses_not_in_rolling_window(self):
        # Losses older than 24h should not count in rolling_loss_r
        old = [_make_trade("hit_sl", hours_ago=25) for _ in range(5)]
        state = compute_state(old, max_consecutive_losses=100, max_daily_loss_r=3.0)
        assert state.rolling_loss_r == pytest.approx(0.0)
        assert state.mode == "normal"


class TestCircuitBreakerStateProperties:
    def test_normal_confidence_penalty_is_zero(self):
        state = CircuitBreakerState(mode="normal")
        assert state.confidence_penalty() == 0

    def test_normal_sizing_multiplier_is_one(self):
        state = CircuitBreakerState(mode="normal")
        assert state.sizing_multiplier() == pytest.approx(1.0)

    def test_paused_is_paused_true(self):
        state = CircuitBreakerState(mode="paused")
        assert state.is_paused is True
        assert state.is_defensive is False
