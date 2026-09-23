"""ArmedShortRow — persists a shorts-engine "armed, waiting for confirmation" state across
daily batch runs and container restarts (Phase 2 two-stage entry, see
docs/superpowers/plans/2026-09-18-shorts-engine.md §5). The backtest/replay path uses an
equivalent in-memory dict local to its day-loop instead — this table is live-only."""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base


class ArmedShortRow(Base):
    __tablename__ = "armed_shorts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    family: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    armed_date: Mapped[date] = mapped_column(Date, nullable=False)
    days_since_armed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # One active (pending) arm per symbol at a time — enforced in code (see
        # shorts_engine_io.py), this index just makes the "any pending arm for this symbol?"
        # lookup cheap.
        Index("ix_armed_shorts_symbol_status", "symbol", "status"),
    )
