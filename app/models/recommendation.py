"""
Recommendation and ActiveTrade models. Aligned with PLAN_MODE and TRADING_APP_PLAN §3.
"""
from datetime import datetime, date
from typing import Optional
from uuid import uuid4

from sqlalchemy import DateTime, Date, String, Float, ForeignKey, Text, Index
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
    volatility_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending | taken | skipped

    active_trade: Mapped[Optional["ActiveTrade"]] = relationship("ActiveTrade", back_populates="recommendation", uselist=False)

    __table_args__ = (Index("ix_recommendations_batch_status", "batch_id", "status"),)


class ActiveTrade(Base):
    __tablename__ = "active_trades"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    recommendation_id: Mapped[str] = mapped_column(String(36), ForeignKey("recommendations.id"), nullable=False, unique=True)
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

    recommendation: Mapped["Recommendation"] = relationship("Recommendation", back_populates="active_trade")
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
