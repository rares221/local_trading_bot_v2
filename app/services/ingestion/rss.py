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


def _feed_item_rows(items: list[dict], fetched_at: datetime) -> list[dict]:
    """Normalize fetch payload to DB row dicts; caller dedupes by id."""
    seen_ids: set[str] = set()
    to_insert: list[dict] = []
    for it in items:
        fid = it["id"]
        if fid in seen_ids:
            continue
        seen_ids.add(fid)
        to_insert.append({
            "id": fid,
            "title": it["title"],
            "description": it.get("description") or None,
            "content": it.get("content") or None,
            "link": it["link"],
            "published": it["published"],
            "category": it["category"],
            "source": it.get("source", ""),
            "fetched_at": fetched_at,
            "keywords": it.get("keywords") or None,
            "is_processed": False,
            "sentiment_score": None,
            "asset_tags": None,
        })
    return to_insert


async def save_feed_items(session: AsyncSession, items: list[dict]) -> int:
    """
    Insert new feed items by id. Skips rows already in the DB and handles races
    (parallel ingest / double pipeline) without raising IntegrityError.
    """
    from sqlalchemy import select as sa_select
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    now = datetime.now(timezone.utc)
    to_insert = _feed_item_rows(items, now)
    if not to_insert:
        return 0

    bind = session.get_bind()
    table = FeedItem.__table__
    ids = [r["id"] for r in to_insert]

    # SQLite default max bind variables is often 999; chunk IN (...) and INSERT batches.
    _id_chunk = 400
    existing_ids: set[str] = set()
    for i in range(0, len(ids), _id_chunk):
        part = ids[i : i + _id_chunk]
        res = await session.execute(sa_select(FeedItem.id).where(FeedItem.id.in_(part)))
        existing_ids.update(row[0] for row in res.fetchall())

    new_rows = [r for r in to_insert if r["id"] not in existing_ids]
    if not new_rows:
        return 0

    if bind.dialect.name == "sqlite":
        _row_chunk = 50  # 50 rows × ~13 cols stays under default 999 SQLite variables
        for i in range(0, len(new_rows), _row_chunk):
            chunk = new_rows[i : i + _row_chunk]
            stmt = sqlite_insert(table).values(chunk)
            stmt = stmt.on_conflict_do_nothing(index_elements=[table.c.id])
            await session.execute(stmt)
        return len(new_rows)

    for row in new_rows:
        session.add(FeedItem(**row))
    return len(new_rows)
