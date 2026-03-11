"""
Market analysis report cache. Phase 1 output: one row per run (or overwrite latest).
"""
from datetime import datetime
from sqlalchemy import DateTime, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.core.base import Base


class MarketAnalysis(Base):
    __tablename__ = "market_analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sources_used: Mapped[dict] = mapped_column(JSON, nullable=True)  # data lineage
    summary: Mapped[str] = mapped_column(Text, nullable=True)  # human-readable
    payload: Mapped[dict] = mapped_column(JSON, nullable=True)  # full report for Phase 2
