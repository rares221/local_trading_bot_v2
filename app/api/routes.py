"""
API routes: analysis, recommendations, feedback (Took it/Skipped), active trades.
All user-facing responses include disclaimer per TRADING_APP_PLAN.
"""
import asyncio
import logging
from datetime import datetime, timezone, date
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.recommendation import Recommendation, ActiveTrade, ActiveTradeLevel
from app.models.daily_briefing import DailyBriefing

logger = logging.getLogger(__name__)

# Module-level cache for yfinance short names (persists for process lifetime; names rarely change)
_yf_short_name_cache: dict[str, str] = {}

def _get_short_name(symbol: str | None, asset_id: str) -> str | None:
    """Fetch instrument short name from yfinance with in-memory caching. Never raises."""
    if not symbol:
        symbol = asset_id.upper()
    if symbol in _yf_short_name_cache:
        return _yf_short_name_cache[symbol]
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).fast_info
        name = getattr(info, "description", None) or None
        # fast_info doesn't have shortName; fall back to .info (cached by yfinance internally)
        if not name:
            full = yf.Ticker(symbol).info
            name = full.get("shortName") or full.get("longName") or None
        if name:
            _yf_short_name_cache[symbol] = name
        return name
    except Exception:
        return None

DISCLAIMER = (
    "This analysis is for educational and research purposes only. Not financial advice. "
    "Past performance does not guarantee future results. Consult qualified financial professionals "
    "before making investment decisions. All investments carry risk of loss."
)

router = APIRouter()


