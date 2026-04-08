"""Add llm_stop_loss, llm_take_profit to live_recommendations

Revision ID: 005
Revises: 004
Create Date: 2026-04-08
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "live_recommendations",
        sa.Column("llm_stop_loss", sa.Float(), nullable=True),
    )
    op.add_column(
        "live_recommendations",
        sa.Column("llm_take_profit", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("live_recommendations", "llm_take_profit")
    op.drop_column("live_recommendations", "llm_stop_loss")
