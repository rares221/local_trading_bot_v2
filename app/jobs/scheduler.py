"""
APScheduler jobs: ingestion + sentiment, analysis run, daily recommendation batch, active-trade updates.
Aligned with PLAN_MODE: one daily batch (≤5); update job only refreshes active trades.
"""
from datetime import datetime, timezone, date
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.feed_item import FeedItem
from app.models.recommendation import Recommendation, ActiveTrade
from app.models.market_analysis import MarketAnalysis
from app.services.ingestion import fetch_all_feeds, save_feed_items
from app.services.sentiment import score_sentiment, tag_assets, detect_tickers
from app.services.analysis import run_analysis, get_latest_analysis
from app.services.recommendations import build_daily_candidates


async def job_ingest_and_sentiment() -> None:
    """Fetch RSS feeds, save items, run sentiment and asset tagging."""
    async with async_session_factory() as session:
        items = fetch_all_feeds()
        saved = await save_feed_items(session, items)
        # Enrich existing items with sentiment (simple: recent unprocessed)
        q = select(FeedItem).where(FeedItem.is_processed == False).limit(500)
        result = await session.execute(q)
        for it in result.scalars().all():
            text = f"{it.title or ''} {it.description or ''}"
            it.sentiment_score = score_sentiment(text)
            keyword_tags = tag_assets(text) or []
            dynamic_tickers = detect_tickers(text) or []
            it.asset_tags = list(dict.fromkeys(keyword_tags + dynamic_tickers)) or None
            it.is_processed = True
        await session.commit()


async def job_run_analysis() -> None:
    """Build market analysis from feed items."""
    async with async_session_factory() as session:
        await run_analysis(session)
        await session.commit()


async def job_daily_batch() -> None:
    """
    Produce up to 5 recommendations for today (PLAN_MODE).
    Stub: creates placeholder recs if we have analysis; real engine later.
    """
    batch_id = date.today().isoformat()
    async with async_session_factory() as session:
        # Guard: do not add more pending if today already has pending recommendations (PLAN_MODE C3). Taken ones can coexist with a new set.
        q = select(Recommendation).where(Recommendation.batch_id == batch_id, Recommendation.status == "pending").limit(1)
        result = await session.execute(q)
        if result.scalars().first():
            return
        analysis = await get_latest_analysis(session)
        if not analysis:
            return
        # Engine: best trade ideas from analysis; respect user risk preference (from UI or config)
        from app.core.preferences import get_preferred_risk
        by_asset_sentiment = (analysis.payload or {}).get("by_asset_sentiment") or None
        candidates = build_daily_candidates(
            max_count=5,
            by_asset_sentiment=by_asset_sentiment,
            preferred_risk=get_preferred_risk(),
            tie_seed=batch_id,
        )
        if not candidates:
            return
        now = datetime.now(timezone.utc)
        for c in candidates:
            rec = Recommendation(
                id=str(uuid4()),
                created_at=now,
                batch_id=batch_id,
                asset_category=c.asset_category,
                asset_id=c.asset_id,
                buy_in_price=c.buy_in_price,
                stop_loss=c.stop_loss,
                take_profit=c.take_profit,
                expiry_date=None,
                rationale_ref=analysis.id,
                risk_tier=c.risk_tier,
                status="pending",
            )
            session.add(rec)
        await session.commit()


async def job_update_active_trades() -> None:
    """Refresh SL/TP/expiry for active trades only (PLAN_MODE C4). No new recommendations."""
    async with async_session_factory() as session:
        q = select(ActiveTrade)
        result = await session.execute(q)
        now = datetime.now(timezone.utc)
        for trade in result.scalars().all():
            # Stub: leave values as-is; real logic will pull from latest analysis/prices
            trade.last_updated = now
        await session.commit()


def start_scheduler() -> AsyncIOScheduler:
    """Schedule jobs. Call from main after uvicorn or run in worker."""
    scheduler = AsyncIOScheduler()
    # Ingestion + sentiment: every 2h (align with plan)
    scheduler.add_job(
        job_ingest_and_sentiment,
        "interval",
        hours=settings.analysis_run_interval_hours,
        id="ingest_sentiment",
    )
    # Analysis: every 2h
    scheduler.add_job(
        job_run_analysis,
        "interval",
        hours=settings.analysis_run_interval_hours,
        id="run_analysis",
    )
    # Daily batch: once per day at configured hour
    scheduler.add_job(
        job_daily_batch,
        "cron",
        hour=settings.daily_batch_hour,
        minute=0,
        id="daily_batch",
    )
    # Active trades update: every 2h
    scheduler.add_job(
        job_update_active_trades,
        "interval",
        hours=settings.update_active_trades_interval_hours,
        id="update_active_trades",
    )
    scheduler.start()
    return scheduler
