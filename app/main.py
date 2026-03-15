from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.database import init_db
from app.api.routes import router
from app.jobs.scheduler import start_scheduler

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler = start_scheduler()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
)
app.include_router(router, prefix="/api", tags=["api"])
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
