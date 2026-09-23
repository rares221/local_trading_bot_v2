"""Add armed_shorts table

Revision ID: 009
Revises: 008
Create Date: 2026-09-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "armed_shorts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("family", sa.String(16), nullable=False),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("armed_date", sa.Date, nullable=False),
        sa.Column("days_since_armed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_armed_shorts_symbol_status", "armed_shorts", ["symbol", "status"])


def downgrade() -> None:
    op.drop_index("ix_armed_shorts_symbol_status", table_name="armed_shorts")
    op.drop_table("armed_shorts")
