"""
Research / rationale (Option B): context from analysis + feed, one-shot LLM summary.
Builds dict: sentiment, buy_in_price, risk_tier, headlines; optionally generates 2–4 sentence rationale.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feed_item import FeedItem
from app.services.analysis import get_latest_analysis
from app.services.recommendations.engine import (
    get_asset_price_and_risk,
    get_sentiment_score_and_count,
    get_yf_symbol,
)


async def build_research_context(
    session: AsyncSession,
    asset_id: str,
    category: str,
) -> dict[str, Any]:
    """
    Build context dict for (asset_id, category): sentiment from latest analysis,
    buy_in_price (current or from existing rec), risk_tier, direction, and 1–2 headline snippets.
    """
    from app.models.recommendation import Recommendation

    ctx: dict[str, Any] = {
        "asset_id": asset_id,
        "category": category,
        "sentiment_avg": None,
        "sentiment_count": 0,
        "buy_in_price": None,
        "risk_tier": None,
        "direction": None,
        "headlines": [],
    }
    analysis = await get_latest_analysis(session)
    if analysis and analysis.payload:
        by_asset = (analysis.payload or {}).get("by_asset_sentiment") or {}
        pair = get_sentiment_score_and_count(category, asset_id, by_asset)
        if pair:
            ctx["sentiment_avg"], ctx["sentiment_count"] = pair
    price_risk = get_asset_price_and_risk(category, asset_id)
    if price_risk:
        ctx["buy_in_price"], ctx["risk_tier"] = price_risk

    # Look up direction from the most recent recommendation for this asset
    rec_q = (
        select(Recommendation)
        .where(Recommendation.asset_id == asset_id, Recommendation.asset_category == category)
        .order_by(Recommendation.created_at.desc())
        .limit(1)
    )
    rec_result = await session.execute(rec_q)
    rec = rec_result.scalars().first()
    if rec:
        ctx["direction"] = getattr(rec, "direction", None) or "long"
        if ctx["buy_in_price"] is None and rec.buy_in_price:
            ctx["buy_in_price"] = rec.buy_in_price
        if ctx["risk_tier"] is None and rec.risk_tier:
            ctx["risk_tier"] = rec.risk_tier
        ctx["volatility_notes"] = getattr(rec, "volatility_notes", None)
        ctx["confidence_score"] = getattr(rec, "confidence_score", None)

    # Derive direction from sentiment if no recommendation exists
    if not ctx["direction"]:
        sent = ctx.get("sentiment_avg")
        ctx["direction"] = "short" if (sent is not None and sent < -0.05) else "long"

    # Up to 2 recent feed headlines that mention this asset (asset_id in asset_tags)
    q = select(FeedItem).order_by(FeedItem.fetched_at.desc()).limit(200)
    result = await session.execute(q)
    for it in result.scalars().all():
        if it.asset_tags and asset_id in it.asset_tags:
            ctx["headlines"].append(it.title or "(no title)")
            if len(ctx["headlines"]) >= 2:
                break
    # Optional: yfinance .info for sector, industry, market cap (stocks/ETFs; skip on failure)
    symbol = get_yf_symbol(category, asset_id)
    if symbol:
        try:
            import yfinance as yf
            import logging
            log = logging.getLogger("yfinance")
            old_level = log.level
            log.setLevel(logging.WARNING)
            try:
                info = yf.Ticker(symbol).info
                if isinstance(info, dict):
                    fund: dict[str, Any] = {}
                    if info.get("sector"):
                        fund["sector"] = str(info["sector"])
                    if info.get("industry"):
                        fund["industry"] = str(info["industry"])
                    if info.get("marketCap") is not None:
                        fund["market_cap"] = int(info["marketCap"])
                    if info.get("shortName"):
                        fund["short_name"] = str(info["shortName"])
                    if fund:
                        ctx["fundamentals"] = fund
            finally:
                log.setLevel(old_level)
        except Exception:
            pass
    return ctx


async def generate_rationale(context: dict[str, Any]) -> str | None:
    """
    One-shot LLM call: 2–4 sentences on why this idea might make sense and main risk.
    Uses configured provider (OpenAI, Anthropic, Ollama, OpenRouter) via app.services.research.llm.
    Returns None if provider is unavailable or the call fails.
    """
    from app.services.research.llm import generate_rationale as _generate
    return await _generate(context)


__all__ = ["build_research_context", "generate_rationale"]
