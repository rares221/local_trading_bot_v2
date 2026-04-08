"""
Live Trading DB models — 4 tables, independent from existing batch pipeline tables.

Tables:
  live_news_items      — RSS articles ingested by the stream engine (7-day TTL)
  live_analysis_runs   — Each LLM analysis run (button press or auto-trigger)
  live_recommendations — Individual recommendations per analysis run
  live_trade_updates   — Auto-refresh updates for the selected trade
"""
from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base


class LiveNewsItem(Base):
    __tablename__ = "live_news_items"

    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    link: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    published: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    asset_tags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # JSON list
    sentiment_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    __table_args__ = (
        Index("ix_live_news_fetched_at", "fetched_at"),
        Index("ix_live_news_source", "source"),
    )


class LiveAnalysisRun(Base):
    __tablename__ = "live_analysis_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # manual_button | pre_market_warmup | news_refresh | ticker_watch
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="manual_button")
    ticker_context: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    market_session: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    llm_prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    llm_response_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    estimated_cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    raw_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    context_snapshot_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parsed_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    recommendations: Mapped[list["LiveRecommendation"]] = relationship(
        "LiveRecommendation", back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_live_analysis_runs_created_at", "created_at"),
    )


class LiveRecommendation(Base):
    __tablename__ = "live_recommendations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("live_analysis_runs.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # stocks | commodities | etfs | fx | crypto | indices
    asset_category: Mapped[str] = mapped_column(String(32), nullable=False)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False, default="long")
    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Math-based SL/TP (ATR + support/resistance, same logic as main engine)
    stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # LLM-estimated SL/TP (may differ from math; LLM adjusts based on news/context)
    llm_stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    llm_take_profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    risk_reward: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # scalp | intraday | swing  (determined by LLM based on ATR%, volatility, session)
    timeframe: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    catalysts: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON list
    # regular | pre_market_warmup | after_hours | closed | open_24h
    market_session: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    run: Mapped["LiveAnalysisRun"] = relationship("LiveAnalysisRun", back_populates="recommendations")

    __table_args__ = (
        Index("ix_live_recommendations_run_category", "run_id", "asset_category"),
    )


class LiveTradeUpdate(Base):
    __tablename__ = "live_trade_updates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    trigger_headline: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence_before: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    confidence_after: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    direction_flip: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suggested_sl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    suggested_tp: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    assessment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # hold | tighten_sl | exit | add
    action_advised: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        Index("ix_live_trade_updates_created_at", "created_at"),
    )
