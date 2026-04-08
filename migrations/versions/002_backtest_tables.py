"""Add backtest_runs and backtest_trades tables

Revision ID: 002
Revises: 001
Create Date: 2026-04-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("symbols_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("risk_tier", sa.String(32), nullable=False, server_default="moderate"),
        sa.Column("min_confidence", sa.Integer, nullable=False, server_default="40"),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("total_signals", sa.Integer, nullable=True),
        sa.Column("total_trades", sa.Integer, nullable=True),
        sa.Column("win_rate", sa.Float, nullable=True),
        sa.Column("sharpe_ratio", sa.Float, nullable=True),
        sa.Column("max_drawdown", sa.Float, nullable=True),
        sa.Column("avg_rr_realized", sa.Float, nullable=True),
        sa.Column("avg_rr_projected", sa.Float, nullable=True),
        sa.Column("metrics_json", sa.Text, nullable=True),
    )
    op.create_index("ix_backtest_runs_status", "backtest_runs", ["status"])

    op.create_table(
        "backtest_trades",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("backtest_runs.id"), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("asset_category", sa.String(32), nullable=False, server_default="stocks"),
        sa.Column("direction", sa.String(8), nullable=False, server_default="long"),
        sa.Column("risk_tier", sa.String(32), nullable=False),
        sa.Column("entry_date", sa.Date, nullable=False),
        sa.Column("entry_price", sa.Float, nullable=False),
        sa.Column("stop_loss", sa.Float, nullable=False),
        sa.Column("take_profit", sa.Float, nullable=False),
        sa.Column("confidence_score", sa.Integer, nullable=False),
        sa.Column("risk_reward", sa.Float, nullable=True),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("closing_price", sa.Float, nullable=True),
        sa.Column("outcome_date", sa.Date, nullable=True),
        sa.Column("bars_held", sa.Integer, nullable=True),
        sa.Column("pnl_r", sa.Float, nullable=True),
        sa.Column("volatility_regime", sa.String(16), nullable=True),
        sa.Column("trend_bias", sa.String(16), nullable=True),
        sa.Column("atr_pct", sa.Float, nullable=True),
    )
    op.create_index("ix_backtest_trades_run_id", "backtest_trades", ["run_id"])
    op.create_index("ix_backtest_trades_run_symbol", "backtest_trades", ["run_id", "symbol"])


def downgrade() -> None:
    op.drop_table("backtest_trades")
    op.drop_table("backtest_runs")
