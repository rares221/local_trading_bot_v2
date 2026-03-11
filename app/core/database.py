from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from .base import Base


# Create data dir so SQLite file can be created
def _ensure_data_dir() -> None:
    url = settings.database_url
    if "sqlite" in url:
        # ./data/trading.db -> ./data
        path = Path(url.replace("sqlite+aiosqlite:///", ""))
        if path.suffix:
            path = path.parent
        path.mkdir(parents=True, exist_ok=True)


_ensure_data_dir()

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
)
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


def _add_rationale_text_column_if_missing(sync_conn) -> None:
    """Add recommendations.rationale_text if missing (Option B migration)."""
    result = sync_conn.execute(text("PRAGMA table_info(recommendations)"))
    rows = result.fetchall()
    if not rows:
        return
    has_col = any(row[1] == "rationale_text" for row in rows)
    if not has_col:
        sync_conn.execute(text("ALTER TABLE recommendations ADD COLUMN rationale_text TEXT"))


def _migrate_daily_briefings(sync_conn) -> None:
    """Create daily_briefings table if missing."""
    sync_conn.execute(text(
        "CREATE TABLE IF NOT EXISTS daily_briefings ("
        "id VARCHAR(36) PRIMARY KEY, "
        "created_at TIMESTAMP NOT NULL, "
        "content_text TEXT NOT NULL, "
        "payload JSON)"
    ))


def _migrate_active_trades_and_levels(sync_conn) -> None:
    """Add suggested_* to active_trades; ensure active_trade_levels exists; backfill initial level for existing trades."""
    import uuid
    result = sync_conn.execute(text("PRAGMA table_info(active_trades)"))
    rows = result.fetchall()
    if rows:
        names = [row[1] for row in rows]
        for col, typ in [("suggested_sl", "REAL"), ("suggested_tp", "REAL"), ("suggested_at", "TEXT")]:
            if col not in names:
                sync_conn.execute(text(f"ALTER TABLE active_trades ADD COLUMN {col} {typ}"))
    sync_conn.execute(text(
        "CREATE TABLE IF NOT EXISTS active_trade_levels ("
        "id VARCHAR(36) PRIMARY KEY, "
        "active_trade_id VARCHAR(36) NOT NULL, "
        "sl REAL NOT NULL, tp REAL NOT NULL, "
        "created_at TIMESTAMP NOT NULL, "
        "source VARCHAR(32) NOT NULL, "
        "FOREIGN KEY(active_trade_id) REFERENCES active_trades(id))"
    ))
    # Backfill: one 'initial' level per active_trade that has no levels yet
    cursor = sync_conn.execute(text(
        "SELECT at.id, at.current_sl, at.current_tp, at.user_taken_at FROM active_trades at "
        "WHERE NOT EXISTS (SELECT 1 FROM active_trade_levels l WHERE l.active_trade_id = at.id)"
    ))
    for row in cursor.fetchall():
        sync_conn.execute(
            text(
                "INSERT INTO active_trade_levels (id, active_trade_id, sl, tp, created_at, source) "
                "VALUES (:id, :aid, :sl, :tp, :created_at, 'initial')"
            ),
            {"id": str(uuid.uuid4()), "aid": row[0], "sl": row[1], "tp": row[2], "created_at": row[3]}
        )


async def init_db() -> None:
    import app.models  # noqa: F401 - register all models with Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_rationale_text_column_if_missing)
        await conn.run_sync(_migrate_daily_briefings)
        await conn.run_sync(_migrate_active_trades_and_levels)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
