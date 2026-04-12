"""Add market_regime column to backtest_trades

Revision ID: 006
Revises: 005
Create Date: 2026-04-12
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "backtest_trades",
        sa.Column("market_regime", sa.String(16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("backtest_trades", "market_regime")
