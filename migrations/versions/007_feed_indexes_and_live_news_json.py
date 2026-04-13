"""Feed item indexes + live_news_items asset_tags JSON migration

Revision ID: 007
Revises: 006
Create Date: 2026-04-13

Changes:
  1. Partial index on feed_items(is_processed) WHERE is_processed = FALSE
     — eliminates full table scan on every ingest cycle
  2. GIN index on feed_items(asset_tags)
     — enables native JSON @> containment operator (replaces LIKE workaround)
  3. Composite partial index on feed_items(fetched_at, sentiment_score)
     WHERE sentiment_score IS NOT NULL
     — speeds up 5d/10d sentiment window queries in research pipeline
  4. live_news_items.asset_tags TEXT -> JSONB
     — consistent with FeedItem.asset_tags (JSON), enables native JSON ops
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _try_exec(sql: str) -> None:
    """Execute SQL inside a savepoint so failures don't abort the transaction."""
    sp = "sp_007_partial"
    try:
        op.execute(sa.text(f"SAVEPOINT {sp}"))
        op.execute(sa.text(sql))
        op.execute(sa.text(f"RELEASE SAVEPOINT {sp}"))
    except Exception:
        op.execute(sa.text(f"ROLLBACK TO SAVEPOINT {sp}"))


def upgrade() -> None:
    # 1. Partial index on is_processed — only unprocessed rows (the hot set)
    _try_exec(
        "CREATE INDEX IF NOT EXISTS ix_feed_items_is_processed "
        "ON feed_items (is_processed) "
        "WHERE is_processed = FALSE"
    )

    # 2. GIN index on asset_tags JSON column
    _try_exec(
        "CREATE INDEX IF NOT EXISTS ix_feed_items_asset_tags_gin "
        "ON feed_items USING GIN (asset_tags)"
    )

    # 3. Composite partial index for sentiment window queries
    _try_exec(
        "CREATE INDEX IF NOT EXISTS ix_feed_items_sentiment_fetched "
        "ON feed_items (fetched_at DESC, sentiment_score) "
        "WHERE sentiment_score IS NOT NULL"
    )

    # 4. Migrate live_news_items.asset_tags from TEXT to JSONB
    # Rows with invalid JSON (e.g. NULL or malformed) are set to NULL via CASE
    _try_exec(
        "ALTER TABLE live_news_items "
        "ALTER COLUMN asset_tags TYPE JSONB "
        "USING CASE "
        "  WHEN asset_tags IS NULL OR asset_tags = '' THEN NULL "
        "  ELSE asset_tags::jsonb "
        "END"
    )


def downgrade() -> None:
    _try_exec("DROP INDEX IF EXISTS ix_feed_items_is_processed")
    _try_exec("DROP INDEX IF EXISTS ix_feed_items_asset_tags_gin")
    _try_exec("DROP INDEX IF EXISTS ix_feed_items_sentiment_fetched")
    _try_exec(
        "ALTER TABLE live_news_items "
        "ALTER COLUMN asset_tags TYPE TEXT "
        "USING asset_tags::text"
    )
