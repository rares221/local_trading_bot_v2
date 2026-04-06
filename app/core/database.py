from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from .base import Base


def _ensure_sqlite_data_dir() -> None:
    url = settings.database_url
    if "sqlite" in url:
        path = Path(url.replace("sqlite+aiosqlite:///", ""))
        if path.suffix:
            path = path.parent
        path.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_data_dir()

_engine_kwargs: dict = {"echo": settings.debug}
if "postgresql" in settings.database_url:
    # asyncpg pool sizing for production
    _engine_kwargs.update({"pool_size": 10, "max_overflow": 20, "pool_pre_ping": True})

engine = create_async_engine(settings.database_url, **_engine_kwargs)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def init_db() -> None:
    """
    Initialize database schema.
    - SQLite (dev): create tables via SQLAlchemy metadata (no Alembic).
    - PostgreSQL (prod): Alembic runs migrations at deploy; this only ensures
      the TimescaleDB extension exists and tables are present if Alembic hasn't run yet.
    """
    import app.models  # noqa: F401 — register all models with Base.metadata

    if "postgresql" in settings.database_url:
        async with engine.begin() as conn:
            await conn.run_sync(_ensure_timescaledb_extension)
        # Schema is managed by Alembic — do not call create_all here to avoid conflicts.
        return

    # SQLite: create tables + run inline column migrations for backwards compatibility
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_sqlite_compat_migrations)


def _ensure_timescaledb_extension(sync_conn) -> None:
    from sqlalchemy import text
    try:
        sync_conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE"))
    except Exception:
        # TimescaleDB not installed (plain PostgreSQL) — continue without it
        pass


def _sqlite_compat_migrations(sync_conn) -> None:
    """
    Inline column additions for SQLite — keeps backward compatibility when running
    without Alembic. PostgreSQL uses proper Alembic migration files instead.
    """
    from sqlalchemy import text
    import uuid

    def _ensure_columns(table: str, columns: list[tuple[str, str, str]]) -> None:
        result = sync_conn.execute(text(f"PRAGMA table_info({table})"))
        rows = result.fetchall()
        if not rows:
            return
        existing = {row[1] for row in rows}
        for col, typ, default in columns:
            if col not in existing:
                stmt = f"ALTER TABLE {table} ADD COLUMN {col} {typ}"
                if default:
                    stmt += f" DEFAULT {default}"
                sync_conn.execute(text(stmt))

    # recommendations
    _ensure_columns("recommendations", [
        ("rationale_text",     "TEXT",        ""),
        ("direction",          "VARCHAR(8)",  "'long'"),
        ("confidence_score",   "INTEGER",     ""),
        ("risk_reward",        "REAL",        ""),
        ("volatility_notes",   "TEXT",        ""),
        ("suggested_risk_pct", "REAL",        ""),
        ("min_confidence_gate", "INTEGER",    ""),
        ("shadow_b_threshold", "INTEGER",     ""),
        ("shadow_b_pass",      "BOOLEAN",     ""),
        ("instrument_type",    "VARCHAR(16)", ""),
    ])

    # active_trades
    _ensure_columns("active_trades", [
        ("suggested_sl",      "REAL",        ""),
        ("suggested_tp",      "REAL",        ""),
        ("suggested_at",      "TEXT",        ""),
        ("trade_phase",       "VARCHAR(24)", "'open'"),
        ("breakeven_hit",     "BOOLEAN",     "0"),
        ("highest_price",     "REAL",        ""),
        ("lowest_price",      "REAL",        ""),
        ("signal_flip_count", "INTEGER",     "0"),
        ("outcome",           "VARCHAR(24)", ""),
        ("outcome_at",        "TEXT",        ""),
        ("closing_price",     "REAL",        ""),
    ])

    # active_trade_levels table
    sync_conn.execute(text(
        "CREATE TABLE IF NOT EXISTS active_trade_levels ("
        "id VARCHAR(36) PRIMARY KEY, "
        "active_trade_id VARCHAR(36) NOT NULL, "
        "sl REAL NOT NULL, tp REAL NOT NULL, "
        "created_at TIMESTAMP NOT NULL, "
        "source VARCHAR(32) NOT NULL, "
        "FOREIGN KEY(active_trade_id) REFERENCES active_trades(id))"
    ))

    # daily_briefings table
    sync_conn.execute(text(
        "CREATE TABLE IF NOT EXISTS daily_briefings ("
        "id VARCHAR(36) PRIMARY KEY, "
        "created_at TIMESTAMP NOT NULL, "
        "content_text TEXT NOT NULL, "
        "payload JSON)"
    ))

    # active_trades: add strategy_leg / position_fraction if missing (multi-leg support)
    result = sync_conn.execute(text("PRAGMA table_info(active_trades)"))
    rows = result.fetchall()
    if rows and "strategy_leg" not in {r[1] for r in rows}:
        colnames = [r[1] for r in rows]
        parts: list[str] = []
        for r in rows:
            _cid, name, ctype, notnull, _dflt, pk = r
            coltype = (ctype or "NUMERIC").strip() or "NUMERIC"
            line = f'"{name}" {coltype}'
            if pk:
                line += " NOT NULL PRIMARY KEY"
            elif notnull:
                line += " NOT NULL"
            parts.append(line)
        parts.append('"strategy_leg" VARCHAR(32) NOT NULL DEFAULT \'vanilla\'')
        parts.append('"position_fraction" REAL NOT NULL DEFAULT 1.0')
        cols_sql = ", ".join(parts)
        create_sql = (
            f"CREATE TABLE active_trades_new ({cols_sql}, "
            "UNIQUE (recommendation_id, strategy_leg), "
            "FOREIGN KEY (recommendation_id) REFERENCES recommendations (id))"
        )
        old_cols_q = ", ".join(f'"{c}"' for c in colnames)
        sync_conn.execute(text("PRAGMA foreign_keys=OFF"))
        sync_conn.execute(text(create_sql))
        sync_conn.execute(text(
            f"INSERT INTO active_trades_new ({old_cols_q}, strategy_leg, position_fraction) "
            f"SELECT {old_cols_q}, 'vanilla', 1.0 FROM active_trades"
        ))
        sync_conn.execute(text("DROP TABLE active_trades"))
        sync_conn.execute(text("ALTER TABLE active_trades_new RENAME TO active_trades"))
        sync_conn.execute(text("PRAGMA foreign_keys=ON"))

    # Backfill initial level for trades with no levels
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
