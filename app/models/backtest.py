"""
BacktestRun and BacktestTrade models for historical replay and outcome simulation.
"""
import json
from datetime import datetime, date
from typing import Optional
from uuid import uuid4

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    # JSON array of symbols tested
    symbols_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    risk_tier: Mapped[str] = mapped_column(String(32), nullable=False, default="moderate")
    min_confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=40)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")  # running | completed | failed
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Aggregate metrics (populated when status=completed)
    total_signals: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_trades: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    win_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sharpe_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_rr_realized: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_rr_projected: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # JSON dict with per-confidence-bucket win rates, benchmark comparison, etc.
    metrics_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    trades: Mapped[list["BacktestTrade"]] = relationship(
        "BacktestTrade", back_populates="run", cascade="all, delete-orphan"
    )

    @property
    def symbols(self) -> list[str]:
        try:
            return json.loads(self.symbols_json)
        except Exception:
            return []

    @property
    def metrics(self) -> dict:
        try:
            return json.loads(self.metrics_json) if self.metrics_json else {}
        except Exception:
            return {}

    __table_args__ = (Index("ix_backtest_runs_status", "status"),)


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("backtest_runs.id"), nullable=False, index=True)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_category: Mapped[str] = mapped_column(String(32), nullable=False, default="stocks")
    direction: Mapped[str] = mapped_column(String(8), nullable=False, default="long")
    risk_tier: Mapped[str] = mapped_column(String(32), nullable=False)

    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit: Mapped[float] = mapped_column(Float, nullable=False)

    confidence_score: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_reward: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Outcome fields (populated by simulator)
    outcome: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # hit_tp | hit_sl | expired
    closing_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    outcome_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    bars_held: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # P&L in R-multiples: +RR for hit_tp, -1.0 for hit_sl, 0 for expired (at closing price)
    pnl_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Snapshot context at entry
    volatility_regime: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    trend_bias: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    atr_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Market regime at entry date (bull | bear | high_vol | crisis | neutral)
    market_regime: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    run: Mapped["BacktestRun"] = relationship("BacktestRun", back_populates="trades")

    __table_args__ = (
        Index("ix_backtest_trades_run_symbol", "run_id", "symbol"),
    )
