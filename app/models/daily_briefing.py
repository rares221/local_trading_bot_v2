"""
Daily briefing: one row per run (heartbeat-style checklist result).
"""
from datetime import datetime
from sqlalchemy import DateTime, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.core.base import Base


class DailyBriefing(Base):
    __tablename__ = "daily_briefings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)  # short summary
    payload: Mapped[dict] = mapped_column(JSON, nullable=True)     # checklist results
