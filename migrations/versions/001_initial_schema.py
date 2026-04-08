"""Initial schema with TimescaleDB hypertables

Revision ID: 001
Revises:
Create Date: 2026-04-06

Creates all tables from scratch for a fresh PostgreSQL / TimescaleDB deployment.
For SQLite (dev), schema is managed inline in database.py — Alembic only runs
against PostgreSQL.

TimescaleDB hypertables are created for time-series heavy tables:
  - feed_items       (partitioned by fetched_at)
  - market_analyses  (partitioned by as_of)
  - daily_briefings  (partitioned by created_at)
  - recommendations  (partitioned by created_at)
  - active_trade_levels (partitioned by created_at)
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _try_create_hypertable(table: str, time_column: str) -> None:
    """
    Create TimescaleDB hypertable using a SAVEPOINT so that a failure does NOT
    abort the surrounding transaction (plain PostgreSQL without TimescaleDB extension).
    """
    sp = f"sp_hypertable_{table}"
    try:
        op.execute(sa.text(f"SAVEPOINT {sp}"))
        op.execute(
            sa.text(
                f"SELECT create_hypertable('{table}', '{time_column}', "
                f"if_not_exists => TRUE, migrate_data => TRUE)"
            )
        )
        op.execute(sa.text(f"RELEASE SAVEPOINT {sp}"))
    except Exception:
        # TimescaleDB not available — roll back only to the savepoint, keep transaction alive
        op.execute(sa.text(f"ROLLBACK TO SAVEPOINT {sp}"))


def upgrade() -> None:
    # ------------------------------------------------------------------
    # feed_items — time-series: one row per RSS article
    # ------------------------------------------------------------------
    op.create_table(
        "feed_items",
        sa.Column("id", sa.String(512), primary_key=True),
        sa.Column("title", sa.String(1024), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("link", sa.String(2048), nullable=False),
        sa.Column("published", sa.DateTime(timezone=True), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("source", sa.String(256), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("keywords", sa.JSON, nullable=True),
        sa.Column("is_processed", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("sentiment_score", sa.Float, nullable=True),
        sa.Column("asset_tags", sa.JSON, nullable=True),
    )
    op.create_index("ix_feed_items_category", "feed_items", ["category"])
    op.create_index("ix_feed_items_fetched_at", "feed_items", ["fetched_at"])
    op.create_index("ix_feed_items_published", "feed_items", ["published"])
    _try_create_hypertable("feed_items", "fetched_at")

    # ------------------------------------------------------------------
    # market_analyses — time-series: one row per analysis run
    # ------------------------------------------------------------------
    op.create_table(
        "market_analyses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sources_used", sa.JSON, nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("payload", sa.JSON, nullable=True),
    )
    op.create_index("ix_market_analyses_as_of", "market_analyses", ["as_of"])
    _try_create_hypertable("market_analyses", "as_of")

    # ------------------------------------------------------------------
    # daily_briefings — time-series: one row per briefing run
    # ------------------------------------------------------------------
    op.create_table(
        "daily_briefings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_text", sa.Text, nullable=False),
        sa.Column("payload", sa.JSON, nullable=True),
    )
    op.create_index("ix_daily_briefings_created_at", "daily_briefings", ["created_at"])
    _try_create_hypertable("daily_briefings", "created_at")

    # ------------------------------------------------------------------
    # recommendations — time-series: one row per recommendation
    # ------------------------------------------------------------------
    op.create_table(
        "recommendations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("batch_id", sa.String(32), nullable=False),
        sa.Column("asset_category", sa.String(32), nullable=False),
        sa.Column("asset_id", sa.String(32), nullable=False),
        sa.Column("buy_in_price", sa.Float, nullable=False),
        sa.Column("stop_loss", sa.Float, nullable=False),
        sa.Column("take_profit", sa.Float, nullable=False),
        sa.Column("expiry_date", sa.Date, nullable=True),
        sa.Column("rationale_ref", sa.String(256), nullable=True),
        sa.Column("rationale_text", sa.Text, nullable=True),
        sa.Column("risk_tier", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False, server_default="long"),
        sa.Column("confidence_score", sa.Integer, nullable=True),
        sa.Column("risk_reward", sa.Float, nullable=True),
        sa.Column("volatility_notes", sa.Text, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("suggested_risk_pct", sa.Float, nullable=True),
        sa.Column("instrument_type", sa.String(16), nullable=True),
        sa.Column("min_confidence_gate", sa.Integer, nullable=True),
        sa.Column("shadow_b_threshold", sa.Integer, nullable=True),
        sa.Column("shadow_b_pass", sa.Boolean, nullable=True),
    )
    op.create_index("ix_recommendations_batch_id", "recommendations", ["batch_id"])
    op.create_index("ix_recommendations_batch_status", "recommendations", ["batch_id", "status"])
    op.create_index("ix_recommendations_created_at", "recommendations", ["created_at"])
    _try_create_hypertable("recommendations", "created_at")

    # ------------------------------------------------------------------
    # active_trades — state table (not a hypertable)
    # ------------------------------------------------------------------
    op.create_table(
        "active_trades",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("recommendation_id", sa.String(36), sa.ForeignKey("recommendations.id"), nullable=False),
        sa.Column("user_taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_sl", sa.Float, nullable=False),
        sa.Column("current_tp", sa.Float, nullable=False),
        sa.Column("suggested_sl", sa.Float, nullable=True),
        sa.Column("suggested_tp", sa.Float, nullable=True),
        sa.Column("suggested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expiry", sa.Date, nullable=True),
        sa.Column("last_updated", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trade_phase", sa.String(24), nullable=False, server_default="open"),
        sa.Column("breakeven_hit", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("highest_price", sa.Float, nullable=True),
        sa.Column("lowest_price", sa.Float, nullable=True),
        sa.Column("signal_flip_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("outcome", sa.String(24), nullable=True),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closing_price", sa.Float, nullable=True),
        sa.Column("strategy_leg", sa.String(32), nullable=False, server_default="vanilla"),
        sa.Column("position_fraction", sa.Float, nullable=False, server_default="1.0"),
        sa.UniqueConstraint("recommendation_id", "strategy_leg", name="uq_active_trades_rec_strategy_leg"),
    )
    op.create_index("ix_active_trades_recommendation_id", "active_trades", ["recommendation_id"])

    # ------------------------------------------------------------------
    # active_trade_levels — time-series: SL/TP history per trade
    # ------------------------------------------------------------------
    op.create_table(
        "active_trade_levels",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("active_trade_id", sa.String(36), sa.ForeignKey("active_trades.id"), nullable=False),
        sa.Column("sl", sa.Float, nullable=False),
        sa.Column("tp", sa.Float, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
    )
    op.create_index("ix_active_trade_levels_active_trade_id", "active_trade_levels", ["active_trade_id"])
    op.create_index("ix_active_trade_levels_created_at", "active_trade_levels", ["created_at"])
    _try_create_hypertable("active_trade_levels", "created_at")


def downgrade() -> None:
    op.drop_table("active_trade_levels")
    op.drop_table("active_trades")
    op.drop_table("recommendations")
    op.drop_table("daily_briefings")
    op.drop_table("market_analyses")
    op.drop_table("feed_items")
