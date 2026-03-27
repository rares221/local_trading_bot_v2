"""
API routes: analysis, recommendations, feedback (Took it/Skipped), active trades.
All user-facing responses include disclaimer per TRADING_APP_PLAN.
"""
import asyncio
from datetime import datetime, timezone, date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.recommendation import Recommendation, ActiveTrade, ActiveTradeLevel
from app.models.market_analysis import MarketAnalysis
from app.models.daily_briefing import DailyBriefing

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
    p = get_provider_by_id(settings.research_llm_provider or "openai")
    if p and not model:
        model = p.default_model
    return _with_disclaimer({
        "providers": providers,
        "current": {"provider": settings.research_llm_provider or "openai", "model": model or ""},
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
    score = getattr(r, "confidence_score", None)
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
        "confidence_bucket": _confidence_bucket(score),
        "risk_reward": getattr(r, "risk_reward", None),
        "suggested_risk_pct": getattr(r, "suggested_risk_pct", None),
        "volatility_notes": getattr(r, "volatility_notes", None),
        "status": r.status,
        "rationale_text": getattr(r, "rationale_text", None),
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


@router.post("/recommendations/{recommendation_id}/took-it")
async def mark_took_it(
    recommendation_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Mark recommendation as taken; create active trade for updates (PLAN_MODE)."""
    rec = await db.get(Recommendation, recommendation_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    if rec.status != "pending":
        raise HTTPException(status_code=400, detail=f"Recommendation already {rec.status}")
    rec.status = "taken"
    now = datetime.now(timezone.utc)
    active = ActiveTrade(
        recommendation_id=rec.id,
        user_taken_at=now,
        current_sl=rec.stop_loss,
        current_tp=rec.take_profit,
        expiry=rec.expiry_date,
        last_updated=now,
    )
    db.add(active)
    await db.flush()
    level = ActiveTradeLevel(
        active_trade_id=active.id,
        sl=rec.stop_loss,
        tp=rec.take_profit,
        created_at=now,
        source="initial",
    )
    db.add(level)
    await db.commit()
    return _with_disclaimer({"ok": True, "recommendation_id": recommendation_id, "active_trade_created": True})


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
    for t in trades:
        rec = t.recommendation
        levels_sorted = sorted(t.levels or [], key=lambda L: L.created_at)
        conf_score = getattr(rec, "confidence_score", None) if rec else None
        items.append({
            "id": t.id,
            "recommendation_id": t.recommendation_id,
            "asset_id": rec.asset_id if rec else None,
            "asset_category": rec.asset_category if rec else None,
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
            "levels": [
                {"sl": L.sl, "tp": L.tp, "created_at": L.created_at.isoformat(), "source": L.source}
                for L in levels_sorted
            ],
        })
    return _with_disclaimer({"items": items})


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
            brier_pairs.append((getattr(rec, "confidence_score", 0) or 0) / 100.0, 1.0)
        elif outcome == "hit_sl":
            buckets[bucket]["hit_sl"] += 1
            brier_pairs.append((getattr(rec, "confidence_score", 0) or 0) / 100.0, 0.0)
        elif outcome == "expired":
            buckets[bucket]["expired"] += 1
            brier_pairs.append((getattr(rec, "confidence_score", 0) or 0) / 100.0, 0.0)
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

    a, b = fit_platt_calibration(confidences, outcomes)
    set_platt_params(a, b)
    return _with_disclaimer({"ok": True, "message": f"Platt calibration fitted on {len(confidences)} trades.", "a": a, "b": b})


@router.get("/regime/status")
async def get_regime_status(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """
    Get current regime status and any trades flagged for review due to recent transitions.
    Refreshes regime from live market context so the UI is not stuck on "unknown" until
    the active-trades job runs.
    """
    from app.services.market_context import build_market_context
    from app.services.regime_monitor import (
        detect_regime_transition,
        get_current_regime_state,
        get_trades_needing_review,
    )
    from sqlalchemy.orm import selectinload

    live_regime: str | None = None
    try:
        ctx = await asyncio.to_thread(build_market_context)
        if ctx and getattr(ctx, "regime", None):
            live_regime = ctx.regime.regime
            await asyncio.to_thread(detect_regime_transition, live_regime)
    except Exception:
        pass

    regime_state = get_current_regime_state()
    if regime_state.get("current_regime") is None and live_regime:
        regime_state = {**regime_state, "current_regime": live_regime}
    
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
    
    return _with_disclaimer({
        "regime_state": regime_state,
        "flagged_trades": flagged_trades,
        "flagged_count": len(flagged_trades),
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
    })


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "app": "local_trading_bot_v2"}


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
    """Return user preferences (risk stance + research LLM provider/model/URL + risk budget)."""
    from app.core.preferences import get_preferred_risk, get_research_llm_config, get_risk_budget_1r
    out = {"preferred_risk": get_preferred_risk(), "risk_budget_1r": get_risk_budget_1r()}
    out.update(get_research_llm_config())
    return _with_disclaimer(out)


class PreferencesUpdate(BaseModel):
    preferred_risk: str | None = None
    research_llm_provider: str | None = None
    research_llm_model: str | None = None
    ollama_base_url: str | None = None
    risk_budget_1r: float | None = None


@router.patch("/preferences")
async def update_preferences(body: PreferencesUpdate) -> dict[str, Any]:
    """Update preferences. Risk: conservative|moderate|aggressive. LLM: provider (openai|anthropic|ollama|openrouter), model, ollama_base_url. Risk budget: 0.0-10.0 (1.0 = 100%)."""
    from app.core.preferences import set_preferred_risk, set_research_llm_config, get_preferred_risk, get_research_llm_config, set_risk_budget_1r, get_risk_budget_1r
    try:
        if body.preferred_risk is not None:
            set_preferred_risk(body.preferred_risk)
        if body.research_llm_provider is not None or body.research_llm_model is not None or body.ollama_base_url is not None:
            set_research_llm_config(
                provider=body.research_llm_provider,
                model=body.research_llm_model,
                ollama_base_url=body.ollama_base_url,
            )
        if body.risk_budget_1r is not None:
            set_risk_budget_1r(body.risk_budget_1r)
        out = {"preferred_risk": get_preferred_risk(), "risk_budget_1r": get_risk_budget_1r()}
        out.update(get_research_llm_config())
        out["message"] = "Preferences saved."
        return _with_disclaimer(out)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Actions (same logic as scripts; for UI buttons) ---

@router.post("/actions/run-pipeline-once")
async def action_run_pipeline_once() -> dict[str, Any]:
    """Run ingestion + sentiment, analysis, and daily batch (same as scripts/run_pipeline_once.py)."""
    from app.jobs.scheduler import job_ingest_and_sentiment, job_run_analysis, job_daily_batch
    await job_ingest_and_sentiment()
    await job_run_analysis()
    await job_daily_batch()
    return _with_disclaimer({"ok": True, "message": "Pipeline run complete. Check recommendations."})


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
