"""Add walkforward_runs table

Revision ID: 008
Revises: 007
Create Date: 2026-04-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "walkforward_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("risk_tier", sa.String(32), nullable=False, server_default="moderate"),
        sa.Column("min_confidence", sa.Integer, nullable=False, server_default="40"),
        sa.Column("in_sample_days", sa.Integer, nullable=False, server_default="252"),
        sa.Column("out_sample_days", sa.Integer, nullable=False, server_default="63"),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("total_windows", sa.Integer, nullable=True),
        sa.Column("degradation_ratio", sa.Float, nullable=True),
        sa.Column("robustness", sa.String(32), nullable=True),
        sa.Column("result_json", sa.Text, nullable=True),
    )
    op.create_index("ix_walkforward_runs_status", "walkforward_runs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_walkforward_runs_status", table_name="walkforward_runs")
    op.drop_table("walkforward_runs")
