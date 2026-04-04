"""
Recommendation and ActiveTrade models. Aligned with PLAN_MODE and TRADING_APP_PLAN §3.
"""
from datetime import datetime, date
from typing import Optional
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Date, Integer, String, Float, ForeignKey, Text, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.base import Base


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # e.g. calendar day YYYY-MM-DD
    asset_category: Mapped[str] = mapped_column(String(32), nullable=False)  # stocks | oil | commodities | forex
    asset_id: Mapped[str] = mapped_column(String(32), nullable=False)  # ticker or instrument
    buy_in_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit: Mapped[float] = mapped_column(Float, nullable=False)
    expiry_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    rationale_ref: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)  # analysis run id or report ref
    rationale_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Option B: LLM-generated "why and risk"
    risk_tier: Mapped[str] = mapped_column(String(32), nullable=False)  # conservative | moderate | aggressive
    direction: Mapped[str] = mapped_column(String(8), nullable=False, default="long")  # long | short
    confidence_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 0–100
    risk_reward: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # e.g. 2.5
    volatility_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending | taken | skipped
    suggested_risk_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # position sizing: suggested risk % of capital
    # Instrument type: cfd | invest | etf | spot (auto-set at batch time; editable later)
    instrument_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # A/B shadow: logged at batch time; live gate unchanged
    min_confidence_gate: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shadow_b_threshold: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shadow_b_pass: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    active_trades: Mapped[list["ActiveTrade"]] = relationship(
        "ActiveTrade", back_populates="recommendation"
    )

    __table_args__ = (Index("ix_recommendations_batch_status", "batch_id", "status"),)


class ActiveTrade(Base):
    __tablename__ = "active_trades"
    __table_args__ = (
        UniqueConstraint("recommendation_id", "strategy_leg", name="uq_active_trades_rec_strategy_leg"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    recommendation_id: Mapped[str] = mapped_column(String(36), ForeignKey("recommendations.id"), nullable=False, index=True)
    user_taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Original levels when user took the trade (never overwritten by update job)
    current_sl: Mapped[float] = mapped_column(Float, nullable=False)
    current_tp: Mapped[float] = mapped_column(Float, nullable=False)
    # Latest pipeline-suggested levels (from update job); user can compare with original
    suggested_sl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    suggested_tp: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    suggested_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expiry: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Trade management lifecycle: open -> partial_tp -> trailing -> closed
    trade_phase: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    breakeven_hit: Mapped[bool] = mapped_column(default=False)
    highest_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # for longs
    lowest_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)   # for shorts
    signal_flip_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Outcome logging: hit_tp | hit_sl | expired | manual (for calibration / Brier later)
    outcome: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    outcome_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    closing_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Price when outcome occurred
    # Execution plan: vanilla = one row; split strategies = multiple rows (same rec, different strategy_leg / TP).
    strategy_leg: Mapped[str] = mapped_column(String(32), nullable=False, default="vanilla")
    position_fraction: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    recommendation: Mapped["Recommendation"] = relationship("Recommendation", back_populates="active_trades")
    levels: Mapped[list["ActiveTradeLevel"]] = relationship(
        "ActiveTradeLevel", back_populates="active_trade", order_by="ActiveTradeLevel.created_at"
    )


class ActiveTradeLevel(Base):
    """History of SL/TP values per run (initial when taken + each scheduled update)."""
    __tablename__ = "active_trade_levels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    active_trade_id: Mapped[str] = mapped_column(String(36), ForeignKey("active_trades.id"), nullable=False, index=True)
    sl: Mapped[float] = mapped_column(Float, nullable=False)
    tp: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # 'initial' | 'scheduled_update'

    active_trade: Mapped["ActiveTrade"] = relationship("ActiveTrade", back_populates="levels")
