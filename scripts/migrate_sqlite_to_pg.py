"""
SQLite → PostgreSQL data migration.

Reads all rows from the SQLite database and inserts them into PostgreSQL.
Runs automatically at startup when MIGRATION_ENABLED=true.

Usage (standalone):
    python scripts/migrate_sqlite_to_pg.py

Environment variables used:
    SQLITE_SOURCE_URL  — source SQLite path, e.g. sqlite:///./data/trading.db
    DATABASE_URL       — target PostgreSQL URL (postgresql+asyncpg://...)
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Table migration order respects foreign key dependencies
TABLES_IN_ORDER = [
    "feed_items",
    "market_analyses",
    "daily_briefings",
    "recommendations",
    "active_trades",
    "active_trade_levels",
]


def _sqlite_url_to_path(url: str) -> Path:
    """Convert sqlite:///./data/trading.db → Path('./data/trading.db')."""
    return Path(url.replace("sqlite:///", "").replace("sqlite://", ""))


def _coerce_value(value: Any, pg_type: str = "") -> Any:
    """
    Coerce a SQLite value to the correct Python type for PostgreSQL:
    - 'null' string → None
    - Boolean columns: SQLite stores 0/1 integers → Python bool
    - JSON columns: must be a JSON string (psycopg2 would send lists as text[] otherwise)
    - Other string values that look like JSON → parsed (for generic cases)
    """
    # SQLite can store None as the string 'null'
    if value == "null":
        return None

    # Boolean columns: SQLite stores 0/1 as integers
    if "bool" in pg_type:
        if value is None:
            return None
        return bool(value)

    # JSON columns: psycopg2 sends Python list/dict as text[] / hstore unless we
    # serialize back to a JSON string — PostgreSQL will cast it correctly.
    if pg_type in ("json", "jsonb"):
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        if isinstance(value, str):
            stripped = value.strip()
            # Already a JSON string — validate and return as-is
            if stripped and stripped[0] in ("{", "["):
                try:
                    json.loads(stripped)  # validate
                    return stripped
                except (json.JSONDecodeError, ValueError):
                    pass
        return value

    # For non-JSON string values that look like JSON objects/arrays, parse them
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped[0] in ("{", "["):
            try:
                return json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                pass

    return value


def run_migration(sqlite_url: str, pg_url: str) -> dict[str, int]:
    """
    Synchronous migration: reads from SQLite, writes to PostgreSQL.
    Returns a dict of {table: rows_migrated}.
    """
    import sqlite3
    import psycopg2
    import psycopg2.extras

    # Convert asyncpg URL to psycopg2 URL for sync migration
    pg_sync_url = pg_url.replace("postgresql+asyncpg://", "postgresql://")

    sqlite_path = _sqlite_url_to_path(sqlite_url)
    if not sqlite_path.exists():
        logger.warning("[MIGRATION] SQLite source not found at %s — skipping migration", sqlite_path)
        return {}

    logger.info("[MIGRATION] Connecting to SQLite: %s", sqlite_path)
    src = sqlite3.connect(str(sqlite_path))
    src.row_factory = sqlite3.Row

    logger.info("[MIGRATION] Connecting to PostgreSQL: %s", pg_sync_url.split("@")[-1])
    dst = psycopg2.connect(pg_sync_url)
    dst.autocommit = False

    counts: dict[str, int] = {}

    try:
        with dst.cursor() as cur:
            # Disable FK checks during bulk load, re-enable after
            cur.execute("SET session_replication_role = 'replica'")

        for table in TABLES_IN_ORDER:
            # Check source table exists
            src_cur = src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
            if not src_cur.fetchone():
                logger.info("[MIGRATION] Table %s not in SQLite — skipping", table)
                counts[table] = 0
                continue

            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                logger.info("[MIGRATION] Table %s is empty — skipping", table)
                counts[table] = 0
                continue

            columns = [desc[0] for desc in src.execute(f"SELECT * FROM {table} LIMIT 0").description]

            # Fetch PostgreSQL column types so we can coerce SQLite values correctly
            with dst.cursor() as type_cur:
                type_cur.execute(
                    """
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_name = %s
                    """,
                    (table,),
                )
                pg_types: dict[str, str] = {r[0]: r[1].lower() for r in type_cur.fetchall()}

            # Build INSERT ... ON CONFLICT DO NOTHING (idempotent re-runs)
            placeholders = ", ".join(["%s"] * len(columns))
            col_list = ", ".join(f'"{c}"' for c in columns)
            insert_sql = (
                f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) '
                f"ON CONFLICT DO NOTHING"
            )

            batch: list[tuple] = []
            for row in rows:
                values = tuple(
                    _coerce_value(row[c], pg_types.get(c, "")) for c in columns
                )
                batch.append(values)

            with dst.cursor() as cur:
                psycopg2.extras.execute_batch(cur, insert_sql, batch, page_size=500)

            dst.commit()
            counts[table] = len(batch)
            logger.info("[MIGRATION] %s: %d rows migrated", table, len(batch))

        with dst.cursor() as cur:
            cur.execute("SET session_replication_role = 'origin'")
        dst.commit()

    except Exception:
        dst.rollback()
        logger.exception("[MIGRATION] Migration failed — rolling back")
        raise
    finally:
        src.close()
        dst.close()

    total = sum(counts.values())
    logger.info("[MIGRATION] Done. Total rows migrated: %d across %d tables", total, len(counts))
    return counts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")

    # Allow running standalone with env vars or default paths
    import os
    sqlite_url = os.environ.get("SQLITE_SOURCE_URL", "sqlite:///./data/trading.db")
    pg_url = os.environ.get("DATABASE_URL", "")

    if not pg_url or "postgresql" not in pg_url:
        print("ERROR: DATABASE_URL must point to a PostgreSQL instance.", file=sys.stderr)
        sys.exit(1)

    run_migration(sqlite_url, pg_url)
