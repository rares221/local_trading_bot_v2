"""
RSS fetch and store. Translates codeops-trading-app behaviour: fetch feeds, map to FeedItem shape.
"""
from datetime import datetime, timezone
import re
from typing import Any

import feedparser
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feed_item import FeedItem
from app.services.ingestion.feeds import default_feeds, FeedSpec, FEED_CATEGORIES


def _sanitize_filename(name: str) -> str:
    """Sanitize for use in IDs; single pass (fixes codeops bug)."""
    invalid = set('<>:"/\\|?*')
    result = "".join("_" if c in invalid else c for c in name)
    return result[:100] if len(result) > 100 else result


def _guid(entry: Any, fallback_title: str) -> str:
    """Stable id from entry GUID or fallback."""
    guid = getattr(entry, "id", None) or getattr(entry, "link", None)
    if guid:
        return _sanitize_filename(guid)[:512]
    return _sanitize_filename(f"{datetime.now(timezone.utc).isoformat()}_{fallback_title}")[:512]


def fetch_feed(spec: FeedSpec) -> list[dict]:
    """Fetch one feed; return list of item dicts (title, description, link, published, etc.)."""
    parsed = feedparser.parse(spec.url)
    items = []
    for entry in getattr(parsed, "entries", []):
        title = getattr(entry, "title", "") or ""
        published = getattr(entry, "published_parsed", None)
        if published:
            from time import struct_time
            dt = datetime(*published[:6], tzinfo=timezone.utc) if isinstance(published, struct_time) else datetime.now(timezone.utc)
        else:
            dt = datetime.now(timezone.utc)
        items.append({
            "id": _guid(entry, title),
            "title": title,
            "description": getattr(entry, "summary", "") or "",
            "content": getattr(entry, "content", [{}])[0].get("value", "") if getattr(entry, "content", None) else "",
            "link": getattr(entry, "link", "") or "",
            "published": dt,
            "category": spec.category,
            "source": spec.url,
            "keywords": getattr(entry, "tags", []) and [t.get("term", "") for t in entry.tags if isinstance(t, dict)] or [],
        })
    return items


def fetch_all_feeds() -> list[dict]:
    """Fetch all default feeds; return flat list of item dicts."""
    all_items = []
    for spec in default_feeds():
        try:
            items = fetch_feed(spec)
            for it in items:
                it["source"] = spec.url
            all_items.extend(items)
        except Exception:
            continue
    return all_items


async def save_feed_items(session: AsyncSession, items: list[dict]) -> int:
    """Upsert feed items (by id). Returns count saved."""
    now = datetime.now(timezone.utc)
    saved = 0
    for it in items:
        existing = await session.get(FeedItem, it["id"])
        if existing:
            continue
        feed_item = FeedItem(
            id=it["id"],
            title=it["title"],
            description=it.get("description") or None,
            content=it.get("content") or None,
            link=it["link"],
            published=it["published"],
            category=it["category"],
            source=it.get("source", ""),
            fetched_at=now,
            keywords=it.get("keywords") or None,
            is_processed=False,
            sentiment_score=None,
            asset_tags=None,
        )
        session.add(feed_item)
        saved += 1
    return saved
