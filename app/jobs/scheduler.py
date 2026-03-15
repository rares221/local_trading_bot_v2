"""
APScheduler jobs: ingestion + sentiment, analysis run, daily recommendation batch,
active-trade updates (ATR-based trailing), daily briefing, feed cleanup.
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
from app.services.recommendations import build_daily_candidates, get_asset_price_and_risk
from app.services.recommendations.engine import get_yf_symbol, compute_sl_tp_for_price, get_sentiment_score_and_count
from app.services.technicals import build_snapshot, compute_atr_levels, clear_cache, determine_direction
from app.services.briefing import run_briefing


async def job_ingest_and_sentiment() -> None:
    """Fetch RSS feeds, save items, run sentiment and asset tagging."""
    from app.services.ticker_resolver import reset_run_counter
    reset_run_counter()
    async with async_session_factory() as session:
        items = fetch_all_feeds()
        saved = await save_feed_items(session, items)
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
    Produce up to 5 recommendations per category for today.
    Uses ATR-based SL/TP, long/short direction, and expiry estimation.
    """
    batch_id = date.today().isoformat()
    async with async_session_factory() as session:
        q = select(Recommendation).where(
            Recommendation.batch_id == batch_id, Recommendation.status == "pending"
        ).limit(1)
        result = await session.execute(q)
        if result.scalars().first():
            return
        analysis = await get_latest_analysis(session)
        if not analysis:
            return
        q_taken = select(Recommendation.asset_category, Recommendation.asset_id).where(
            Recommendation.status == "taken"
        ).distinct()
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
                ctx["direction"] = c.direction
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
                expiry_date=c.expiry_date,
                rationale_ref=analysis.id,
                rationale_text=rationale_text,
                risk_tier=c.risk_tier,
                direction=c.direction,
                confidence_score=c.confidence_score,
                risk_reward=c.risk_reward,
                volatility_notes=c.volatility_notes,
                status="pending",
            )
            session.add(rec)
        await session.commit()


