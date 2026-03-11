"""
APScheduler jobs: ingestion + sentiment, analysis run, daily recommendation batch, active-trade updates.
Aligned with PLAN_MODE: one daily batch (≤5); update job only refreshes active trades.
"""
from datetime import datetime, timezone, date, timedelta
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.feed_item import FeedItem
from app.models.recommendation import Recommendation, ActiveTrade, ActiveTradeLevel
from app.models.market_analysis import MarketAnalysis
from app.services.ingestion import fetch_all_feeds, save_feed_items
from app.services.sentiment import score_sentiment, tag_assets, detect_tickers
from app.services.analysis import run_analysis, get_latest_analysis
from app.services.recommendations import build_daily_candidates, compute_sl_tp_for_price, get_asset_price_and_risk
from app.services.briefing import run_briefing


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
        # Do not create new pending recs for assets that already have a taken recommendation (§5.4)
        q_taken = select(Recommendation.asset_category, Recommendation.asset_id).where(Recommendation.status == "taken").distinct()
        result_taken = await session.execute(q_taken)
        exclude_taken = {(row[0], row[1]) for row in result_taken.all()}
        from app.core.preferences import get_preferred_risk
        by_asset_sentiment = (analysis.payload or {}).get("by_asset_sentiment") or None
        candidates = build_daily_candidates(
            max_count=5,
            by_asset_sentiment=by_asset_sentiment,
            preferred_risk=get_preferred_risk(),
            tie_seed=batch_id,
            exclude_taken=exclude_taken,
        )
        if not candidates:
            return
        now = datetime.now(timezone.utc)
        from app.services.research import build_research_context, generate_rationale
        for c in candidates:
            rationale_text = None
            try:
                ctx = await build_research_context(session, c.asset_id, c.asset_category)
                ctx["buy_in_price"] = c.buy_in_price
                rationale_text = await generate_rationale(ctx)
            except Exception:
                pass
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
                rationale_text=rationale_text,
                risk_tier=c.risk_tier,
                status="pending",
            )
            session.add(rec)
        await session.commit()


async def job_update_active_trades() -> None:
    """
    Suggest new SL/TP from latest price; do not overwrite user's original levels.
    Writes to suggested_sl/suggested_tp/suggested_at and appends to levels history.
    """
    async with async_session_factory() as session:
        q = select(ActiveTrade).options(selectinload(ActiveTrade.recommendation))
        result = await session.execute(q)
        now = datetime.now(timezone.utc)
        for trade in result.scalars().all():
            rec = trade.recommendation
            if not rec:
                continue
            price_risk = get_asset_price_and_risk(rec.asset_category, rec.asset_id)
            if not price_risk:
                trade.last_updated = now
                continue
            price, _ = price_risk
            sl, tp = compute_sl_tp_for_price(price, rec.risk_tier)
            trade.suggested_sl = sl
            trade.suggested_tp = tp
            trade.suggested_at = now
            trade.last_updated = now
            level = ActiveTradeLevel(
                active_trade_id=trade.id,
                sl=sl,
                tp=tp,
                created_at=now,
                source="scheduled_update",
            )
            session.add(level)
        await session.commit()


async def job_daily_briefing() -> None:
    """Run daily briefing checklist and store result (heartbeat-style)."""
    async with async_session_factory() as session:
        await run_briefing(session)
        await session.commit()


async def job_cleanup_feed_items() -> None:
    """Delete feed_items older than feed_max_age_days. Keeps DB size bounded."""
    async with async_session_factory() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.feed_max_age_days)
        await session.execute(delete(FeedItem).where(FeedItem.fetched_at < cutoff))
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
    # Daily briefing: once per day (e.g. after batch; same hour + 1 or fixed 8:00)
    briefing_hour = min(23, (settings.daily_batch_hour + 1) % 24)
    scheduler.add_job(
        job_daily_briefing,
        "cron",
        hour=briefing_hour,
        minute=5,
        id="daily_briefing",
    )
    # Active trades update: every 2h
    scheduler.add_job(
        job_update_active_trades,
        "interval",
        hours=settings.update_active_trades_interval_hours,
        id="update_active_trades",
    )
    # Feed-item cleanup: daily (delete items older than feed_max_age_days)
    scheduler.add_job(
        job_cleanup_feed_items,
        "cron",
        hour=4,
        minute=0,
        id="cleanup_feed_items",
    )
    scheduler.start()
    return scheduler
