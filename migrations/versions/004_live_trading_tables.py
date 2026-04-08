"""Live Trading tables

Revision ID: 004
Revises: 003
Create Date: 2026-04-08

Creates 4 new tables for the Live Trading feature:
  live_news_items        — RSS stream articles (7-day TTL, hypertable)
  live_analysis_runs     — LLM analysis run metadata
  live_recommendations   — Per-run recommendations
  live_trade_updates     — Auto-refresh history for selected trade
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _try_create_hypertable(table: str, time_column: str) -> None:
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
        op.execute(sa.text(f"ROLLBACK TO SAVEPOINT {sp}"))


def upgrade() -> None:
    op.create_table(
        "live_news_items",
        sa.Column("id", sa.String(512), primary_key=True),
        sa.Column("title", sa.Text, nullable=True),
        sa.Column("link", sa.String(2048), nullable=True),
        sa.Column("published", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(256), nullable=True),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("asset_tags", sa.Text, nullable=True),
        sa.Column("sentiment_score", sa.Float, nullable=True),
    )
    op.create_index("ix_live_news_fetched_at", "live_news_items", ["fetched_at"])
    op.create_index("ix_live_news_source", "live_news_items", ["source"])
    _try_create_hypertable("live_news_items", "fetched_at")

    op.create_table(
        "live_analysis_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger", sa.String(32), nullable=False, server_default="manual_button"),
        sa.Column("ticker_context", sa.String(32), nullable=True),
        sa.Column("market_session", sa.String(32), nullable=True),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("llm_prompt_tokens", sa.Integer, nullable=True),
        sa.Column("llm_response_tokens", sa.Integer, nullable=True),
        sa.Column("estimated_cost_usd", sa.Float, nullable=True),
        sa.Column("raw_response", sa.Text, nullable=True),
        sa.Column("context_snapshot_json", sa.Text, nullable=True),
        sa.Column("parsed_ok", sa.Boolean, nullable=False, server_default="false"),
    )
    op.create_index("ix_live_analysis_runs_created_at", "live_analysis_runs", ["created_at"])

    op.create_table(
        "live_recommendations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("live_analysis_runs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("asset_category", sa.String(32), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False, server_default="long"),
        sa.Column("entry_price", sa.Float, nullable=True),
        sa.Column("stop_loss", sa.Float, nullable=True),
        sa.Column("take_profit", sa.Float, nullable=True),
        sa.Column("confidence", sa.Integer, nullable=True),
        sa.Column("risk_reward", sa.Float, nullable=True),
        sa.Column("rationale", sa.Text, nullable=True),
        sa.Column("timeframe", sa.String(16), nullable=True),
        sa.Column("catalysts", sa.Text, nullable=True),
        sa.Column("market_session", sa.String(32), nullable=True),
    )
    op.create_index(
        "ix_live_recommendations_run_category",
        "live_recommendations", ["run_id", "asset_category"]
    )

    op.create_table(
        "live_trade_updates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=False),
        sa.Column("trigger_headline", sa.Text, nullable=True),
        sa.Column("confidence_before", sa.Integer, nullable=True),
        sa.Column("confidence_after", sa.Integer, nullable=True),
        sa.Column("direction_flip", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("suggested_sl", sa.Float, nullable=True),
        sa.Column("suggested_tp", sa.Float, nullable=True),
        sa.Column("assessment", sa.Text, nullable=True),
        sa.Column("action_advised", sa.String(32), nullable=True),
    )
    op.create_index("ix_live_trade_updates_created_at", "live_trade_updates", ["created_at"])
    op.create_index("ix_live_trade_updates_ticker", "live_trade_updates", ["ticker"])


def downgrade() -> None:
    op.drop_table("live_trade_updates")
    op.drop_table("live_recommendations")
    op.drop_table("live_analysis_runs")
    op.drop_table("live_news_items")