async def job_update_active_trades() -> None:
    """
    Professional trade management lifecycle for active trades:
    1. OPEN phase: Track price movement, compute ATR trailing SL/TP
    2. At 1R profit (risk = |entry - SL|): suggest moving SL to breakeven -> PARTIAL_TP phase
    3. TRAILING phase: SL only moves favorably, trail behind structure
    4. If price hits TP or SL -> mark CLOSED
    """
    clear_cache()
    async with async_session_factory() as session:
        # Load latest analysis for sentiment lookup
        analysis = await get_latest_analysis(session)
        by_asset_sentiment = (analysis.payload or {}).get("by_asset_sentiment", {}) if analysis and analysis.payload else {}

        q = select(ActiveTrade).options(selectinload(ActiveTrade.recommendation))
        result = await session.execute(q)
        now = datetime.now(timezone.utc)
        for trade in result.scalars().all():
            rec = trade.recommendation
            if not rec:
                continue
            symbol = get_yf_symbol(rec.asset_category, rec.asset_id)
            if not symbol:
                trade.last_updated = now
                continue
            snap = build_snapshot(symbol)
            if not snap or snap.price <= 0:
                trade.last_updated = now
                continue

            direction = getattr(rec, "direction", "long") or "long"
            current_price = snap.price
            entry_price = rec.buy_in_price
            original_sl = trade.current_sl
            risk = abs(entry_price - original_sl)
            phase = getattr(trade, "trade_phase", "open") or "open"

            # Track extreme prices for trailing
            if direction == "long":
                if trade.highest_price is None or current_price > trade.highest_price:
                    trade.highest_price = current_price
            else:
                if trade.lowest_price is None or current_price < trade.lowest_price:
                    trade.lowest_price = current_price

            # Phase transitions
            if phase == "open" and risk > 0:
                # Check if we've reached 1R profit (reward = risk distance)
                if direction == "long" and current_price >= entry_price + risk:
                    trade.trade_phase = "partial_tp"
                    phase = "partial_tp"
                elif direction == "short" and current_price <= entry_price - risk:
                    trade.trade_phase = "partial_tp"
                    phase = "partial_tp"

            if phase == "partial_tp" and not trade.breakeven_hit:
                # Move SL to breakeven (entry price + small buffer)
                buffer = risk * 0.05  # 5% of risk as buffer above breakeven
                if direction == "long":
                    trade.breakeven_hit = True
                    # SL moves to entry + buffer
                    sl_breakeven = round(entry_price + buffer, 2)
                    if trade.suggested_sl is None or sl_breakeven > trade.suggested_sl:
                        trade.suggested_sl = sl_breakeven
                else:
                    trade.breakeven_hit = True
                    sl_breakeven = round(entry_price - buffer, 2)
                    if trade.suggested_sl is None or sl_breakeven < trade.suggested_sl:
                        trade.suggested_sl = sl_breakeven
                trade.trade_phase = "trailing"
                phase = "trailing"

            # ATR-based trailing (all phases)
            sl, tp = compute_atr_levels(current_price, rec.risk_tier, snapshot=snap, direction=direction)

            # SL only moves in favorable direction
            if trade.suggested_sl is not None:
                if direction == "long":
                    sl = max(sl, trade.suggested_sl)
                else:
                    sl = min(sl, trade.suggested_sl)

            # Phase check: if current price has hit TP or SL, mark closed
            if direction == "long":
                if current_price <= (trade.suggested_sl or original_sl):
                    trade.trade_phase = "closed_sl"
                elif current_price >= rec.take_profit:
                    trade.trade_phase = "closed_tp"
            else:
                if current_price >= (trade.suggested_sl or original_sl):
                    trade.trade_phase = "closed_sl"
                elif current_price <= rec.take_profit:
                    trade.trade_phase = "closed_tp"

            # Signal flip detection: re-evaluate direction from current data
            pair = get_sentiment_score_and_count(rec.asset_category, rec.asset_id, by_asset_sentiment)
            current_sentiment = pair[0] if pair else None
            current_signal = determine_direction(current_sentiment, snap)
            flip_count = getattr(trade, "signal_flip_count", 0) or 0
            if current_signal != direction:
                trade.signal_flip_count = min(flip_count + 1, 10)
            else:
                trade.signal_flip_count = max(flip_count - 1, 0)

            trade.suggested_sl = sl
            trade.suggested_tp = tp
            trade.suggested_at = now
            trade.last_updated = now
            level = ActiveTradeLevel(
                active_trade_id=trade.id,
                sl=sl,
                tp=tp,
                created_at=now,
                source=f"scheduled_update_{phase}",
            )
            session.add(level)
        await session.commit()


async def job_daily_briefing() -> None:
    """Run daily briefing checklist and store result."""
    async with async_session_factory() as session:
        await run_briefing(session)
        await session.commit()


async def job_cleanup_feed_items() -> None:
    """Delete feed_items older than feed_max_age_days."""
    async with async_session_factory() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.feed_max_age_days)
        await session.execute(delete(FeedItem).where(FeedItem.fetched_at < cutoff))
        await session.commit()


def start_scheduler() -> AsyncIOScheduler:
    """Schedule jobs."""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        job_ingest_and_sentiment,
        "interval",
        hours=settings.analysis_run_interval_hours,
        id="ingest_sentiment",
    )
    scheduler.add_job(
        job_run_analysis,
        "interval",
        hours=settings.analysis_run_interval_hours,
        id="run_analysis",
    )
    scheduler.add_job(
        job_daily_batch,
        "cron",
        hour=settings.daily_batch_hour,
        minute=0,
        id="daily_batch",
    )
    briefing_hour = min(23, (settings.daily_batch_hour + 1) % 24)
    scheduler.add_job(
        job_daily_briefing,
        "cron",
        hour=briefing_hour,
        minute=5,
        id="daily_briefing",
    )
    scheduler.add_job(
        job_update_active_trades,
        "interval",
        hours=settings.update_active_trades_interval_hours,
        id="update_active_trades",
    )
    scheduler.add_job(
        job_cleanup_feed_items,
        "cron",
        hour=4,
        minute=0,
        id="cleanup_feed_items",
    )
    scheduler.start()
    return scheduler