def _with_disclaimer(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    out["disclaimer"] = DISCLAIMER
    return out


@router.get("/analysis/latest")
async def get_latest_analysis(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Return latest market analysis report (Phase 1 output)."""
    from app.services.analysis import get_latest_analysis as get_analysis
    analysis = await get_analysis(db)
    if not analysis:
        raise HTTPException(status_code=404, detail="No market analysis yet. Run ingestion and analysis job first.")
    return _with_disclaimer({
        "id": analysis.id,
        "as_of": analysis.as_of.isoformat(),
        "sources_used": analysis.sources_used,
        "summary": analysis.summary,
        "payload": analysis.payload,
    })


@router.get("/research/providers")
async def list_research_providers() -> dict[str, Any]:
    """
    List LLM providers available for research rationale (OpenAI, Anthropic, Ollama, OpenRouter).
    Returns current provider and model from config so UI can show selection.
    """
    from app.services.research.providers import PROVIDERS, get_provider_by_id, is_available
    from app.services.research.llm import _get_api_key_for_provider
    from app.core.config import settings

    providers = [
        {
            "id": p.id,
            "display_name": p.display_name,
            "default_model": p.default_model,
            "requires_api_key": p.api_key_env_var is not None,
            "available": is_available(p.id, _get_api_key_for_provider(p.id)),
        }
        for p in PROVIDERS
    ]
    model = (settings.research_llm_model or "").strip()
    p = get_provider_by_id(settings.research_llm_provider or "")
    if p and not model:
        model = p.default_model
    return _with_disclaimer({
        "providers": providers,
        "current": {"provider": settings.research_llm_provider or "", "model": model or ""},
    })


@router.get("/research/resolve")
async def resolve_company(
    q: str,
) -> dict[str, Any]:
    """
    Resolve company name (or partial) to ticker(s). Uses COMPANY_TO_TICKER.
    Returns { "query": "...", "matches": [ { "name": "...", "ticker": "..." }, ... ] }.
    """
    from app.services.sentiment.ticker_detection import resolve_company_to_ticker
    query = (q or "").strip()
    matches = resolve_company_to_ticker(query) if query else []
    return _with_disclaimer({"query": query, "matches": matches})


@router.get("/research/summary")
async def get_research_summary(
    asset_id: str,
    category: str,
    generate: bool = True,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Build research context for (asset_id, category) and optionally generate LLM rationale.
    Returns { "context": {...}, "summary": "…" }. Summary is None if no API key or generate=False.
    """
    from app.services.research import build_research_context, generate_rationale

    context = await build_research_context(db, asset_id, category)
    summary = await generate_rationale(context) if generate else None
    return _with_disclaimer({"context": context, "summary": summary})


@router.get("/briefing/latest")
async def get_latest_briefing(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Return the most recent daily briefing (if any)."""
    from sqlalchemy import desc
    q = select(DailyBriefing).order_by(desc(DailyBriefing.created_at)).limit(1)
    result = await db.execute(q)
    row = result.scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail="No briefing yet. Run the daily briefing job or POST /api/briefing/run.")
    return _with_disclaimer({
        "id": row.id,
        "created_at": row.created_at.isoformat(),
        "content_text": row.content_text,
        "payload": row.payload,
    })


@router.get("/briefing/sparklines")
async def get_briefing_sparklines() -> dict[str, Any]:
    """Return 30-day close price history for key briefing tickers."""
    import yfinance as yf

    TICKERS = {
        "spx":    "^GSPC",
        "vix":    "^VIX",
        "wti":    "CL=F",
        "brent":  "BZ=F",
        "gold":   "GC=F",
        "silver": "SI=F",
    }

    def _fetch() -> dict[str, list[float]]:
        result: dict[str, list[float]] = {}
        for key, ticker in TICKERS.items():
            try:
                hist = yf.Ticker(ticker).history(period="30d")
                if not hist.empty:
                    result[key] = [round(float(v), 4) for v in hist["Close"].dropna().tolist()]
            except Exception:
                pass
        return result

    data = await asyncio.to_thread(_fetch)
    return data


@router.get("/briefing/checklist")
async def get_briefing_checklist() -> dict[str, Any]:
    """Return the configured briefing checklist (for UI or editing)."""
    from app.services.briefing import load_checklist
    return _with_disclaimer({"checklist": load_checklist()})


@router.post("/briefing/run")
async def run_briefing_now(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Run the daily briefing checklist now and return the new briefing."""
    from app.services.briefing import run_briefing
    row = await run_briefing(db)
    await db.commit()
    return _with_disclaimer({
        "id": row.id,
        "created_at": row.created_at.isoformat(),
        "content_text": row.content_text,
        "payload": row.payload,
    })


def _confidence_bucket(score: int | None) -> str:
    """Map 0–100 confidence to bucket for UI (empirical hit rate can be added later)."""
    if score is None:
        return "low"
    if score < 30:
        return "low"
    if score < 50:
        return "medium"
    if score < 70:
        return "high"
    return "very_high"


def _rec_to_item(r: Recommendation) -> dict:
    from app.services.calibration import apply_platt_calibration, get_platt_params
    from app.services.recommendations.asset_class_policy import (
        engine_policy_to_dict,
        get_asset_class_policy,
    )
    from app.services.recommendations.engine import get_yf_symbol as _get_yf_sym
    from app.jobs.scheduler import _vp_by_symbol, _rs_by_symbol
    _sym = _get_yf_sym(r.asset_category or "", r.asset_id or "")
    _vp = _vp_by_symbol.get(_sym, {}) if _sym else {}
    _rs = _rs_by_symbol.get(_sym, {}) if _sym else {}

    score = getattr(r, "confidence_score", None)
    
    # Add calibrated confidence if Platt is fitted
    platt_a, platt_b = get_platt_params()
    confidence_calibrated = None
    if score is not None and platt_a is not None and platt_b is not None:
        # Return as percentage (0-100) for consistency with raw score
        confidence_calibrated = round(apply_platt_calibration(score) * 100, 1)
    
    item = {
        "id": r.id,
        "created_at": r.created_at.isoformat(),
        "batch_id": r.batch_id,
        "asset_category": r.asset_category,
        "asset_id": r.asset_id,
        "buy_in_price": r.buy_in_price,
        "stop_loss": r.stop_loss,
        "take_profit": r.take_profit,
        "expiry_date": r.expiry_date.isoformat() if r.expiry_date else None,
        "risk_tier": r.risk_tier,
        "direction": getattr(r, "direction", "long") or "long",
        "confidence_score": score,
        "confidence_calibrated": confidence_calibrated,
        "confidence_bucket": _confidence_bucket(score),
        "risk_reward": getattr(r, "risk_reward", None),
        "suggested_risk_pct": getattr(r, "suggested_risk_pct", None),
        "volatility_notes": getattr(r, "volatility_notes", None),
        "status": r.status,
        "rationale_text": getattr(r, "rationale_text", None),
        "min_confidence_gate": getattr(r, "min_confidence_gate", None),
        "shadow_b_threshold": getattr(r, "shadow_b_threshold", None),
        "shadow_b_pass": getattr(r, "shadow_b_pass", None),
        "instrument_type": getattr(r, "instrument_type", None),
        "near_earnings": getattr(r, "near_earnings", False),
        "days_to_earnings": getattr(r, "days_to_earnings", None),
        "gap_risk_level": getattr(r, "gap_risk_level", "low"),
        "gap_risk_note": getattr(r, "gap_risk_note", ""),
        "vpoc": _vp.get("vpoc"),
        "vah": _vp.get("vah"),
        "val": _vp.get("val"),
        "vp_bias": _vp.get("vp_bias", "at_vpoc"),
        "vp_overextension": _vp.get("vp_overextension"),
        "in_value_area": _vp.get("in_value_area", False),
        "rs_20d": _rs.get("rs_20d"),
        "rs_60d": _rs.get("rs_60d"),
        "sector_rs": _rs.get("sector_rs"),
        "engine_policy": engine_policy_to_dict(
            get_asset_class_policy(r.asset_category or "", r.asset_id or "")
        ),
    }
    return item


@router.get("/recommendations")
async def list_recommendations(
    batch_id: str | None = None,
    status: str | None = None,
    asset_category: str | None = None,
    group_by: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    List recommendations. Optional: batch_id, status, asset_category (stocks|commodities|other).
    Use group_by=category to get { stocks: [...], commodities: [...], other: [...], indices: [...] } for the recommendation page.
    Each item includes ``engine_policy``: per–asset-class tuning (regime scales, R:R floor, news boost, technicals style) from the live policy layer.
    When group_by=category, defaults to today's batch and deduplicates by (category, asset_id).
    Includes macro_overlay when FOMC/CPI/NFP window applies (same rule as batch confidence penalty).
    """
    from app.services.macro_calendar import get_macro_overlay_info

    macro_overlay = get_macro_overlay_info()
    # Default to today's batch so the Recommendations page shows one set of ideas
    if batch_id is None and group_by == "category":
        batch_id = date.today().isoformat()
    q = select(Recommendation).order_by(Recommendation.created_at.desc())
    if batch_id:
        q = q.where(Recommendation.batch_id == batch_id)
    if status:
        q = q.where(Recommendation.status == status)
    if asset_category:
        q = q.where(Recommendation.asset_category == asset_category)
    q = q.limit(100)
    result = await db.execute(q)
    recs = result.scalars().all()
    # Deduplicate by (asset_category, asset_id) so each asset appears once (keep newest)
    seen_key: set[tuple[str, str]] = set()
    recs_deduped: list[Recommendation] = []
    for r in recs:
        key = (r.asset_category or "", r.asset_id or "")
        if key in seen_key:
            continue
        seen_key.add(key)
        recs_deduped.append(r)
    items = [_rec_to_item(r) for r in recs_deduped]
    if group_by == "category":
        grouped: dict[str, list] = {"stocks": [], "commodities": [], "other": [], "indices": []}
        # Attach current price for each rec (from latest snapshot)
        price_snapshot: dict[str, float] = {}
        if recs_deduped:
            from app.services.recommendations.engine import snapshot_prices_for_recommendations

            price_snapshot = await asyncio.to_thread(
                snapshot_prices_for_recommendations,
                recs_deduped,
            )
        for r in recs_deduped:
            cat = r.asset_category if r.asset_category in grouped else "other"
            if cat not in grouped:
                grouped[cat] = []
            item = _rec_to_item(r)
            item["current_price"] = price_snapshot.get(r.asset_id or "") if price_snapshot else None
            grouped[cat].append(item)
        return _with_disclaimer({"by_category": grouped, "items": items, "macro_overlay": macro_overlay})
    return _with_disclaimer({"items": items, "macro_overlay": macro_overlay})


@router.get("/recommendations/export")
async def export_recommendations_csv(
    batch_id: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Export recommendations as CSV. Defaults to today's batch.
    Optional filters: batch_id (YYYY-MM-DD), status (pending|taken|skipped).
    """
    from fastapi.responses import StreamingResponse
    import io
    import csv

    # Default to today's batch
    if batch_id is None:
        batch_id = date.today().isoformat()

    q = select(Recommendation).order_by(Recommendation.created_at.desc())
    if batch_id:
        q = q.where(Recommendation.batch_id == batch_id)
    if status:
        q = q.where(Recommendation.status == status)
    q = q.limit(200)
    result = await db.execute(q)
    recs = result.scalars().all()

    # Build CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header row
    writer.writerow([
        "Asset", "Category", "Direction", "Entry", "Stop Loss", "Take Profit",
        "R:R", "Confidence", "Bucket", "Size %", "Risk Tier", "Expiry",
        "Status", "Batch", "Created At",
        "MinConfGate", "ShadowBThreshold", "ShadowBPass",
    ])
    
    # Data rows
    for r in recs:
        score = getattr(r, "confidence_score", None)
        sbp = getattr(r, "shadow_b_pass", None)
        writer.writerow([
            r.asset_id,
            r.asset_category,
            getattr(r, "direction", "long") or "long",
            r.buy_in_price,
            r.stop_loss,
            r.take_profit,
            getattr(r, "risk_reward", None),
            score,
            _confidence_bucket(score),
            getattr(r, "suggested_risk_pct", None),
            r.risk_tier,
            r.expiry_date.isoformat() if r.expiry_date else None,
            r.status,
            r.batch_id,
            r.created_at.isoformat() if r.created_at else None,
            getattr(r, "min_confidence_gate", None),
            getattr(r, "shadow_b_threshold", None),
            sbp if sbp is None else ("1" if sbp else "0"),
        ])

    output.seek(0)
    filename = f"recommendations_{batch_id}.csv"
    
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


class TakeTradeRequest(BaseModel):
    """Optional body for POST took-it (trade builder). Default strategy = follow recommendation as-is."""
    strategy: str = "vanilla"  # see app.services.trade_strategies.VALID_STRATEGIES


@router.get("/recommendations/item/{recommendation_id}")
async def get_recommendation_item(
    recommendation_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Single recommendation for trade builder + current price snapshot."""
    rec = await db.get(Recommendation, recommendation_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    from app.services.recommendations.engine import snapshot_prices_for_recommendations

    item = _rec_to_item(rec)
    prices = await asyncio.to_thread(snapshot_prices_for_recommendations, [rec])
    item["current_price"] = prices.get(rec.asset_id or "") if prices else None
    return _with_disclaimer({"item": item})


@router.post("/recommendations/{recommendation_id}/took-it")
async def mark_took_it(
    recommendation_id: str,
    db: AsyncSession = Depends(get_db),
    body: TakeTradeRequest | None = Body(default=None),
) -> dict[str, Any]:
    """Mark recommendation as taken; create one or more active trades (vanilla or split TP)."""
    from app.services.trade_strategies import legs_for_strategy, normalize_strategy

    rec = await db.get(Recommendation, recommendation_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    if rec.status != "pending":
        raise HTTPException(status_code=400, detail=f"Recommendation already {rec.status}")
    try:
        strategy = normalize_strategy((body or TakeTradeRequest()).strategy)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    legs = legs_for_strategy(
        strategy,
        rec.buy_in_price,
        rec.stop_loss,
        rec.take_profit,
        getattr(rec, "direction", "long") or "long",
    )
    rec.status = "taken"
    now = datetime.now(timezone.utc)
    created_ids: list[str] = []
    first_active_id: str | None = None
    for leg in legs:
        active = ActiveTrade(
            recommendation_id=rec.id,
            user_taken_at=now,
            current_sl=rec.stop_loss,
            current_tp=leg["take_profit"],
            expiry=rec.expiry_date,
            last_updated=now,
            strategy_leg=leg["strategy_leg"],
            position_fraction=leg["position_fraction"],
        )
        db.add(active)
        await db.flush()
        created_ids.append(active.id)
        if first_active_id is None:
            first_active_id = active.id
        level = ActiveTradeLevel(
            active_trade_id=active.id,
            sl=rec.stop_loss,
            tp=leg["take_profit"],
            created_at=now,
            source="initial",
        )
        db.add(level)
    await db.commit()

    # P8: capture live metrics snapshot into llm_rationale_log (fire-and-forget)
    if first_active_id:
        from app.services.research import capture_metrics_at_entry
        asyncio.create_task(capture_metrics_at_entry(
            recommendation_id=rec.id,
            active_trade_id=first_active_id,
            asset_id=rec.asset_id or "",
            asset_category=rec.asset_category or "",
            risk_tier=rec.risk_tier or "moderate",
            direction=getattr(rec, "direction", "long") or "long",
            sentiment_avg=None,
        ))
    return _with_disclaimer({
        "ok": True,
        "recommendation_id": recommendation_id,
        "active_trade_created": True,
        "active_trade_ids": created_ids,
        "strategy": strategy,
    })


class TradeBuilderExplainRequest(BaseModel):
    recommendation_id: str
    strategy: str = "vanilla"


class TradeBuilderFromAssetRequest(BaseModel):
    """
    Create a pending recommendation for trade builder from a specific symbol (even if it is not on today's list).

    By default the server uses **live prices/indicators** plus the **latest stored analysis** payload
    (sentiment + event tags) if any — same inputs the daily batch uses, but only for this one asset.
    That does **not** rerun ingestion or the recommendation batch.

    If ``run_full_pipeline`` is true, the app runs **ingestion + sentiment** and **analysis** jobs first
    (same as the first two stages of Dashboard → Run full pipeline), then builds from the refreshed payload.
    It still does **not** rerun the daily batch; use the Dashboard action if you want today's full list regenerated.
    """
    asset_id: str
    category: str = "stocks"
    run_full_pipeline: bool = False


@router.post("/trade-builder/from-asset")
async def trade_builder_from_asset(
    body: TradeBuilderFromAssetRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create a pending Recommendation from asset + category; open trade builder with returned ``recommendation_id``."""
    from functools import partial

    from app.core.preferences import get_preferred_risk
    from app.jobs.scheduler import job_ingest_and_sentiment, job_run_analysis
    from app.services.analysis import get_latest_analysis as fetch_latest_analysis
    from app.services.recommendations.engine import build_trade_builder_candidate
    from app.services.research import build_research_context, generate_rationale

    if body.run_full_pipeline:
        await job_ingest_and_sentiment()
        await job_run_analysis()

    analysis = await fetch_latest_analysis(db)
    payload = (analysis.payload or {}) if analysis else {}
    by_asset_sentiment = payload.get("by_asset_sentiment")
    active_events = payload.get("active_events")

    preferred = get_preferred_risk()
    _build = partial(
        build_trade_builder_candidate,
        body.asset_id.strip(),
        (body.category or "stocks").strip(),
        by_asset_sentiment=by_asset_sentiment,
        preferred_risk=preferred,
        active_events=active_events if isinstance(active_events, list) else None,
    )
    cand, err = await asyncio.to_thread(_build)
    if err or not cand:
        msg = err or "Could not build plan"
        logger.warning(
            "trade_builder_from_asset failed: %s (asset_id=%r category=%r)",
            msg,
            body.asset_id.strip(),
            (body.category or "stocks").strip(),
        )
        raise HTTPException(status_code=400, detail=msg)

    _reg_note = getattr(cand, "market_regime_note", None)
    _vol = cand.volatility_notes
    if _reg_note and _vol:
        _combined_notes = f"{_reg_note}; {_vol}"
    elif _reg_note:
        _combined_notes = _reg_note
    else:
        _combined_notes = _vol

    rationale_text = None
    try:
        from datetime import date as _date
        from app.services.technicals import classify_horizon as _classify_horizon
        ctx = await build_research_context(db, cand.asset_id, cand.asset_category)
        ctx["buy_in_price"] = cand.buy_in_price
        ctx["direction"] = cand.direction
        if cand.expiry_date and "expiry_days" not in ctx:
            _ed = max(1, (cand.expiry_date - _date.today()).days)
            ctx["expiry_days"] = _ed
            ctx["horizon"] = _classify_horizon(_ed)
        rationale_text = await generate_rationale(ctx)
    except Exception:
        await db.rollback()

    now = datetime.now(timezone.utc)
    batch_id = date.today().isoformat()
    rec = Recommendation(
        id=str(uuid4()),
        created_at=now,
        batch_id=batch_id,
        asset_category=cand.asset_category,
        asset_id=cand.asset_id,
        buy_in_price=cand.buy_in_price,
        stop_loss=cand.stop_loss,
        take_profit=cand.take_profit,
        expiry_date=cand.expiry_date,
        rationale_ref=analysis.id if analysis else None,
        rationale_text=rationale_text,
        risk_tier=cand.risk_tier,
        direction=cand.direction or "long",
        confidence_score=cand.confidence_score,
        risk_reward=cand.risk_reward,
        volatility_notes=_combined_notes,
        status="pending",
        suggested_risk_pct=getattr(cand, "suggested_risk_pct", None),
        min_confidence_gate=getattr(cand, "min_confidence_gate", None),
        shadow_b_threshold=getattr(cand, "shadow_b_threshold", None),
        shadow_b_pass=getattr(cand, "shadow_b_pass", None),
    )
    db.add(rec)
    await db.commit()
    return _with_disclaimer(
        {
            "ok": True,
            "recommendation_id": rec.id,
            "used_latest_analysis": analysis is not None,
        }
    )


@router.get("/trade-builder/strategies")
async def list_trade_builder_strategies() -> dict[str, Any]:
    """Execution strategies available in the trade builder (id + label for dropdowns)."""
    from app.services.trade_strategies import strategy_catalog

    return _with_disclaimer({"strategies": strategy_catalog()})


@router.get("/trade-builder/static-explanation")
async def trade_builder_static_explanation(
    recommendation_id: str,
    strategy: str = "vanilla",
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Instant strategy summary (no LLM). Use with POST explain-strategy for optional AI detail."""
    from app.services.trade_strategies import (
        leg_count_for_strategy,
        normalize_strategy,
        strategy_static_explanation,
    )

    rec = await db.get(Recommendation, recommendation_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    try:
        strat = normalize_strategy(strategy)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    direction = getattr(rec, "direction", "long") or "long"
    text = strategy_static_explanation(
        strat, rec.buy_in_price, rec.stop_loss, rec.take_profit, direction
    )
    n_legs = leg_count_for_strategy(
        strat,
        rec.buy_in_price,
        rec.stop_loss,
        rec.take_profit,
        direction,
    )
    return _with_disclaimer(
        {
            "explanation": text,
            "strategy": strat,
            "leg_count": n_legs,
        }
    )


@router.post("/trade-builder/explain-strategy")
async def explain_trade_builder_strategy(
    body: TradeBuilderExplainRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Optional richer explanation via the Dashboard research LLM.
    Client should show GET /trade-builder/static-explanation first; this may return null if LLM is off or fails.
    """
    from app.services.research.llm import generate_strategy_explanation
    from app.services.trade_strategies import (
        build_strategy_explanation_user_message,
        normalize_strategy,
    )

    rec = await db.get(Recommendation, body.recommendation_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    try:
        strat = normalize_strategy(body.strategy)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    direction = getattr(rec, "direction", "long") or "long"
    user_msg = build_strategy_explanation_user_message(
        strat,
        rec.asset_id,
        rec.asset_category or "",
        direction,
        rec.buy_in_price,
        rec.stop_loss,
        rec.take_profit,
        getattr(rec, "risk_reward", None),
        getattr(rec, "volatility_notes", None),
    )
    explanation = await generate_strategy_explanation(user_msg)
    if not explanation or not str(explanation).strip():
        return _with_disclaimer(
            {
                "explanation": None,
                "source": "llm_unavailable",
                "strategy": strat,
            }
        )
    return _with_disclaimer(
        {
            "explanation": str(explanation).strip(),
            "source": "llm",
            "strategy": strat,
        }
    )


@router.post("/recommendations/{recommendation_id}/skipped")
async def mark_skipped(
    recommendation_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Mark recommendation as skipped; no further updates (PLAN_MODE)."""
    rec = await db.get(Recommendation, recommendation_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    rec.status = "skipped"
    await db.commit()
    return _with_disclaimer({"ok": True, "recommendation_id": recommendation_id})


@router.get("/active-trades")
async def list_active_trades(
    include_closed: bool = False,
    db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """
    List active trades with original SL/TP (when taken), suggested SL/TP (latest run), and full levels history.
    By default, excludes closed trades (use include_closed=true to show all).
    """
    from sqlalchemy.orm import selectinload

    q = (
        select(ActiveTrade)
        .options(selectinload(ActiveTrade.recommendation), selectinload(ActiveTrade.levels))
        .order_by(ActiveTrade.user_taken_at.desc())
    )
    
    # Filter out closed trades unless explicitly requested
    if not include_closed:
        q = q.where(~ActiveTrade.trade_phase.startswith("closed"))
    result = await db.execute(q)
    trades = result.scalars().unique().all()
    items = []
    from app.services.trade_strategies import strategy_label_for_leg
    from app.services.recommendations.engine import get_yf_symbol

    # Earnings proximity: cached per-ticker, safe to call for every trade
    from app.services.earnings_calendar import is_near_earnings as _is_near_earnings
    from app.core.preferences import get_earnings_calendar_config, get_gap_risk_config
    from app.services.recommendations.engine import _compute_gap_risk
    from app.jobs.scheduler import _gap_risk_by_symbol, _vp_by_symbol
    _earnings_cfg = get_earnings_calendar_config()
    _earnings_window = _earnings_cfg["earnings_window_days"]
    _gap_cfg = get_gap_risk_config()

    for t in trades:
        rec = t.recommendation
        levels_sorted = sorted(t.levels or [], key=lambda L: L.created_at)
        conf_score = getattr(rec, "confidence_score", None) if rec else None
        strat_leg = getattr(t, "strategy_leg", None) or "vanilla"

        symbol = get_yf_symbol(rec.asset_category, rec.asset_id) if rec else None

        # Earnings check for stocks (cached 24h — no performance concern)
        near_earnings = False
        days_to_earnings = None
        if rec and (rec.asset_category or "") == "stocks" and _earnings_window > 0 and symbol:
            try:
                near_earnings, days_to_earnings = _is_near_earnings(symbol, _earnings_window)
            except Exception:
                pass

        # Volume Profile: read from scheduler cache (populated every 2h)
        _vp = _vp_by_symbol.get(symbol, {}) if symbol else {}
        vp_data = {
            "vpoc": _vp.get("vpoc"),
            "vah": _vp.get("vah"),
            "val": _vp.get("val"),
            "vp_bias": _vp.get("vp_bias", "at_vpoc"),
            "vp_overextension": _vp.get("vp_overextension"),
            "in_value_area": _vp.get("in_value_area", False),
        }
        # VPOC distance %: how far current price is from fair value
        vp_distance_pct = None
        if vp_data["vpoc"] and rec:
            try:
                _cur = float(rec.buy_in_price)  # use entry as proxy if no live price
                vp_distance_pct = round((_cur - vp_data["vpoc"]) / vp_data["vpoc"] * 100, 2)
            except Exception:
                pass

        # Gap risk: use scheduler cache when available (full computation with snap data);
        # fall back to lightweight inline computation (factors 2-5, no snap needed).
        # getattr(None, ..., None) is safe — Factor 1 simply won't fire without snap.
        _gap_cached = _gap_risk_by_symbol.get(symbol) if symbol else None
        if _gap_cached:
            gap_risk_level = _gap_cached["level"]
            gap_risk_note = _gap_cached["note"]
        elif rec and _gap_cfg.get("gap_risk_enabled", True):
            try:
                _expiry_days = (t.expiry - date.today()).days if t.expiry else 7
                gap_risk_level, _, gap_risk_note = _compute_gap_risk(
                    snap=None,  # no snap in route context; Factor 1 skipped
                    expiry_days=max(0, _expiry_days),
                    near_earnings=near_earnings,
                    category=rec.asset_category,
                    today=date.today(),
                    max_penalty=_gap_cfg.get("gap_risk_max_penalty", 0.50),
                )
            except Exception:
                gap_risk_level = "low"
                gap_risk_note = ""
        else:
            gap_risk_level = "low"
            gap_risk_note = ""

        # Short name: use cache-first lookup (non-blocking for cached entries)
        short_name = None
        if rec:
            cached_sym = symbol or rec.asset_id.upper()
            if cached_sym in _yf_short_name_cache:
                short_name = _yf_short_name_cache[cached_sym]
            else:
                try:
                    short_name = await asyncio.to_thread(_get_short_name, symbol, rec.asset_id)
                except Exception:
                    pass

        items.append({
            "id": t.id,
            "recommendation_id": t.recommendation_id,
            "strategy_leg": strat_leg,
            "strategy_label": strategy_label_for_leg(strat_leg),
            "position_fraction": getattr(t, "position_fraction", 1.0) or 1.0,
            "asset_id": rec.asset_id if rec else None,
            "asset_category": rec.asset_category if rec else None,
            "short_name": short_name,
            "buy_in_price": rec.buy_in_price if rec else None,
            "direction": getattr(rec, "direction", "long") if rec else "long",
            "risk_tier": rec.risk_tier if rec else None,
            "confidence_score": conf_score,
            "confidence_bucket": _confidence_bucket(conf_score) if conf_score is not None else None,
            "user_taken_at": t.user_taken_at.isoformat(),
            "original_sl": t.current_sl,
            "original_tp": t.current_tp,
            "suggested_sl": t.suggested_sl,
            "suggested_tp": t.suggested_tp,
            "suggested_at": t.suggested_at.isoformat() if t.suggested_at else None,
            "expiry": t.expiry.isoformat() if t.expiry else None,
            "last_updated": t.last_updated.isoformat(),
            "trade_phase": getattr(t, "trade_phase", "open"),
            "breakeven_hit": getattr(t, "breakeven_hit", False),
            "highest_price": getattr(t, "highest_price", None),
            "lowest_price": getattr(t, "lowest_price", None),
            "signal_flip_count": getattr(t, "signal_flip_count", 0) or 0,
            "outcome": getattr(t, "outcome", None),
            "outcome_at": (oat.isoformat() if (oat := getattr(t, "outcome_at", None)) else None),
            "closing_price": getattr(t, "closing_price", None),
            "near_earnings": near_earnings,
            "days_to_earnings": days_to_earnings,
            "gap_risk_level": gap_risk_level,
            "gap_risk_note": gap_risk_note,
            "vpoc": vp_data["vpoc"],
            "vah": vp_data["vah"],
            "val": vp_data["val"],
            "vp_bias": vp_data["vp_bias"],
            "vp_overextension": vp_data["vp_overextension"],
            "in_value_area": vp_data["in_value_area"],
            "vp_distance_pct": vp_distance_pct,
            "levels": [
                {"sl": L.sl, "tp": L.tp, "created_at": L.created_at.isoformat(), "source": L.source}
                for L in levels_sorted
            ],
        })
    return _with_disclaimer({"items": items})


@router.get("/active-trades/sparkline/{asset_id}")
async def get_active_trade_sparkline(
    asset_id: str,
    category: str = "stocks",
) -> dict[str, Any]:
    """Hourly close prices for a mini sparkline chart (last ~40 candles). Uses cached yfinance data."""
    from app.services.technicals import fetch_ohlcv
    from app.services.recommendations.engine import get_yf_symbol

    symbol = get_yf_symbol(category, asset_id)
    if not symbol:
        raise HTTPException(status_code=404, detail="Unknown asset")

    df = await asyncio.to_thread(fetch_ohlcv, symbol, interval="1h")
    if df is None or df.empty:
        df = await asyncio.to_thread(fetch_ohlcv, symbol)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail="No price data")

    closes = [round(float(c), 4) for c in df["Close"].tail(40).tolist()]
    return {"asset_id": asset_id, "prices": closes}


@router.get("/active-trades/prices")
async def get_active_trade_prices(
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Current market price for each open active trade, keyed by asset_id. Lightweight — safe to poll every 60s."""
    from sqlalchemy.orm import selectinload
    from app.services.recommendations.engine import get_yf_symbol, snapshot_prices_for_recommendations

    q = (
        select(ActiveTrade)
        .options(selectinload(ActiveTrade.recommendation))
        .where(ActiveTrade.outcome.is_(None))
    )
    result = await db.execute(q)
    trades = result.scalars().all()

    # Build minimal objects snapshot_prices_for_recommendations expects
    class _AssetRef:
        def __init__(self, asset_id: str, asset_category: str):
            self.asset_id = asset_id
            self.asset_category = asset_category

    refs = []
    seen: set[str] = set()
    for t in trades:
        rec = t.recommendation
        if not rec:
            continue
        key = f"{rec.asset_category}:{rec.asset_id}"
        if key not in seen:
            seen.add(key)
            refs.append(_AssetRef(rec.asset_id, rec.asset_category))

    prices = await asyncio.to_thread(snapshot_prices_for_recommendations, refs)
    return {"prices": prices}


@router.get("/active-trades/{trade_id}/gap-advice")
async def get_gap_advice(
    trade_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Generate a short LLM recommendation for an active trade with gap risk.
    Assembles: snap (ATR, gap metrics), sentiment, market regime, near_earnings.
    Returns {"advice": "<one sentence>"} or {"advice": null} if LLM unavailable.
    """
    from sqlalchemy.orm import selectinload
    from app.services.recommendations.engine import get_yf_symbol
    from app.services.technicals import build_snapshot
    from app.services.earnings_calendar import is_near_earnings as _is_near_earnings
    from app.services.research.llm import generate_gap_advice
    from app.core.preferences import get_gap_risk_config, get_earnings_calendar_config
    from app.jobs.scheduler import _gap_risk_by_symbol
    from datetime import date
    import calendar

    q = (
        select(ActiveTrade)
        .options(selectinload(ActiveTrade.recommendation))
        .where(ActiveTrade.id == trade_id)
    )
    result = await db.execute(q)
    trade = result.scalars().first()
    if not trade or not trade.recommendation:
        raise HTTPException(status_code=404, detail="Trade not found")

    rec = trade.recommendation
    symbol = get_yf_symbol(rec.asset_category, rec.asset_id)

    # Fetch snap in thread (blocking yfinance call)
    snap = await asyncio.to_thread(build_snapshot, symbol) if symbol else None

    # Gap risk factors from scheduler cache or inline
    _gap_data = _gap_risk_by_symbol.get(symbol, {}) if symbol else {}
    gap_level = _gap_data.get("level", "medium")
    gap_factors = _gap_data.get("note", "")

    # Earnings
    near_earnings, days_to_earnings = False, None
    _earn_cfg = get_earnings_calendar_config()
    if rec.asset_category == "stocks" and symbol:
        try:
            near_earnings, days_to_earnings = _is_near_earnings(symbol, _earn_cfg["earnings_window_days"])
        except Exception:
            pass

    # Market regime from context (non-critical)
    market_regime = "unknown"
    try:
        from app.services.market_context import detect_market_regime
        mr = await asyncio.to_thread(detect_market_regime)
        if mr:
            market_regime = getattr(mr, "regime", None) or "unknown"
    except Exception:
        pass

    today = date.today()
    expiry_days = (trade.expiry - today).days if trade.expiry else 7

    context = {
        "asset_id": rec.asset_id,
        "direction": getattr(rec, "direction", "long"),
        "category": rec.asset_category,
        "gap_level": gap_level,
        "gap_factors": gap_factors,
        "hold_days": max(0, expiry_days),
        "weekday_name": calendar.day_name[today.weekday()],
        "atr_pct": getattr(snap, "atr_pct", None) if snap else None,
        "overnight_gap_avg": getattr(snap, "overnight_gap_pct_avg", None) if snap else None,
        "overnight_gap_p95": getattr(snap, "overnight_gap_pct_p95", None) if snap else None,
        "sentiment_score": None,  # no per-trade sentiment field; leave for LLM inference
        "market_regime": market_regime,
        "near_earnings": near_earnings,
        "days_to_earnings": days_to_earnings,
        "confidence_score": rec.confidence_score,
    }

    advice = await generate_gap_advice(context)
    return {"advice": advice}


@router.post("/active-trades/{trade_id}/close-manually")
async def close_trade_manually(
    trade_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Mark an active trade as closed manually (outcome=manual). Captures current price at close time."""
    from sqlalchemy.orm import selectinload
    from app.services.technicals import get_cached_price
    from app.services.recommendations.engine import get_yf_symbol
    
    q = select(ActiveTrade).options(selectinload(ActiveTrade.recommendation)).where(ActiveTrade.id == trade_id)
    result = await db.execute(q)
    trade = result.scalar_one_or_none()
    
    if not trade:
        raise HTTPException(status_code=404, detail="Active trade not found")
    if getattr(trade, "outcome", None) is not None:
        return _with_disclaimer({"ok": True, "message": "Trade already has an outcome.", "trade_id": trade_id})
    
    now = datetime.now(timezone.utc)
    
    # Capture closing price
    rec = trade.recommendation
    if rec:
        symbol = get_yf_symbol(rec.asset_category, rec.asset_id)
        if symbol:
            closing_price = get_cached_price(symbol)
            if closing_price:
                trade.closing_price = closing_price
    
    trade.trade_phase = "closed_manual"
    trade.outcome = "manual"
    trade.outcome_at = now
    trade.last_updated = now
    await db.commit()
    return _with_disclaimer({"ok": True, "message": "Trade marked closed manually.", "trade_id": trade_id})


@router.get("/calibration")
async def get_calibration(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """
    Empirical hit rate per confidence bucket, Brier score, and ECE from closed trades.
    Uses ActiveTrade.outcome (hit_tp, hit_sl, expired, manual) and linked Recommendation.confidence_score.
    """
    from sqlalchemy.orm import selectinload

    q = (
        select(ActiveTrade)
        .options(selectinload(ActiveTrade.recommendation))
        .where(ActiveTrade.outcome.isnot(None))
        .order_by(ActiveTrade.outcome_at.desc())
    )
    result = await db.execute(q)
    trades = result.scalars().unique().all()

    # By bucket: n, hit_tp, hit_sl, expired, manual, hit_rate (TP / (TP+SL+expired))
    buckets: dict[str, dict[str, Any]] = {}
    for name in ("low", "medium", "high", "very_high"):
        buckets[name] = {"n": 0, "hit_tp": 0, "hit_sl": 0, "expired": 0, "manual": 0, "hit_rate": None}

    # For Brier and ECE: (conf_prob, outcome 0/1), excluding manual
    brier_pairs: list[tuple[float, float]] = []

    for t in trades:
        rec = t.recommendation
        if not rec:
            continue
        bucket = _confidence_bucket(getattr(rec, "confidence_score", None))
        if bucket not in buckets:
            buckets[bucket] = {"n": 0, "hit_tp": 0, "hit_sl": 0, "expired": 0, "manual": 0, "hit_rate": None}
        buckets[bucket]["n"] += 1
        outcome = getattr(t, "outcome", None) or ""
        if outcome == "hit_tp":
            buckets[bucket]["hit_tp"] += 1
            brier_pairs.append(((getattr(rec, "confidence_score", 0) or 0) / 100.0, 1.0))
        elif outcome == "hit_sl":
            buckets[bucket]["hit_sl"] += 1
            brier_pairs.append(((getattr(rec, "confidence_score", 0) or 0) / 100.0, 0.0))
        elif outcome == "expired":
            buckets[bucket]["expired"] += 1
            brier_pairs.append(((getattr(rec, "confidence_score", 0) or 0) / 100.0, 0.0))
        elif outcome == "manual":
            buckets[bucket]["manual"] += 1

    for name, data in buckets.items():
        resolved = data["hit_tp"] + data["hit_sl"] + data["expired"]
        if resolved > 0:
            data["hit_rate"] = round(data["hit_tp"] / resolved, 4)

    n_total = len(brier_pairs)
    brier_score = round(sum((p - o) ** 2 for p, o in brier_pairs) / n_total, 4) if n_total else None
    # ECE: group by confidence bucket, weight by count
    conf_outcomes: list[tuple[float, float]] = brier_pairs
    if conf_outcomes:
        def bucket_of(p: float) -> str:
            s = round(p * 100)
            return _confidence_bucket(s)
        by_b: dict[str, list[tuple[float, float]]] = {}
        for p, o in conf_outcomes:
            b = bucket_of(p)
            by_b.setdefault(b, []).append((p, o))
        ece = 0.0
        for b, pairs in by_b.items():
            n_k = len(pairs)
            avg_conf = sum(x[0] for x in pairs) / n_k
            avg_outcome = sum(x[1] for x in pairs) / n_k
            ece += (n_k / n_total) * abs(avg_conf - avg_outcome)
        ece = round(ece, 4)
    else:
        ece = None

    # Platt calibration params (if fitted)
    from app.services.calibration import get_platt_params
    from app.services.adaptive_thresholds import update_adaptive_thresholds, get_adaptive_threshold, is_adaptive_enabled
    
    platt_a, platt_b = get_platt_params()
    
    # Update adaptive thresholds based on current calibration data
    if n_total >= 10:
        update_adaptive_thresholds(buckets)
    
    adaptive_thresholds = {
        "enabled": is_adaptive_enabled(),
        "risk_on": get_adaptive_threshold("risk_on"),
        "cautious": get_adaptive_threshold("cautious"),
        "risk_off": get_adaptive_threshold("risk_off"),
    }

    return _with_disclaimer({
        "by_bucket": buckets,
        "brier_score": brier_score,
        "ece": ece,
        "n_total": n_total,
        "platt_calibration": {
            "fitted": platt_a is not None,
            "a": platt_a,
            "b": platt_b,
        },
        "adaptive_thresholds": adaptive_thresholds,
    })


@router.post("/calibration/fit")
async def fit_calibration(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """
    Fit Platt calibration from closed trades (hit_tp, hit_sl, expired).
    Updates global calibration params; future recommendations can optionally use calibrated probability.
    """
    from sqlalchemy.orm import selectinload
    from app.services.calibration import fit_platt_calibration, set_platt_params

    q = (
        select(ActiveTrade)
        .options(selectinload(ActiveTrade.recommendation))
        .where(ActiveTrade.outcome.isnot(None))
    )
    result = await db.execute(q)
    trades = result.scalars().unique().all()

    confidences: list[float] = []
    outcomes: list[float] = []
    for t in trades:
        rec = t.recommendation
        if not rec:
            continue
        outcome_val = getattr(t, "outcome", None)
        if outcome_val == "manual":
            continue
        conf = getattr(rec, "confidence_score", None)
        if conf is None:
            continue
        confidences.append(conf / 100.0)
        outcomes.append(1.0 if outcome_val == "hit_tp" else 0.0)

    if len(confidences) < 10:
        return _with_disclaimer({"ok": False, "message": f"Not enough data (n={len(confidences)}). Need at least 10 closed trades."})

    result = fit_platt_calibration(confidences, outcomes)
    if result is None:
        return _with_disclaimer({"ok": False, "message": "Platt calibration failed (sklearn unavailable or fitting error). Existing params preserved."})
    a, b = result
    set_platt_params(a, b)
    return _with_disclaimer({"ok": True, "message": f"Platt calibration fitted on {len(confidences)} trades.", "a": a, "b": b})


@router.post("/calibration/clear")
async def clear_calibration() -> dict[str, Any]:
    """Clear fitted Platt parameters from memory and delete the persisted file."""
    from app.services.calibration import clear_platt_params
    clear_platt_params()
    return _with_disclaimer({"ok": True, "message": "Platt calibration cleared."})


@router.get("/regime/status")
async def get_regime_status(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """
    Get current regime status and any trades flagged for review due to recent transitions.
    Refreshes regime from live market context so the UI is not stuck on "unknown" until
    the active-trades job runs.
    """
    from app.services.market_context import build_market_context, detect_dxy_macro
    from app.services.regime_monitor import (
        detect_regime_transition,
        get_current_regime_state,
        get_trades_needing_review,
    )
    from sqlalchemy.orm import selectinload

    # Read persisted regime state — do NOT recalculate here.
    # detect_regime_transition() is called by the scheduler job (every ~2h).
    # Calling it on every UI poll caused the regime to oscillate on threshold
    # boundaries (e.g. VIX near 20/25), resetting _last_regime_at each time.
    regime_state = get_current_regime_state()

    # If state file is missing (fresh install), do a one-time silent bootstrap
    # without triggering a recorded transition.
    if regime_state.get("current_regime") is None:
        try:
            ctx = await asyncio.to_thread(build_market_context)
            if ctx and getattr(ctx, "regime", None):
                await asyncio.to_thread(detect_regime_transition, ctx.regime.regime)
                regime_state = get_current_regime_state()
        except Exception:
            pass
    
    # Get active trades
    q = (
        select(ActiveTrade)
        .options(selectinload(ActiveTrade.recommendation))
        .where(ActiveTrade.trade_phase.in_(["open", "partial_tp", "trailing"]))
    )
    result = await db.execute(q)
    trades = result.scalars().unique().all()
    
    # Convert to dicts for regime monitor
    trades_data = []
    for t in trades:
        rec = t.recommendation
        trades_data.append({
            "id": t.id,
            "asset_id": rec.asset_id if rec else None,
            "trade_phase": getattr(t, "trade_phase", "open"),
            "signal_flip_count": getattr(t, "signal_flip_count", 0),
        })
    
    # Get trades needing review (will be empty if no recent transition)
    flagged_trades = get_trades_needing_review(trades_data, None)  # transition is tracked internally

    dxy = await asyncio.to_thread(detect_dxy_macro)

    return _with_disclaimer({
        "regime_state": regime_state,
        "flagged_trades": flagged_trades,
        "flagged_count": len(flagged_trades),
        "dxy": {
            "available": dxy.available,
            "symbol": dxy.symbol or None,
            "price": dxy.price,
            "trend": dxy.trend,
            "change_5d_pct": dxy.change_5d_pct,
            "change_20d_pct": dxy.change_20d_pct,
            "summary": dxy.summary or None,
        },
    })


@router.get("/market/context")
async def get_market_context() -> dict[str, Any]:
    """Current market regime, cross-asset correlations, and trading conditions."""
    from app.services.market_context import build_market_context

    ctx = await asyncio.to_thread(build_market_context)
    return _with_disclaimer({
        "regime": ctx.regime.regime,
        "regime_description": ctx.regime.description,
        "spx_trend": ctx.regime.spx_trend,
        "vix_level": ctx.regime.vix_level,
        "vix_regime": ctx.regime.vix_regime,
        "direction_bias": ctx.regime_direction_bias,
        "should_reduce_exposure": ctx.should_reduce_exposure,
        "min_confidence_threshold": ctx.min_confidence_threshold(),
        "max_recommendations_per_category": ctx.max_recommendations_per_category(),
        "correlation_signals": [
            {
                "pair": cs.pair_name,
                "a": cs.asset_a, "a_dir": cs.a_direction,
                "b": cs.asset_b, "b_dir": cs.b_direction,
                "diverging": cs.diverging,
                "signal": cs.signal,
            }
            for cs in ctx.correlation_signals
        ],
        "has_divergence_warning": ctx.has_divergence_warning,
        "dxy": {
            "available": ctx.dxy.available,
            "symbol": ctx.dxy.symbol or None,
            "price": ctx.dxy.price,
            "trend": ctx.dxy.trend,
            "change_5d_pct": ctx.dxy.change_5d_pct,
            "change_20d_pct": ctx.dxy.change_20d_pct,
            "summary": ctx.dxy.summary or None,
        },
    })


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "app": "local_trading_bot_v2"}


# --- Sector Regimes ---

@router.get("/market/sectors")
async def get_sector_regimes() -> dict[str, Any]:
    """
    Get regime status for all tracked sectors (tech, energy, financials, etc.).
    Each sector has its own trend and regime based on sector ETF performance.
    """
    from app.services.market_context import detect_all_sector_regimes
    
    sectors = await asyncio.to_thread(detect_all_sector_regimes)
    
    return _with_disclaimer({
        "sectors": {
            name: {
                "regime": sr.regime,
                "trend": sr.trend,
                "above_sma50": sr.above_sma50,
                "above_sma200": sr.above_sma200,
                "relative_strength_vs_spx": sr.relative_strength,
            }
            for name, sr in sectors.items()
        },
        "count": len(sectors),
    })


@router.get("/market/correlations")
async def get_correlation_matrix() -> dict[str, Any]:
    """
    Get cross-asset correlation signals and a correlation matrix for key assets.
    Includes divergence warnings and 20-day return correlations.
    """
    from app.services.market_context import check_cross_asset_signals, CORRELATION_PAIRS
    from app.services.technicals import fetch_ohlcv
    import pandas as pd
    
    # Get correlation signals
    signals = await asyncio.to_thread(check_cross_asset_signals)
    
    # Build correlation matrix for key assets
    assets = ["^GSPC", "^VIX", "GC=F", "CL=F", "DX-Y.NYB", "^TNX"]
    asset_names = {"^GSPC": "SPX", "^VIX": "VIX", "GC=F": "Gold", "CL=F": "Oil", "DX-Y.NYB": "USD", "^TNX": "10Y Yield"}
    
    def _build_matrix():
        # Fetch all data first
        all_data = {}
        for sym in assets:
            df = fetch_ohlcv(sym, period="30d")
            if df is not None and len(df) >= 10:
                close = df["Close"]
                daily_ret = close.pct_change().dropna()
                # Use date index for alignment
                daily_ret.index = pd.to_datetime(daily_ret.index).date
                all_data[asset_names.get(sym, sym)] = daily_ret
        
        if len(all_data) < 2:
            return None
        
        # Build DataFrame with date alignment (inner join on dates)
        ret_df = pd.DataFrame(all_data)
        ret_df = ret_df.dropna()  # Only keep rows where all assets have data
        
        if len(ret_df) < 5:
            return None
        
        # Use last 20 rows (or all if fewer)
        ret_df = ret_df.tail(20)
        
        corr = ret_df.corr()
        return corr.round(3).to_dict()
    
    matrix = await asyncio.to_thread(_build_matrix)
    
    return _with_disclaimer({
        "signals": [
            {
                "pair": cs.pair_name,
                "asset_a": cs.asset_a, "a_direction": cs.a_direction,
                "asset_b": cs.asset_b, "b_direction": cs.b_direction,
                "expected": cs.expected_relationship,
                "diverging": cs.diverging,
                "signal": cs.signal,
            }
            for cs in signals
        ],
        "correlation_matrix": matrix,
        "has_divergence": any(cs.diverging for cs in signals),
    })


@router.get("/market/correlations/analysis")
async def get_correlation_llm_analysis() -> dict[str, Any]:
    """
    LLM interpretation of current cross-asset divergences.
    Returns a short 2-3 sentence analysis or null if LLM unavailable / no divergences.
    """
    from app.services.market_context import check_cross_asset_signals
    from app.services.research.llm import generate_correlation_analysis

    signals = await asyncio.to_thread(check_cross_asset_signals)
    diverging = [
        {
            "asset_a": cs.asset_a, "a_direction": cs.a_direction,
            "asset_b": cs.asset_b, "b_direction": cs.b_direction,
            "expected_relationship": cs.expected_relationship,
        }
        for cs in signals if cs.diverging
    ]

    if not diverging:
        return {"analysis": None, "reason": "no_divergences"}

    analysis = await generate_correlation_analysis(diverging)
    return {"analysis": analysis, "signal_count": len(diverging)}


# --- Macro Calendar ---

@router.get("/macro-calendar")
async def get_macro_calendar_status() -> dict[str, Any]:
    """
    Get macro calendar status: loaded dates, upcoming events, and overlay info.
    Dates are fetched from public sources (Fed calendar, press releases) and cached.
    """
    from app.services.macro_calendar import get_macro_overlay_info
    return _with_disclaimer(get_macro_overlay_info())


@router.post("/macro-calendar/refresh")
async def refresh_macro_calendar() -> dict[str, Any]:
    """
    Force refresh macro calendar from public sources (Fed, pattern-based CPI/NFP).
    Updates the local cache file.
    """
    from app.services.macro_calendar import refresh_dates, get_macro_overlay_info
    count = await asyncio.to_thread(refresh_dates, True)
    info = get_macro_overlay_info()
    return _with_disclaimer({
        "refreshed": True,
        "dates_loaded": count,
        **info,
    })


# --- Preferences (risk stance); used by recommendation engine ---

@router.get("/preferences")
async def get_preferences() -> dict[str, Any]:
    """Return user preferences (risk stance + research LLM provider/model/URL + risk budget + sentiment decay + confidence thresholds)."""
    from app.core.preferences import (
        get_preferred_risk,
        get_research_llm_config,
        get_risk_budget_1r,
        get_sentiment_half_life,
        get_confidence_thresholds,
        get_shadow_mode_config,
        get_commodity_engine_gates,
        get_non_commodity_engine_gates,
    )
    sm = get_shadow_mode_config()
    cg = get_commodity_engine_gates()
    nc = get_non_commodity_engine_gates()
    out = {
        "preferred_risk": get_preferred_risk(),
        "risk_budget_1r": get_risk_budget_1r(),
        "sentiment_half_life_hours": get_sentiment_half_life(),
        "confidence_thresholds": get_confidence_thresholds(),
        "shadow_mode_enabled": sm["enabled"],
        "shadow_confidence_delta": sm["confidence_delta_stored"],
        "commodity_confidence_offset": cg["commodity_confidence_offset"],
        "commodity_min_risk_reward": cg["commodity_min_risk_reward"],
        "equity_confidence_offset": nc["equity_confidence_offset"],
        "equity_min_risk_reward": nc["equity_min_risk_reward"],
        "other_confidence_offset": nc["other_confidence_offset"],
        "other_min_risk_reward": nc["other_min_risk_reward"],
    }
    out.update(get_research_llm_config())
    return _with_disclaimer(out)


class PreferencesUpdate(BaseModel):
    preferred_risk: str | None = None
    research_llm_provider: str | None = None
    research_llm_model: str | None = None
    ollama_base_url: str | None = None
    lm_studio_base_url: str | None = None
    research_llm_thinking_budget: int | None = None  # 0 = disabled; max depends on provider (Anthropic: up to 32000)
    risk_budget_1r: float | None = None
    sentiment_half_life_hours: float | None = None
    confidence_threshold_risk_on: int | None = None
    confidence_threshold_cautious: int | None = None
    confidence_threshold_risk_off: int | None = None
    shadow_mode_enabled: bool | None = None
    shadow_confidence_delta: int | None = None
    commodity_confidence_offset: int | None = None
    commodity_min_risk_reward: float | None = None
    equity_confidence_offset: int | None = None
    equity_min_risk_reward: float | None = None
    other_confidence_offset: int | None = None
    other_min_risk_reward: float | None = None


@router.patch("/preferences")
async def update_preferences(body: PreferencesUpdate) -> dict[str, Any]:
    """Update preferences. Risk: conservative|moderate|aggressive. LLM: provider, model, ollama_base_url. Risk budget: 0-50. Sentiment half-life: 1-168 hours. Confidence thresholds: 0-80."""
    from app.core.preferences import (
        set_preferred_risk, set_research_llm_config, get_preferred_risk, get_research_llm_config,
        set_risk_budget_1r, get_risk_budget_1r, set_sentiment_half_life, get_sentiment_half_life,
        set_confidence_thresholds, get_confidence_thresholds, set_shadow_mode, get_shadow_mode_config,
        set_commodity_engine_gates, get_commodity_engine_gates,
        apply_non_commodity_engine_patch, get_non_commodity_engine_gates,
    )
    try:
        if body.preferred_risk is not None:
            set_preferred_risk(body.preferred_risk)
        if (
            body.research_llm_provider is not None
            or body.research_llm_model is not None
            or body.ollama_base_url is not None
            or body.lm_studio_base_url is not None
            or body.research_llm_thinking_budget is not None
        ):
            set_research_llm_config(
                provider=body.research_llm_provider,
                model=body.research_llm_model,
                ollama_base_url=body.ollama_base_url,
                lm_studio_base_url=body.lm_studio_base_url,
                thinking_budget=body.research_llm_thinking_budget,
            )
        if body.risk_budget_1r is not None:
            set_risk_budget_1r(body.risk_budget_1r)
        if body.sentiment_half_life_hours is not None:
            set_sentiment_half_life(body.sentiment_half_life_hours)
        if body.confidence_threshold_risk_on is not None or body.confidence_threshold_cautious is not None or body.confidence_threshold_risk_off is not None:
            set_confidence_thresholds(
                risk_on=body.confidence_threshold_risk_on,
                cautious=body.confidence_threshold_cautious,
                risk_off=body.confidence_threshold_risk_off,
            )
        if body.shadow_mode_enabled is not None or body.shadow_confidence_delta is not None:
            set_shadow_mode(
                enabled=body.shadow_mode_enabled,
                confidence_delta=body.shadow_confidence_delta,
            )
        if body.commodity_confidence_offset is not None or body.commodity_min_risk_reward is not None:
            set_commodity_engine_gates(
                confidence_offset=body.commodity_confidence_offset,
                min_risk_reward=body.commodity_min_risk_reward,
            )
        nc_keys = (
            "equity_confidence_offset",
            "equity_min_risk_reward",
            "other_confidence_offset",
            "other_min_risk_reward",
        )
        patch = body.model_dump(exclude_unset=True)
        nc_patch = {k: patch[k] for k in nc_keys if k in patch}
        if nc_patch:
            apply_non_commodity_engine_patch(nc_patch)
        sm = get_shadow_mode_config()
        cg = get_commodity_engine_gates()
        nc = get_non_commodity_engine_gates()
        out = {
            "preferred_risk": get_preferred_risk(),
            "risk_budget_1r": get_risk_budget_1r(),
            "sentiment_half_life_hours": get_sentiment_half_life(),
            "confidence_thresholds": get_confidence_thresholds(),
            "shadow_mode_enabled": sm["enabled"],
            "shadow_confidence_delta": sm["confidence_delta_stored"],
            "commodity_confidence_offset": cg["commodity_confidence_offset"],
            "commodity_min_risk_reward": cg["commodity_min_risk_reward"],
            "equity_confidence_offset": nc["equity_confidence_offset"],
            "equity_min_risk_reward": nc["equity_min_risk_reward"],
            "other_confidence_offset": nc["other_confidence_offset"],
            "other_min_risk_reward": nc["other_min_risk_reward"],
        }
        out.update(get_research_llm_config())
        out["message"] = "Preferences saved."
        return _with_disclaimer(out)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Webhook Notifications ---

@router.get("/notifications/config")
async def get_notification_config() -> dict[str, Any]:
    """Get webhook notification settings."""
    from app.services.notifications import get_webhook_config
    return _with_disclaimer(get_webhook_config())


class NotificationConfigUpdate(BaseModel):
    webhook_url: str | None = None
    webhook_enabled: bool | None = None
    notify_batch_ready: bool | None = None
    notify_regime_change: bool | None = None
    notify_macro_alert: bool | None = None
    notify_trade_outcome: bool | None = None


@router.patch("/notifications/config")
async def update_notification_config(body: NotificationConfigUpdate) -> dict[str, Any]:
    """Update webhook notification settings."""
    from app.services.notifications import set_webhook_config
    config = set_webhook_config(
        webhook_url=body.webhook_url,
        webhook_enabled=body.webhook_enabled,
        notify_batch_ready=body.notify_batch_ready,
        notify_regime_change=body.notify_regime_change,
        notify_macro_alert=body.notify_macro_alert,
        notify_trade_outcome=body.notify_trade_outcome,
    )
    config["message"] = "Notification settings saved."
    return _with_disclaimer(config)


@router.post("/notifications/test")
async def test_notification() -> dict[str, Any]:
    """Send a test notification to verify webhook configuration."""
    from app.services.notifications import send_notification, get_webhook_config
    
    config = get_webhook_config()
    if not config["webhook_url"]:
        raise HTTPException(status_code=400, detail="No webhook URL configured")
    
    # Temporarily enable for test
    test_data = {
        "count": 5,
        "categories": {"stocks": 2, "commodities": 1, "indices": 1, "other": 1}
    }
    
    # Force send regardless of enabled status
    from app.services.notifications import _format_message
    import httpx
    
    payload = _format_message("batch_ready", test_data)
    payload["embeds"][0]["title"] = "🧪 Test Notification"
    payload["embeds"][0]["description"] = "Webhook is configured correctly!"
    payload["text"] = "🧪 Test Notification\nWebhook is configured correctly!"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(config["webhook_url"], json=payload)
            if resp.status_code < 300:
                return _with_disclaimer({"ok": True, "message": "Test notification sent successfully!"})
            else:
                raise HTTPException(status_code=502, detail=f"Webhook returned {resp.status_code}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Webhook request timed out")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to send: {str(e)}")


# --- Actions (same logic as scripts; for UI buttons) ---

@router.get("/pipeline/progress")
async def get_pipeline_progress() -> dict[str, Any]:
    """Return current pipeline progress state (for live progress bar polling)."""
    import app.services.pipeline_progress as progress
    return progress.get()


@router.post("/actions/run-pipeline-once")
async def action_run_pipeline_once() -> dict[str, Any]:
    """
    Kick off the pipeline as a background task and return immediately (202).
    Progress is polled via GET /api/pipeline/progress.
    """
    import asyncio
    import app.services.pipeline_progress as progress
    from app.jobs.scheduler import job_ingest_and_sentiment, job_run_analysis, job_daily_batch

    if progress.get()["running"]:
        return _with_disclaimer({"ok": False, "message": "Pipeline already running."})

    async def _run() -> None:
        try:
            await job_ingest_and_sentiment()
            await job_run_analysis()
            await job_daily_batch()
            progress.finish()
        except Exception as exc:
            progress.finish(error=str(exc))

    progress.start()
    asyncio.create_task(_run())
    return _with_disclaimer({"ok": True, "message": "Pipeline started in background."})


@router.post("/actions/refresh-todays-batch")
async def action_refresh_todays_batch() -> dict[str, Any]:
    """Clear today's pending/skipped recommendations (and their active trades); keep taken trades. Then re-run daily batch."""
    from app.jobs.scheduler import job_daily_batch, job_update_active_trades
    from sqlalchemy import select, delete
    from app.core.database import async_session_factory
    batch_id = date.today().isoformat()
    async with async_session_factory() as session:
        recs_to_remove = await session.execute(
            select(Recommendation.id).where(
                Recommendation.batch_id == batch_id,
                Recommendation.status != "taken",
            )
        )
        rec_ids = [r for r in recs_to_remove.scalars().all()]
        if rec_ids:
            await session.execute(delete(ActiveTrade).where(ActiveTrade.recommendation_id.in_(rec_ids)))
        await session.execute(
            delete(Recommendation).where(
                Recommendation.batch_id == batch_id,
                Recommendation.status != "taken",
            )
        )
        await session.commit()
    await job_daily_batch()
    await job_update_active_trades()
    return _with_disclaimer({"ok": True, "message": f"Today's batch ({batch_id}) refreshed. Active trades updated."})


@router.post("/actions/clear-todays-batch")
async def action_clear_todays_batch() -> dict[str, Any]:
    """Clear today's pending/skipped recommendations only (no re-run). Taken trades and their recommendations are kept."""
    from sqlalchemy import select, delete
    from app.core.database import async_session_factory
    batch_id = date.today().isoformat()
    async with async_session_factory() as session:
        recs_to_remove = await session.execute(
            select(Recommendation.id).where(
                Recommendation.batch_id == batch_id,
                Recommendation.status != "taken",
            )
        )
        rec_ids = [r for r in recs_to_remove.scalars().all()]
        if rec_ids:
            await session.execute(delete(ActiveTrade).where(ActiveTrade.recommendation_id.in_(rec_ids)))
        await session.execute(
            delete(Recommendation).where(
                Recommendation.batch_id == batch_id,
                Recommendation.status != "taken",
            )
        )
        await session.commit()
    return _with_disclaimer({"ok": True, "message": f"Today's batch ({batch_id}) cleared. Taken trades kept."})


@router.post("/actions/update-active-trades")
async def action_update_active_trades() -> dict[str, Any]:
    """Run the active trades update now: recalculate SL/TP, track phases, add history levels."""
    from app.jobs.scheduler import job_update_active_trades
    await job_update_active_trades()
    return _with_disclaimer({"ok": True, "message": "Active trades updated with latest prices and levels."})


@router.get("/circuit-breaker")
async def get_circuit_breaker_status(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """
    Current circuit breaker state: mode (normal/defensive/paused), daily loss R,
    consecutive losses, cumulative drawdown R, and what triggered any active mode.
    """
    from app.services.circuit_breaker import compute_state as _cb_compute
    from app.core.preferences import get_circuit_breaker_config

    cfg = get_circuit_breaker_config()
    result = await db.execute(select(ActiveTrade).where(ActiveTrade.outcome.isnot(None)))
    closed_trades = result.scalars().all()

    if not cfg["circuit_breaker_enabled"]:
        return _with_disclaimer({
            "enabled": False,
            "mode": "normal",
            "config": cfg,
            "stats": {"rolling_loss_r": 0, "consecutive_losses": 0, "drawdown_r": 0},
            "triggered_by": [],
        })

    state = _cb_compute(
        list(closed_trades),
        max_consecutive_losses=cfg["max_consecutive_losses"],
        max_daily_loss_r=cfg["max_daily_loss_r"],
        drawdown_gate_r=cfg["drawdown_gate_r"],
    )
    return _with_disclaimer({
        "enabled": True,
        "mode": state.mode,
        "config": cfg,
        "stats": {
            "rolling_loss_r": state.rolling_loss_r,
            "consecutive_losses": state.consecutive_losses,
            "drawdown_r": state.drawdown_r,
        },
        "triggered_by": state.triggered_by,
    })


@router.post("/actions/purge-all")
async def action_purge_all() -> dict[str, Any]:
    """Delete all recommendations, active trades, and levels. For testing only; use with care."""
    from sqlalchemy import delete
    from app.core.database import async_session_factory
    from app.models.recommendation import ActiveTradeLevel

    async with async_session_factory() as session:
        await session.execute(delete(ActiveTradeLevel))
        await session.execute(delete(ActiveTrade))
        await session.execute(delete(Recommendation))
        await session.commit()
    return _with_disclaimer({"ok": True, "message": "All recommendations and active trades purged."})
