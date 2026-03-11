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
    current_sl: Mapped[float] = mapped_column(Float, nullable=False)
    current_tp: Mapped[float] = mapped_column(Float, nullable=False)
    expiry: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    recommendation: Mapped["Recommendation"] = relationship("Recommendation", back_populates="active_trade")
