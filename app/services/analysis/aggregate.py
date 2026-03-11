"""
Market analysis aggregation. Phase 1: aggregate feed items (with sentiment) into one report.
Includes current price snapshot for the asset universe (WTI, Brent, gold, XOM, Henry Hub).
"""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feed_item import FeedItem
from app.models.market_analysis import MarketAnalysis
from app.services.recommendations import get_price_snapshot


async def run_analysis(session: AsyncSession) -> MarketAnalysis:
    """
    Build market analysis from latest feed items (with sentiment_score and asset_tags).
    One row per run; caller can overwrite 'latest' or keep history by id.
    """
    q = select(FeedItem).order_by(FeedItem.fetched_at.desc()).limit(2000)
    result = await session.execute(q)
    items = result.scalars().all()

    by_category: dict[str, list[dict]] = {}
    for it in items:
        cat = it.category
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append({
            "id": it.id,
            "title": it.title,
            "link": it.link,
            "published": it.published.isoformat() if it.published else None,
            "sentiment_score": it.sentiment_score,
            "asset_tags": it.asset_tags,
        })

    by_asset: dict[str, list[float]] = {}
    for it in items:
        if it.sentiment_score is not None and it.asset_tags:
            for tag in it.asset_tags:
                by_asset.setdefault(tag, []).append(it.sentiment_score)

    as_of = datetime.now(timezone.utc)
    price_snapshot = get_price_snapshot()
    summary_lines = [
        f"Market analysis as of {as_of.isoformat()}.",
        f"Items: {len(items)}.",
        f"Categories: {list(by_category.keys())}.",
    ]
    for tag, scores in by_asset.items():
        avg = sum(scores) / len(scores) if scores else 0
        summary_lines.append(f"Sentiment ({tag}): avg {avg:.3f}, n={len(scores)}.")
    if price_snapshot:
        summary_lines.append("Prices: " + ", ".join(f"{k} {v}" for k, v in price_snapshot.items()) + ".")

    payload = {
        "by_category": by_category,
        "by_asset_sentiment": {k: {"avg": sum(v) / len(v) if v else 0, "count": len(v)} for k, v in by_asset.items()},
        "price_snapshot": price_snapshot,
        "price_snapshot_as_of": as_of.isoformat(),
        "summary_lines": summary_lines,
    }
    analysis = MarketAnalysis(
        id=str(uuid4()),
        as_of=as_of,
        sources_used={"feed_items": len(items)},
        summary="\n".join(summary_lines),
        payload=payload,
    )
    session.add(analysis)
    return analysis


async def get_latest_analysis(session: AsyncSession) -> MarketAnalysis | None:
    """Return the most recent MarketAnalysis row."""
    q = select(MarketAnalysis).order_by(MarketAnalysis.as_of.desc()).limit(1)
    result = await session.execute(q)
    return result.scalars().one_or_none()
