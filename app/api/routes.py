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
from app.models.recommendation import Recommendation, ActiveTrade
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
    """List active trades (recommendations user marked as taken); get updates from update job."""
    q = (
        select(ActiveTrade, Recommendation.asset_id, Recommendation.buy_in_price)
        .join(Recommendation, ActiveTrade.recommendation_id == Recommendation.id)
        .order_by(ActiveTrade.user_taken_at.desc())
    )
    result = await db.execute(q)
    rows = result.all()
    return _with_disclaimer({
        "items": [
            {
                "id": t.id,
                "recommendation_id": t.recommendation_id,
                "asset_id": asset_id,
                "buy_in_price": buy_in_price,
                "user_taken_at": t.user_taken_at.isoformat(),
                "current_sl": t.current_sl,
                "current_tp": t.current_tp,
                "expiry": t.expiry.isoformat() if t.expiry else None,
                "last_updated": t.last_updated.isoformat(),
            }
            for t, asset_id, buy_in_price in rows
        ]
    })


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "app": "local_trading_bot_v2"}


# --- Preferences (risk stance); used by recommendation engine ---

@router.get("/preferences")
async def get_preferences() -> dict[str, Any]:
    """Return user preferences (e.g. preferred_risk for recommendations)."""
    from app.core.preferences import get_preferred_risk
    return _with_disclaimer({"preferred_risk": get_preferred_risk()})


class PreferencesUpdate(BaseModel):
    preferred_risk: str


@router.patch("/preferences")
async def update_preferences(body: PreferencesUpdate) -> dict[str, Any]:
    """Update preferences. preferred_risk: conservative | moderate | aggressive."""
    from app.core.preferences import set_preferred_risk
    try:
        saved = set_preferred_risk(body.preferred_risk)
        return _with_disclaimer({"preferred_risk": saved, "message": "Preferences saved."})
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
