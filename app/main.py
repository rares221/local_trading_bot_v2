from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.core.database import init_db
from app.api.routes import router
from app.jobs.scheduler import start_scheduler


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


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "app": settings.app_name,
        "docs": "/docs",
        "health": "/api/health",
        "recommendations": "/api/recommendations",
        "analysis": "/api/analysis/latest",
    }
