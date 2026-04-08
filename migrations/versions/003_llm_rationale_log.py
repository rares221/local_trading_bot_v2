"""Add llm_rationale_logs table for LLM feedback loop

Revision ID: 003
Revises: 002
Create Date: 2026-04-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_rationale_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),

        # Links
        sa.Column("recommendation_id", sa.String(36),
                  sa.ForeignKey("recommendations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("active_trade_id", sa.String(36),
                  sa.ForeignKey("active_trades.id", ondelete="SET NULL"), nullable=True),

        # Asset context
        sa.Column("asset_id", sa.String(32), nullable=False),
        sa.Column("asset_category", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("horizon", sa.String(8), nullable=False, server_default="near"),
        sa.Column("horizon_days", sa.Integer, nullable=False, server_default="14"),

        # LLM metadata
        sa.Column("provider_id", sa.String(32), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False, server_default="rationale_v1"),

        # Input / Output
        sa.Column("context_snapshot", sa.Text, nullable=False),
        sa.Column("rationale_text", sa.Text, nullable=False),

        # Validation
        sa.Column("validation_flags", sa.Text, nullable=False, server_default="[]"),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),

        # News cross-reference
        sa.Column("news_verified", sa.Boolean, nullable=True),
        sa.Column("news_verification_detail", sa.Text, nullable=True),

        # Market metrics verification
        sa.Column("metrics_at_entry", sa.Text, nullable=True),
        sa.Column("metrics_verification", sa.Text, nullable=True),
        sa.Column("metrics_accuracy_score", sa.Float, nullable=True),

        # Outcome
        sa.Column("outcome", sa.String(24), nullable=True),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closing_price", sa.Float, nullable=True),
        sa.Column("pnl_r", sa.Float, nullable=True),

        # Scoring
        sa.Column("quality_score", sa.Integer, nullable=True),
        sa.Column("hallucination_rate", sa.Float, nullable=True),
    )

    op.create_index("ix_llm_rl_rec", "llm_rationale_logs", ["recommendation_id"])
    op.create_index("ix_llm_rl_trade", "llm_rationale_logs", ["active_trade_id"])
    op.create_index("ix_llm_rl_asset_direction", "llm_rationale_logs", ["asset_id", "direction", "outcome"])
    op.create_index("ix_llm_rl_category_horizon", "llm_rationale_logs", ["asset_category", "horizon", "outcome"])
    op.create_index("ix_llm_rl_provider", "llm_rationale_logs", ["provider_id", "model", "outcome"])
    op.create_index("ix_llm_rl_created", "llm_rationale_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("llm_rationale_logs")
