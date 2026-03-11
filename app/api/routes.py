"""
API routes: analysis, recommendations, feedback (Took it/Skipped), active trades.
All user-facing responses include disclaimer per TRADING_APP_PLAN.
"""
from datetime import datetime, timezone, date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.recommendation import Recommendation, ActiveTrade, ActiveTradeLevel
from app.models.market_analysis import MarketAnalysis

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


def _rec_to_item(r: Recommendation) -> dict:
    return {
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
        "status": r.status,
        "rationale_text": getattr(r, "rationale_text", None),
    }


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
    Use group_by=category to get { stocks: [...], commodities: [...], other: [...] } for the recommendation page.
    """
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
    items = [_rec_to_item(r) for r in recs]
    if group_by == "category":
        grouped: dict[str, list] = {"stocks": [], "commodities": [], "other": []}
        for r in recs:
            cat = r.asset_category if r.asset_category in grouped else "other"
            if cat not in grouped:
                grouped[cat] = []
            grouped[cat].append(_rec_to_item(r))
        return _with_disclaimer({"by_category": grouped, "items": items})
    return _with_disclaimer({"items": items})


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
async def list_active_trades(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """List active trades with original SL/TP (when taken), suggested SL/TP (latest run), and full levels history."""
    from sqlalchemy.orm import selectinload

    q = (
        select(ActiveTrade)
        .options(selectinload(ActiveTrade.recommendation), selectinload(ActiveTrade.levels))
        .order_by(ActiveTrade.user_taken_at.desc())
    )
    result = await db.execute(q)
    trades = result.scalars().unique().all()
    items = []
    for t in trades:
        rec = t.recommendation
        levels_sorted = sorted(t.levels or [], key=lambda L: L.created_at)
        items.append({
            "id": t.id,
            "recommendation_id": t.recommendation_id,
            "asset_id": rec.asset_id if rec else None,
            "buy_in_price": rec.buy_in_price if rec else None,
            "user_taken_at": t.user_taken_at.isoformat(),
            "original_sl": t.current_sl,
            "original_tp": t.current_tp,
            "suggested_sl": t.suggested_sl,
            "suggested_tp": t.suggested_tp,
            "suggested_at": t.suggested_at.isoformat() if t.suggested_at else None,
            "expiry": t.expiry.isoformat() if t.expiry else None,
            "last_updated": t.last_updated.isoformat(),
            "levels": [
                {"sl": L.sl, "tp": L.tp, "created_at": L.created_at.isoformat(), "source": L.source}
                for L in levels_sorted
            ],
        })
    return _with_disclaimer({"items": items})


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "app": "local_trading_bot_v2"}


# --- Preferences (risk stance); used by recommendation engine ---

@router.get("/preferences")
async def get_preferences() -> dict[str, Any]:
    """Return user preferences (risk stance + research LLM provider/model/URL)."""
    from app.core.preferences import get_preferred_risk, get_research_llm_config
    out = {"preferred_risk": get_preferred_risk()}
    out.update(get_research_llm_config())
    return _with_disclaimer(out)


class PreferencesUpdate(BaseModel):
    preferred_risk: str | None = None
    research_llm_provider: str | None = None
    research_llm_model: str | None = None
    ollama_base_url: str | None = None


@router.patch("/preferences")
async def update_preferences(body: PreferencesUpdate) -> dict[str, Any]:
    """Update preferences. Risk: conservative|moderate|aggressive. LLM: provider (openai|anthropic|ollama|openrouter), model, ollama_base_url."""
    from app.core.preferences import set_preferred_risk, set_research_llm_config, get_preferred_risk, get_research_llm_config
    try:
        if body.preferred_risk is not None:
            set_preferred_risk(body.preferred_risk)
        if body.research_llm_provider is not None or body.research_llm_model is not None or body.ollama_base_url is not None:
            set_research_llm_config(
                provider=body.research_llm_provider,
                model=body.research_llm_model,
                ollama_base_url=body.ollama_base_url,
            )
        out = {"preferred_risk": get_preferred_risk()}
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
    from app.jobs.scheduler import job_daily_batch
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
    return _with_disclaimer({"ok": True, "message": f"Today's batch ({batch_id}) refreshed. Taken trades kept."})


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
