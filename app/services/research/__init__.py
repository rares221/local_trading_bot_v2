"""
Research / rationale (Option B): context from analysis + feed, one-shot LLM summary.
Builds dict: sentiment, buy_in_price, risk_tier, headlines; optionally generates 2–4 sentence rationale.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import date, datetime, timezone

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
        if rec.stop_loss:
            ctx["stop_loss"] = float(rec.stop_loss)
        if rec.take_profit:
            ctx["take_profit"] = float(rec.take_profit)
        # Horizon classification from expiry_date
        if rec.expiry_date:
            from app.services.technicals import classify_horizon
            expiry_days = max(1, (rec.expiry_date - date.today()).days)
            ctx["expiry_days"] = expiry_days
            ctx["horizon"] = classify_horizon(expiry_days)

    # Derive direction from sentiment if no recommendation exists
    if not ctx["direction"]:
        sent = ctx.get("sentiment_avg")
        ctx["direction"] = "short" if (sent is not None and sent < -0.05) else "long"

    # If SL/TP are missing, calculate them using ATR + quant workflow
    if not ctx.get("stop_loss") or not ctx.get("take_profit"):
        risk_tier = ctx.get("risk_tier") or "moderate"
        direction = ctx.get("direction") or "long"
        # get_yf_symbol only works for assets in the universe; fall back to direct ticker
        symbol = get_yf_symbol(category, asset_id)
        if not symbol and category.lower() in ("stocks", "etfs", "etf", "stock"):
            symbol = asset_id.upper()
        calculated = False
        if symbol:
            try:
                from app.services.recommendations.engine import (
                    compute_sl_tp_for_symbol as _calc_symbol,
                )
                result = _calc_symbol(symbol, risk_tier, direction)
                if result:
                    live_price, sl, tp, snap = result
                    ctx["stop_loss"] = sl
                    ctx["take_profit"] = tp
                    # Use live price as entry if we didn't have one
                    if ctx["buy_in_price"] is None:
                        ctx["buy_in_price"] = live_price
                    ctx["levels_source"] = "calculated"
                    ctx["atr"] = snap.atr if snap and snap.atr else None
                    # Horizon from estimated expiry (only if not already set from rec)
                    if "expiry_days" not in ctx and live_price and tp:
                        from app.services.technicals import classify_horizon, estimate_expiry_days
                        ed = estimate_expiry_days(live_price, tp, snapshot=snap)
                        ctx["expiry_days"] = ed
                        ctx["horizon"] = classify_horizon(ed)
                    # Volume Profile fields from snapshot
                    if snap and snap.vpoc is not None:
                        ctx["vpoc"] = snap.vpoc
                        ctx["vah"] = snap.vah
                        ctx["val"] = snap.val
                        ctx["vp_bias"] = snap.vp_bias
                        ctx["vp_overextension"] = snap.vp_overextension
                        ctx["in_value_area"] = snap.in_value_area()
                    # Relative Strength vs SPX and sector ETF (stocks only)
                    if category.lower() in ("stocks", "etfs", "etf", "stock"):
                        try:
                            from app.services.technicals import compute_relative_strength
                            from app.services.market_context import get_sector_etf_for_asset
                            rs_20d = compute_relative_strength(symbol, "^GSPC", period=20)
                            rs_60d = compute_relative_strength(symbol, "^GSPC", period=60)
                            if rs_20d is not None:
                                ctx["rs_20d"] = rs_20d
                            if rs_60d is not None:
                                ctx["rs_60d"] = rs_60d
                            sector_etf = get_sector_etf_for_asset(asset_id)
                            if sector_etf:
                                sector_rs = compute_relative_strength(symbol, sector_etf, period=20)
                                if sector_rs is not None:
                                    ctx["sector_rs"] = sector_rs
                                    ctx["sector_etf"] = sector_etf
                        except Exception:
                            pass
                    calculated = True
            except Exception:
                pass
        # Fallback: fetch live price directly if still missing
        if not calculated and not ctx.get("buy_in_price") and symbol:
            try:
                import yfinance as yf
                fi = yf.Ticker(symbol).fast_info
                lp = getattr(fi, "last_price", None) or getattr(fi, "lastPrice", None)
                if lp:
                    ctx["buy_in_price"] = float(lp)
            except Exception:
                pass

        if not calculated and ctx.get("buy_in_price"):
            try:
                from app.services.technicals import compute_atr_levels
                sl, tp = compute_atr_levels(
                    float(ctx["buy_in_price"]), risk_tier, direction=direction
                )
                ctx["stop_loss"] = sl
                ctx["take_profit"] = tp
                ctx["levels_source"] = "calculated"
                calculated = True
            except Exception:
                pass
        if not calculated:
            ctx["levels_source"] = "unavailable"
    else:
        ctx["levels_source"] = "recommendation"

    # Up to 3 recent feed headlines with ages (for near-horizon LLM context)
    ctx["headline_ages"] = []   # parallel list: hours since fetched (float or None)
    q = select(FeedItem).order_by(FeedItem.fetched_at.desc()).limit(200)
    result = await session.execute(q)
    _now_utc = datetime.now(timezone.utc)
    for it in result.scalars().all():
        if it.asset_tags and asset_id in it.asset_tags:
            ctx["headlines"].append(it.title or "(no title)")
            if it.fetched_at:
                fa = it.fetched_at
                if fa.tzinfo is None:
                    fa = fa.replace(tzinfo=timezone.utc)
                ctx["headline_ages"].append(round((_now_utc - fa).total_seconds() / 3600, 1))
            else:
                ctx["headline_ages"].append(None)
            if len(ctx["headlines"]) >= 3:
                break
    # Optional: yfinance .info for sector, industry, market cap (stocks/ETFs; skip on failure)
    symbol = get_yf_symbol(category, asset_id)
    if not symbol and category.lower() in ("stocks", "etfs", "etf", "stock"):
        symbol = asset_id.upper()
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
                    # P/E + 52-week position — used for far-horizon LLM context
                    if info.get("trailingPE") is not None:
                        try:
                            fund["pe_ratio"] = round(float(info["trailingPE"]), 1)
                        except Exception:
                            pass
                    high52 = info.get("fiftyTwoWeekHigh")
                    low52 = info.get("fiftyTwoWeekLow")
                    buy_in = ctx.get("buy_in_price")
                    if high52 and low52 and buy_in and float(high52) > float(low52):
                        try:
                            fund["position_52w"] = round(
                                (float(buy_in) - float(low52)) / (float(high52) - float(low52)) * 100, 1
                            )
                        except Exception:
                            pass
                    if fund:
                        ctx["fundamentals"] = fund
            finally:
                log.setLevel(old_level)
        except Exception:
            pass
    # Sentiment trend from analysis payload (improving / worsening / neutral)
    if analysis and analysis.payload:
        by_asset_s = (analysis.payload or {}).get("by_asset_sentiment") or {}
        asset_info = by_asset_s.get(asset_id) or {}
        st = asset_info.get("sentiment_trend")
        if st:
            ctx["sentiment_trend"] = st

    # Market regime + DXY macro (global signals; sync calls reuse cached yfinance data)
    try:
        from app.services.market_context import detect_market_regime, detect_dxy_macro
        regime = detect_market_regime()
        ctx["market_regime"] = regime.regime          # risk_on | risk_off | cautious
        dxy = detect_dxy_macro()
        if dxy.available:
            ctx["dxy_trend"] = dxy.trend              # bullish | bearish | neutral
            ctx["dxy_change_5d"] = dxy.change_5d_pct
            ctx["dxy_change_20d"] = dxy.change_20d_pct
    except Exception:
        pass

    # Upcoming macro events in the trade window (FOMC, CPI, NFP)
    _expiry_days = int(ctx.get("expiry_days") or 14)
    try:
        from app.services.macro_calendar import get_upcoming_events
        ctx["upcoming_events"] = get_upcoming_events(days_ahead=_expiry_days)
    except Exception:
        ctx["upcoming_events"] = []

    return ctx


async def generate_rationale(context: dict[str, Any]) -> str | None:
    """
    One-shot LLM call: 2–4 sentences on why this idea might make sense and main risk.
    Uses configured provider (OpenAI, Anthropic, Ollama, OpenRouter) via app.services.research.llm.
    Returns None if provider is unavailable or the call fails.
    """
    from app.services.research.llm import generate_rationale as _generate
    return await _generate(context)


async def capture_metrics_at_entry(
    recommendation_id: str,
    active_trade_id: str,
    asset_id: str,
    asset_category: str,
    risk_tier: str,
    direction: str,
    sentiment_avg: float | None = None,
) -> None:
    """Fire-and-forget wrapper — call via asyncio.create_task."""
    from app.services.research.llm import _capture_metrics_at_entry_async
    await _capture_metrics_at_entry_async(
        recommendation_id, active_trade_id, asset_id, asset_category,
        risk_tier, direction, sentiment_avg,
    )


__all__ = ["build_research_context", "generate_rationale", "capture_metrics_at_entry"]
