"""
Research / rationale (Option B): context from analysis + feed, one-shot LLM summary.
Builds dict: sentiment, buy_in_price, risk_tier, headlines; optionally generates 2–4 sentence rationale.
"""
from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import date, datetime, timedelta, timezone
from app.core.market_time import market_date

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
    # Extract all needed data from analysis.payload NOW as plain Python dicts.
    # The ORM object expires after asyncio.to_thread() calls recycle the connection;
    # accessing analysis.payload later triggers a sync lazy-reload that fails with MissingGreenlet.
    _analysis_payload: dict = {}
    if analysis and analysis.payload:
        _analysis_payload = dict(analysis.payload)  # plain dict, no ORM reference
    by_asset = _analysis_payload.get("by_asset_sentiment") or {}
    _active_events_raw: list = _analysis_payload.get("active_events") or []
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
            expiry_days = max(1, (rec.expiry_date - market_date()).days)
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
        if not symbol and category.lower() in ("stocks_large", "stocks_mid", "stocks_small", "stocks", "etfs", "etf", "stock"):
            symbol = asset_id.upper()
        calculated = False
        if symbol:
            try:
                from app.services.recommendations.engine import (
                    compute_sl_tp_for_symbol as _calc_symbol,
                )
                result = await asyncio.to_thread(_calc_symbol, symbol, risk_tier, direction)
                if result:
                    live_price, sl, tp, snap = result
                    ctx["stop_loss"] = sl
                    ctx["take_profit"] = tp
                    # Use live price as entry if we didn't have one
                    if ctx["buy_in_price"] is None:
                        ctx["buy_in_price"] = live_price
                    ctx["levels_source"] = "calculated"
                    ctx["atr"] = snap.atr if snap and snap.atr else None
                    # Individual indicator values — used by V4b hallucination validator
                    if snap.rsi is not None:
                        ctx["rsi"] = round(snap.rsi, 1)
                    if snap.atr_pct is not None:
                        ctx["atr_pct"] = round(snap.atr_pct * 100, 2)  # stored as % (e.g. 2.3)
                    if snap.adx is not None:
                        ctx["adx"] = round(snap.adx[0], 1)
                    if snap.macd is not None:
                        ctx["macd_histogram"] = round(snap.macd[2], 4)
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
                    if category.lower() in ("stocks_large", "stocks_mid", "stocks_small", "stocks", "etfs", "etf", "stock"):
                        try:
                            from app.services.technicals import compute_relative_strength
                            from app.services.market_context import get_sector_etf_for_asset

                            def _compute_rs_block(sym: str, _asset_id: str) -> dict:
                                _out: dict = {}
                                _rs_20d = compute_relative_strength(sym, "^GSPC", period=20)
                                _rs_60d = compute_relative_strength(sym, "^GSPC", period=60)
                                if _rs_20d is not None:
                                    _out["rs_20d"] = _rs_20d
                                if _rs_60d is not None:
                                    _out["rs_60d"] = _rs_60d
                                _sector_etf = get_sector_etf_for_asset(_asset_id)
                                if _sector_etf:
                                    _sector_rs = compute_relative_strength(sym, _sector_etf, period=20)
                                    if _sector_rs is not None:
                                        _out["sector_rs"] = _sector_rs
                                        _out["sector_etf"] = _sector_etf
                                    _sector_vs_spx = compute_relative_strength(_sector_etf, "^GSPC", period=20)
                                    if _sector_vs_spx is not None:
                                        _out["sector_vs_spx_20d"] = _sector_vs_spx
                                return _out

                            rs_data = await asyncio.to_thread(_compute_rs_block, symbol, asset_id)
                            ctx.update(rs_data)
                        except Exception:
                            pass
                    calculated = True
            except Exception:
                pass
        # Fallback: fetch live price directly if still missing
        if not calculated and not ctx.get("buy_in_price") and symbol:
            try:
                from app.services.market_data import fetch_last_price
                lp = await asyncio.to_thread(fetch_last_price, symbol)
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

    # Asset-specific headlines — filtered at SQL level (asset_tags @> [asset_id]).
    # Near horizon: up to 9 headlines with age; far horizon: up to 5 (no age shown).
    # Fetches 3× the cap to account for rows without titles.
    _max_headlines = 9 if ctx.get("horizon") == "near" else 5
    ctx["headlines"] = []
    ctx["headline_ages"] = []
    try:
        _hq = (
            select(FeedItem)
            .where(FeedItem.asset_tags.contains([asset_id]))
            .order_by(FeedItem.fetched_at.desc())
            .limit(_max_headlines * 3)
        )
        _now_utc = datetime.now(timezone.utc)
        for it in (await session.execute(_hq)).scalars().all():
            if not it.title:
                continue
            ctx["headlines"].append(it.title)
            if it.fetched_at:
                fa = it.fetched_at
                if fa.tzinfo is None:
                    fa = fa.replace(tzinfo=timezone.utc)
                ctx["headline_ages"].append(round((_now_utc - fa).total_seconds() / 3600, 1))
            else:
                ctx["headline_ages"].append(None)
            if len(ctx["headlines"]) >= _max_headlines:
                break
    except Exception:
        await session.rollback()
    # Fundamentals: sector, industry, market cap, P/E, 52-week range via market_data routing
    symbol = get_yf_symbol(category, asset_id)
    if not symbol and category.lower() in ("stocks_large", "stocks_mid", "stocks_small", "stocks", "etfs", "etf", "stock"):
        symbol = asset_id.upper()
    if symbol:
        try:
            from app.services.market_data import fetch_company_profile, fetch_metrics, fetch_next_earnings as _fetch_earnings

            def _fetch_fundamentals(_sym: str, _horizon: str | None, _buy_in: float | None, _expiry_days: int) -> dict:
                _fund: dict = {}
                _profile = fetch_company_profile(_sym)
                if _profile:
                    if _profile.get("sector"):
                        _fund["sector"] = str(_profile["sector"])
                    if _profile.get("industry"):
                        _fund["industry"] = str(_profile["industry"])
                    if _profile.get("market_cap") is not None:
                        _fund["market_cap"] = int(_profile["market_cap"])
                    if _profile.get("name"):
                        _fund["short_name"] = str(_profile["name"])
                _metrics = fetch_metrics(_sym)
                if _metrics:
                    if _metrics.get("pe_ratio") is not None:
                        _fund["pe_ratio"] = _metrics["pe_ratio"]
                    _high52 = _metrics.get("high_52w")
                    _low52 = _metrics.get("low_52w")
                    if _high52 and _low52 and _buy_in and float(_high52) > float(_low52):
                        try:
                            _fund["position_52w"] = round(
                                (float(_buy_in) - float(_low52)) / (float(_high52) - float(_low52)) * 100, 1
                            )
                        except Exception:
                            pass
                if _horizon == "far":
                    try:
                        from datetime import date as _date
                        _today = market_date()
                        _ed_date = _fetch_earnings(_sym)
                        if _ed_date:
                            _dte = (_ed_date - _today).days
                            if _dte >= 0:
                                _fund["days_to_earnings"] = _dte
                                _fund["earnings_in_window"] = _dte <= _expiry_days
                    except Exception:
                        pass
                return _fund

            fund = await asyncio.to_thread(
                _fetch_fundamentals,
                symbol,
                ctx.get("horizon"),
                ctx.get("buy_in_price"),
                int(ctx.get("expiry_days") or 60),
            )
            if fund:
                earnings_in_window = fund.pop("earnings_in_window", None)
                days_to_earnings = fund.pop("days_to_earnings", None)
                if fund:
                    ctx["fundamentals"] = fund
                if days_to_earnings is not None:
                    ctx["days_to_earnings"] = days_to_earnings
                if earnings_in_window is not None:
                    ctx["earnings_in_window"] = earnings_in_window
        except Exception:
            pass
    # Sentiment trend from analysis payload (improving / worsening / neutral)
    # Uses pre-extracted _analysis_payload (plain dict) — avoids ORM lazy-reload after to_thread.
    if _analysis_payload:
        asset_info = by_asset.get(asset_id) or {}
        st = asset_info.get("sentiment_trend")
        if st:
            ctx["sentiment_trend"] = st
        vel = asset_info.get("velocity")
        if vel is not None:
            ctx["sentiment_velocity"] = vel  # positive = improving, negative = worsening
        conv = asset_info.get("conviction")
        if conv is not None:
            ctx["sentiment_conviction"] = conv  # 1.0 = consensus, 0.0 = contradictory
        if _active_events_raw:
            ctx["active_events"] = _active_events_raw  # e.g. ["earnings", "fomc", "nfp"]

    # Market regime + DXY macro (global signals; wrapped in thread to avoid event-loop blocking)
    try:
        from app.services.market_context import detect_market_regime, detect_dxy_macro

        def _fetch_regime_dxy() -> tuple:
            _regime = detect_market_regime()
            _dxy = detect_dxy_macro()
            return _regime, _dxy

        regime, dxy = await asyncio.to_thread(_fetch_regime_dxy)
        ctx["market_regime"] = regime.regime          # risk_on | risk_off | cautious
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

    # Far-horizon extended sentiment windows (5d and 10d simple averages).
    # The 24h decay-weighted avg in sentiment_avg is near-useless on multi-week trades
    # (a 3-day-old article has weight ≈ 0.12); unweighted windows over 5-10 days are
    # a better structural signal.  Only executed when horizon is "far" to avoid the
    # extra DB round-trip on every near-horizon research call.
    if ctx.get("horizon") == "far":
        try:
            _now = datetime.now(timezone.utc)
            _cutoff_5d  = _now - timedelta(days=5)
            _cutoff_10d = _now - timedelta(days=10)
            _q_sent = (
                select(
                    FeedItem.sentiment_score,
                    FeedItem.fetched_at,
                    FeedItem.asset_tags,
                )
                .where(
                    FeedItem.fetched_at >= _cutoff_10d,
                    FeedItem.sentiment_score.isnot(None),
                )
                .limit(500)
            )
            _sent_rows = (await session.execute(_q_sent)).all()
            _scores_5d: list[float] = []
            _scores_10d: list[float] = []
            for _score, _fa, _tags in _sent_rows:
                if not _tags or asset_id not in _tags:
                    continue
                if _fa.tzinfo is None:
                    _fa = _fa.replace(tzinfo=timezone.utc)
                _scores_10d.append(float(_score))
                if _fa >= _cutoff_5d:
                    _scores_5d.append(float(_score))
            if _scores_5d:
                ctx["sentiment_5d_avg"] = round(sum(_scores_5d) / len(_scores_5d), 4)
            if _scores_10d:
                ctx["sentiment_10d_avg"] = round(sum(_scores_10d) / len(_scores_10d), 4)
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
