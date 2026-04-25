from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from .base import Base


engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# Alias used by background tasks that create their own sessions
AsyncSessionLocal = async_session_factory


async def init_db() -> None:
    """
    Initialize database.
    Schema is managed by Alembic migrations for PostgreSQL.
    For SQLite (dev), create_all is used as fallback.
    """
    import app.models  # noqa: F401 — register all models with Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(_ensure_timescaledb_extension)
        # For SQLite dev: create any tables that don't exist yet (idempotent).
        # PostgreSQL uses Alembic migrations instead.
        if "sqlite" in str(engine.url):
            await conn.run_sync(Base.metadata.create_all)


def _ensure_timescaledb_extension(sync_conn) -> None:
    from sqlalchemy import text
    try:
        sync_conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE"))
    except Exception:
        pass


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
