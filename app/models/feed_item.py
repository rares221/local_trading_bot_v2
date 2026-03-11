"""
Feed item model. Reused from codeops-trading-app storage.FeedItem, extended with
sentiment_score and asset_tags for the new pipeline (TRADING_APP_PLAN Phase 1).
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, String, Text, Boolean, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.core.base import Base


class FeedItem(Base):
    __tablename__ = "feed_items"

    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    link: Mapped[str] = mapped_column(String(2048), nullable=False)
    published: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(256), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    keywords: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # list of str
    is_processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # New fields for Phase 1 pipeline (sentiment + asset tagging)
    sentiment_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    asset_tags: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # e.g. ["oil", "brent"]
