"""
Daily briefing: configurable checklist (SPX/VIX/commodities, recs today) run once per day.
Produces a short text summary stored in daily_briefings; optional API and later Telegram.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yfinance as yf
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.daily_briefing import DailyBriefing
from app.models.recommendation import Recommendation

DEFAULT_CHECKLIST = [
    {"id": "spx_move", "label": "S&P 500 move", "type": "index_move", "symbol": "^GSPC", "threshold_pct": 1.5},
    {"id": "vix_level", "label": "VIX level", "type": "level", "symbol": "^VIX", "above": 20},
    {"id": "oil_wti", "label": "WTI crude", "type": "price", "symbol": "CL=F"},
    {"id": "oil_brent", "label": "Brent crude", "type": "price", "symbol": "BZ=F"},
    {"id": "gold", "label": "Gold", "type": "price", "symbol": "GC=F"},
    {"id": "silver", "label": "Silver", "type": "price", "symbol": "SI=F"},
    {"id": "recs_today", "label": "Recommendations today", "type": "recs_count"},
]


def load_checklist() -> list[dict[str, Any]]:
    """Load checklist from data/briefing_checklist.json or return default."""
    path = Path(settings.data_dir) / "briefing_checklist.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass
    return DEFAULT_CHECKLIST


def _fetch_price(symbol: str) -> float | None:
    try:
        log = logging.getLogger("yfinance")
        old = log.level
        log.setLevel(logging.WARNING)
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="5d")
            if hist is None or hist.empty or "Close" not in hist.columns:
                return None
            return float(hist["Close"].iloc[-1])
        finally:
            log.setLevel(old)
    except Exception:
        return None


def _fetch_change_pct(symbol: str) -> tuple[float | None, float | None]:
    """Return (current_close, 1d_change_pct) or (None, None)."""
    try:
        log = logging.getLogger("yfinance")
        old = log.level
        log.setLevel(logging.WARNING)
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="5d")
            if hist is None or hist.empty or len(hist) < 2 or "Close" not in hist.columns:
                return None, None
            close = hist["Close"]
            curr = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            if prev == 0:
                return curr, None
            return curr, round((curr - prev) / prev * 100, 2)
        finally:
            log.setLevel(old)
    except Exception:
        return None, None


def _evaluate_item(item: dict[str, Any], session: AsyncSession | None) -> dict[str, Any]:
    """Evaluate one checklist item. Returns { id, label, result, highlight }."""
    item_id = item.get("id", "")
    label = item.get("label", item_id)
    itype = item.get("type", "price")
    result: str
    highlight = False

    if itype == "index_move":
        symbol = item.get("symbol", "")
        threshold = float(item.get("threshold_pct", 1.0))
        curr, ch_pct = _fetch_change_pct(symbol)
        if curr is not None and ch_pct is not None:
            result = f"{ch_pct:+.2f}% (close {curr:.2f})"
            if abs(ch_pct) >= threshold:
                highlight = True
        else:
            result = "no data"

    elif itype == "level":
        symbol = item.get("symbol", "")
        above = item.get("above")
        below = item.get("below")
        price = _fetch_price(symbol)
        if price is not None:
            result = f"{price:.2f}"
            if above is not None and price > float(above):
                highlight = True
                result += f" (above {above})"
            elif below is not None and price < float(below):
                highlight = True
                result += f" (below {below})"
        else:
            result = "no data"

    elif itype == "price":
        symbol = item.get("symbol", "")
        price = _fetch_price(symbol)
        result = f"{price:.2f}" if price is not None else "no data"

    else:
        # recs_count is handled in async _evaluate_one
        result = "—"

    return {"id": item_id, "label": label, "result": result, "highlight": highlight}


async def _evaluate_one(item: dict[str, Any], session: AsyncSession | None) -> dict[str, Any]:
    if item.get("type") == "recs_count" and session is not None:
        batch_id = date.today().isoformat()
        q = select(Recommendation).where(Recommendation.batch_id == batch_id)
        r = await session.execute(q)
        count = len(r.scalars().all())
        return {
            "id": item.get("id", ""),
            "label": item.get("label", ""),
            "result": f"{count} recs",
            "highlight": count > 0,
        }
    return _evaluate_item(item, session)


async def evaluate_checklist(session: AsyncSession | None) -> list[dict[str, Any]]:
    """Run all checklist items and return list of { id, label, result, highlight }."""
    checklist = load_checklist()
    return [await _evaluate_one(item, session) for item in checklist]


def format_briefing(items: list[dict[str, Any]]) -> str:
    """Turn checklist results into a short text summary."""
    lines: list[str] = []
    for x in items:
        mark = " *" if x.get("highlight") else ""
        lines.append(f"- {x.get('label', '')}: {x.get('result', '')}{mark}")
    return "\n".join(lines) if lines else "No checklist items."


async def run_briefing(session: AsyncSession) -> DailyBriefing:
    """Evaluate checklist, format, save to daily_briefings, return the row."""
    items = await evaluate_checklist(session)
    # Fix: evaluate_checklist currently calls _evaluate_item for non-recs items; _evaluate_item is sync.
    # So we need evaluate_checklist to actually run _evaluate_item for non-DB items. Let me fix evaluate_checklist to do both sync and async parts.
    # I already have _evaluate_item_async that only does recs_count async and delegates rest to _evaluate_item. So evaluate_checklist should use _evaluate_item_async. Let me fix that.
    content = format_briefing(items)
    now = datetime.now(timezone.utc)
    row = DailyBriefing(
        id=str(uuid4()),
        created_at=now,
        content_text=content,
        payload={"items": items},
    )
    session.add(row)
    return row
