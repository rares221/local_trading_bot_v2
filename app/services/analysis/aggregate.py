"""
Market analysis aggregation. Phase 1: aggregate feed items (with sentiment) into one report.
Includes current price snapshot and time-decay weighted sentiment.
"""
import math
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feed_item import FeedItem
from app.models.market_analysis import MarketAnalysis
from app.services.recommendations import get_price_snapshot

# Half-life for sentiment decay in hours: items from 24h ago have half the weight
SENTIMENT_HALF_LIFE_HOURS = 24.0


def _decay_weight(item_time: datetime | None, now: datetime) -> float:
    """Exponential decay weight based on age. Recent items weigh more."""
    if item_time is None:
        return 0.5
    if item_time.tzinfo is None:
        item_time = item_time.replace(tzinfo=timezone.utc)
    age_hours = max(0, (now - item_time).total_seconds() / 3600)
    return math.exp(-0.693 * age_hours / SENTIMENT_HALF_LIFE_HOURS)


async def run_analysis(session: AsyncSession) -> MarketAnalysis:
    """
    Build market analysis from latest feed items (with sentiment_score and asset_tags).
    Sentiment is time-decay weighted: recent items influence the average more.
    """
    q = select(FeedItem).order_by(FeedItem.fetched_at.desc()).limit(2000)
    result = await session.execute(q)
    items = result.scalars().all()

    now = datetime.now(timezone.utc)

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

    # Time-decay weighted sentiment per asset tag
    by_asset_weighted: dict[str, list[tuple[float, float]]] = {}  # tag -> [(score, weight)]
    for it in items:
        if it.sentiment_score is not None and it.asset_tags:
            w = _decay_weight(it.published or it.fetched_at, now)
            for tag in it.asset_tags:
                by_asset_weighted.setdefault(tag, []).append((it.sentiment_score, w))

    by_asset_sentiment: dict[str, dict] = {}
    for tag, pairs in by_asset_weighted.items():
        total_weight = sum(w for _, w in pairs)
        if total_weight > 0:
            weighted_avg = sum(s * w for s, w in pairs) / total_weight
        else:
            weighted_avg = sum(s for s, _ in pairs) / len(pairs) if pairs else 0
        by_asset_sentiment[tag] = {
            "avg": round(weighted_avg, 4),
            "count": len(pairs),
        }

    as_of = now
    price_snapshot = get_price_snapshot()
    summary_lines = [
        f"Market analysis as of {as_of.isoformat()}.",
        f"Items: {len(items)}.",
        f"Categories: {list(by_category.keys())}.",
    ]
    for tag, info in by_asset_sentiment.items():
        summary_lines.append(
            f"Sentiment ({tag}): weighted avg {info['avg']:.3f}, n={info['count']}."
        )
    if price_snapshot:
        summary_lines.append(
            "Prices: " + ", ".join(f"{k} {v}" for k, v in price_snapshot.items()) + "."
        )

    payload = {
        "by_category": by_category,
        "by_asset_sentiment": by_asset_sentiment,
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
    q = select(MarketAnalysis).order_by(MarketAnalysis.as_of.desc()).limit(1)
    result = await session.execute(q)
    return result.scalars().one_or_none()
