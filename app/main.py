from contextlib import asynccontextmanager
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.database import init_db
from app.api.routes import router
from app.api.backtest_routes import router as backtest_router
from app.jobs.scheduler import start_scheduler
from app.live_trading.live_routes import live_router

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _setup_app_logging() -> None:
    """
    Uvicorn configures its own loggers; app code uses logging.getLogger(__name__).
    Without a handler on those loggers (or DEBUG root), [ENGINE] INFO lines never hit the terminal.
    """
    fmt = logging.Formatter("%(levelname)s [%(name)s] %(message)s")
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(fmt)
    for name in (
        "app.main",
        "app.api.routes",
        "app.services.recommendations.engine",
        "app.jobs.scheduler",
        "app.services.technicals",
        "app.live_trading.live_analyzer",
        "app.live_trading.live_confidence",
        "app.live_trading.stream_processor",
        "scripts.migrate_sqlite_to_pg",
    ):
        log = logging.getLogger(name)
        log.setLevel(logging.INFO)
        if not any(type(h) is logging.StreamHandler for h in log.handlers):
            log.addHandler(handler)
        log.propagate = False


_setup_app_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_db()
    except Exception:
        logger.exception(
            "Database initialization failed. "
            "For SQLite: restore data/*.db from backup or remove the DB file. "
            "For PostgreSQL: ensure DATABASE_URL is set and TimescaleDB is running."
        )
        raise
    # Fail fast on missing required config (LLM provider / key) before serving.
    from app.core.config import validate_startup_config
    validate_startup_config()
    try:
        from app.services.calibration import load_platt_from_disk
        load_platt_from_disk()
    except Exception:
        pass
    # Refresh macro calendar cache if stale (runs in background, non-blocking)
    try:
        from app.services.macro_calendar import refresh_dates
        refresh_dates(force=False)
    except Exception:
        pass
    scheduler = start_scheduler()

    # Live Trading — stream engine
    try:
        import asyncio
        from app.live_trading.stream_engine import engine as live_engine
        from app.live_trading.stream_processor import processor as live_processor
        from app.live_trading.trade_watcher import get_watchlist
        live_engine.watchlist = get_watchlist()
        live_processor.set_engine(live_engine)
        asyncio.create_task(live_engine.start())
        logger.info("[LIVE_TRADING] Stream engine started, watchlist=%s", live_engine.watchlist)
    except Exception:
        logger.exception("[LIVE_TRADING] Failed to start stream engine")

    yield
    scheduler.shutdown(wait=False)


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
)
app.include_router(router, prefix="/api", tags=["api"])
app.include_router(backtest_router, prefix="/api", tags=["backtest"])
app.include_router(live_router, prefix="/api/live", tags=["live-trading"])
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


def _read_static(name: str) -> str:
    path = STATIC_DIR / name
    if not path.is_file():
        return f"<html><body><h1>Not found</h1><p>Static file missing: {name}</p></body></html>"
    return path.read_text(encoding="utf-8")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    """Simple UI: buttons for pipeline, refresh, clear; link to recommendations."""
    return HTMLResponse(content=_read_static("dashboard.html"))


@app.get("/recommendations", response_class=HTMLResponse)
async def recommendations_page() -> HTMLResponse:
    """Recommendations page: stocks, commodities, other; Took it / Skipped."""
    return HTMLResponse(content=_read_static("recommendations.html"))


@app.get("/trade-builder", response_class=HTMLResponse)
async def trade_builder_page() -> HTMLResponse:
    """Plan execution (vanilla vs split TP) before opening active trades."""
    return HTMLResponse(content=_read_static("trade-builder.html"))


@app.get("/analysis", response_class=HTMLResponse)
async def analysis_page() -> HTMLResponse:
    """Market analysis page: latest report (summary + payload)."""
    return HTMLResponse(content=_read_static("analysis.html"))


@app.get("/active-trades", response_class=HTMLResponse)
async def active_trades_page() -> HTMLResponse:
    """Active trades page: list of trades user marked as taken."""
    return HTMLResponse(content=_read_static("active-trades.html"))


@app.get("/research", response_class=HTMLResponse)
async def research_page() -> HTMLResponse:
    """Research an asset: form to look up any asset and get LLM research summary."""
    return HTMLResponse(content=_read_static("research.html"))


@app.get("/briefing", response_class=HTMLResponse)
async def briefing_page() -> HTMLResponse:
    """Daily briefing: heartbeat-style checklist (SPX/VIX/oil, recs today)."""
    return HTMLResponse(content=_read_static("briefing.html"))


@app.get("/journal", response_class=HTMLResponse)
async def journal_page() -> HTMLResponse:
    """Trade journal: review closed trades with outcomes, filter by confidence/regime/asset."""
    return HTMLResponse(content=_read_static("journal.html"))


@app.get("/calendar", response_class=HTMLResponse)
async def calendar_page() -> HTMLResponse:
    """Macro calendar: FOMC, CPI, NFP dates that affect recommendation confidence."""
    return HTMLResponse(content=_read_static("calendar.html"))


@app.get("/backtest", response_class=HTMLResponse)
async def backtest_page() -> HTMLResponse:
    """Backtesting: run historical replay and view results (win rate, Sharpe, drawdown)."""
    return HTMLResponse(content=_read_static("backtest.html"))


@app.get("/live-trading", response_class=HTMLResponse)
async def live_trading_page() -> HTMLResponse:
    """Live Trading: real-time market monitor, LLM analysis, selected trade watcher."""
    return HTMLResponse(content=_read_static("live-trading.html"))
