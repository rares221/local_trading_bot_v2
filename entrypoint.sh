#!/bin/sh
set -e

# 1. Run Alembic migrations (creates / updates schema in PostgreSQL)
if echo "$DATABASE_URL" | grep -q "postgresql"; then
    echo "[ENTRYPOINT] Running Alembic migrations..."
    alembic upgrade head
    echo "[ENTRYPOINT] Alembic migrations done"
fi

# 2. SQLite → PostgreSQL data transfer (one-time, idempotent)
if [ "$MIGRATION_ENABLED" = "true" ]; then
    echo "[ENTRYPOINT] Running SQLite → PostgreSQL data migration..."
    python3 scripts/migrate_sqlite_to_pg.py
    echo "[ENTRYPOINT] Data migration done"
fi

# 3. Start the app
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
