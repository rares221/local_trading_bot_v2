"""WalkForwardRun model — persists async walk-forward analysis results."""
import json
from datetime import datetime, date
from typing import Optional
from uuid import uuid4

from sqlalchemy import Date, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base


class WalkForwardRun(Base):
    __tablename__ = "walkforward_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    risk_tier: Mapped[str] = mapped_column(String(32), nullable=False, default="moderate")
    min_confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=40)
    in_sample_days: Mapped[int] = mapped_column(Integer, nullable=False, default=252)
    out_sample_days: Mapped[int] = mapped_column(Integer, nullable=False, default=63)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    total_windows: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    degradation_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    robustness: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    @property
    def result(self) -> dict:
        try:
            return json.loads(self.result_json) if self.result_json else {}
        except Exception:
            return {}

    __table_args__ = (Index("ix_walkforward_runs_status", "status"),)
