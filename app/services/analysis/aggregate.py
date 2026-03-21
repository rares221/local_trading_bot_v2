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
from app.services.recommendations import get_price_snapshot, get_previous_price_snapshot
from app.services.sentiment import tag_event_types

# Half-life for sentiment decay in hours: items from 24h ago have half the weight
SENTIMENT_HALF_LIFE_HOURS = 24.0
# Windows for sentiment trend: recent = last 24h, older = 24–72h (for divergence)
SENTIMENT_RECENT_HOURS = 24.0
SENTIMENT_OLDER_MAX_HOURS = 72.0

# Source reliability: feed URL (substring match) -> weight. Higher = more trust.
SOURCE_WEIGHTS: dict[str, float] = {
    "federalreserve.gov": 1.35,
    "ecb.europa.eu": 1.35,
    "reuters.com": 1.2,
    "bloomberg.com": 1.2,
    "ft.com": 1.15,
    "cnbc.com": 1.1,
    "eia.gov": 1.2,
    "kitco.com": 1.1,
    "oilprice.com": 1.1,
    "seekingalpha.com": 1.05,
}
DEFAULT_SOURCE_WEIGHT = 1.0

# Entity weight: tag (e.g. Fed, ECB) -> weight. Central banks / official = higher.
ENTITY_WEIGHTS: dict[str, float] = {
    "Fed": 1.25,
    "ECB": 1.25,
    "BOJ": 1.2,
    "BOE": 1.2,
    "PBOC": 1.2,
}
DEFAULT_ENTITY_WEIGHT = 1.0


def _decay_weight(item_time: datetime | None, now: datetime) -> float:
    """Exponential decay weight based on age. Recent items weigh more."""
    if item_time is None:
        return 0.5
    if item_time.tzinfo is None:
        item_time = item_time.replace(tzinfo=timezone.utc)
    age_hours = max(0, (now - item_time).total_seconds() / 3600)
    return math.exp(-0.693 * age_hours / SENTIMENT_HALF_LIFE_HOURS)


def _age_hours(item_time: datetime | None, now: datetime) -> float:
    """Age of item in hours (for recent/older windows)."""
    if item_time is None:
        return 0.0
    if item_time.tzinfo is None:
        item_time = item_time.replace(tzinfo=timezone.utc)
    return max(0, (now - item_time).total_seconds() / 3600)


def _source_weight(source: str | None) -> float:
    """Weight by source reliability (URL substring match)."""
    if not source:
        return DEFAULT_SOURCE_WEIGHT
    lower = source.lower()
    for key, w in SOURCE_WEIGHTS.items():
        if key in lower:
            return w
    return DEFAULT_SOURCE_WEIGHT


def _entity_weight(tag: str) -> float:
    """Weight by entity (e.g. central banks)."""
    return ENTITY_WEIGHTS.get(tag, DEFAULT_ENTITY_WEIGHT)


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
    active_events: set[str] = set()
    for it in items:
        cat = it.category
        if cat not in by_category:
            by_category[cat] = []
        text = f"{it.title or ''} {it.description or ''}"
        event_types = tag_event_types(text)
        if event_types and _age_hours(it.published or it.fetched_at, now) <= SENTIMENT_RECENT_HOURS:
            active_events.update(event_types)
        by_category[cat].append({
            "id": it.id,
            "title": it.title,
            "link": it.link,
            "published": it.published.isoformat() if it.published else None,
            "sentiment_score": it.sentiment_score,
            "asset_tags": it.asset_tags,
            "event_types": event_types,
        })

    # Time-decay weighted sentiment per asset tag; source and entity weighting
    by_asset_weighted: dict[str, list[tuple[float, float]]] = {}  # tag -> [(score, weight)]
    for it in items:
        if it.sentiment_score is not None and it.asset_tags:
            base_w = _decay_weight(it.published or it.fetched_at, now)
            src_w = _source_weight(getattr(it, "source", None))
            for tag in it.asset_tags:
                w = base_w * src_w * _entity_weight(tag)
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

    # Sentiment trend (recent 24h vs older 24–72h) and price trend for divergence (same source/entity weights)
    recent_pairs: dict[str, list[tuple[float, float]]] = {}
    older_pairs: dict[str, list[tuple[float, float]]] = {}
    for it in items:
        if it.sentiment_score is None or not it.asset_tags:
            continue
        age = _age_hours(it.published or it.fetched_at, now)
        base_w = _decay_weight(it.published or it.fetched_at, now)
        src_w = _source_weight(getattr(it, "source", None))
        for tag in it.asset_tags:
            w = base_w * src_w * _entity_weight(tag)
            if age <= SENTIMENT_RECENT_HOURS:
                recent_pairs.setdefault(tag, []).append((it.sentiment_score, w))
            if SENTIMENT_RECENT_HOURS < age <= SENTIMENT_OLDER_MAX_HOURS:
                older_pairs.setdefault(tag, []).append((it.sentiment_score, w))

    def _weighted_avg(pairs: list[tuple[float, float]]) -> float | None:
        if not pairs:
            return None
        tw = sum(w for _, w in pairs)
        if tw <= 0:
            return None
        return sum(s * w for s, w in pairs) / tw

    price_snapshot = get_price_snapshot(extra_tags=set(by_asset_sentiment.keys()))
    previous_price_snapshot = get_previous_price_snapshot(extra_tags=set(by_asset_sentiment.keys()))
    PRICE_CHANGE_THRESHOLD = 0.005  # 0.5% to call up/down

    for tag in list(by_asset_sentiment.keys()):
        info = by_asset_sentiment[tag]
        recent_avg = _weighted_avg(recent_pairs.get(tag, []))
        older_avg = _weighted_avg(older_pairs.get(tag, []))
        current_price = price_snapshot.get(tag)
        prev_price = previous_price_snapshot.get(tag)

        sentiment_trend = "neutral"
        if recent_avg is not None and older_avg is not None:
            if recent_avg > older_avg + 0.05:
                sentiment_trend = "improving"
            elif recent_avg < older_avg - 0.05:
                sentiment_trend = "worsening"

        price_trend = "neutral"
        if current_price is not None and prev_price is not None and prev_price > 0:
            chg = (current_price - prev_price) / prev_price
            if chg > PRICE_CHANGE_THRESHOLD:
                price_trend = "up"
            elif chg < -PRICE_CHANGE_THRESHOLD:
                price_trend = "down"

        divergence = (
            (sentiment_trend == "improving" and price_trend == "down")
            or (sentiment_trend == "worsening" and price_trend == "up")
        )
        info["sentiment_trend"] = sentiment_trend
        info["price_trend"] = price_trend
        info["divergence"] = divergence

    as_of = now
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
        "active_events": sorted(active_events),
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
