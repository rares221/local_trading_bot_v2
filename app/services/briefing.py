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
    """Evaluate checklist, generate LLM narrative, save to daily_briefings, return the row."""
    from app.models.recommendation import ActiveTrade, Recommendation
    items = await evaluate_checklist(session)
    content = format_briefing(items)

    # Count today's recommendations by direction for narrative context
    batch_id = date.today().isoformat()
    recs_q = await session.execute(
        select(Recommendation).where(Recommendation.batch_id == batch_id)
    )
    recs_today = recs_q.scalars().all()
    recs_long  = sum(1 for r in recs_today if getattr(r, "direction", "long") == "long")
    recs_short = sum(1 for r in recs_today if getattr(r, "direction", "long") == "short")

    # Count open trades
    trades_q = await session.execute(
        select(ActiveTrade).where(ActiveTrade.outcome.is_(None))
    )
    active_count = len(trades_q.scalars().all())

    # Fetch market regime and DXY from market_context (cached yfinance, non-fatal)
    market_regime: str | None = None
    dxy_trend: str | None = None
    dxy_change_5d: float | None = None
    try:
        from app.services.market_context import detect_market_regime, detect_dxy_macro
        import asyncio
        _regime = await asyncio.to_thread(detect_market_regime)
        market_regime = _regime.regime
        _dxy = await asyncio.to_thread(detect_dxy_macro)
        if _dxy.available:
            dxy_trend = _dxy.trend
            dxy_change_5d = _dxy.change_5d_pct
    except Exception:
        pass

    # P18: LLM narrative (fire-and-forget style — non-fatal if fails)
    narrative: str | None = None
    try:
        from app.services.research.llm import generate_brief_narrative
        narrative = await generate_brief_narrative(
            checklist_items=items,
            recs_count=len(recs_today),
            recs_long=recs_long,
            recs_short=recs_short,
            active_trades_count=active_count,
            market_regime=market_regime,
            dxy_trend=dxy_trend,
            dxy_change_5d=dxy_change_5d,
        )
    except Exception:
        pass

    now = datetime.now(timezone.utc)
    payload: dict = {"items": items}
    if narrative:
        payload["narrative"] = narrative

    row = DailyBriefing(
        id=str(uuid4()),
        created_at=now,
        content_text=content,
        payload=payload,
    )
    session.add(row)
    return row
