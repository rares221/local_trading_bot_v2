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


async def init_db() -> None:
    import app.models  # noqa: F401 - register all models with Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_rationale_text_column_if_missing)


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
