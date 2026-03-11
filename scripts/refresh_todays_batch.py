#!/usr/bin/env python3
"""
Clear today's recommendation batch and re-run the pipeline so you get up to 5
recommendations with real prices (Option A). Use when you want to refresh
today's batch without waiting for the next day.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import date
from sqlalchemy import select, delete
from app.core.database import async_session_factory
from app.models.recommendation import Recommendation, ActiveTrade
from app.jobs.scheduler import job_daily_batch


async def main() -> None:
    batch_id = date.today().isoformat()
    async with async_session_factory() as session:
        # Delete active trades that reference today's recommendations
        recs_today = await session.execute(select(Recommendation.id).where(Recommendation.batch_id == batch_id))
        rec_ids = [r for r in recs_today.scalars().all()]
        if rec_ids:
            await session.execute(delete(ActiveTrade).where(ActiveTrade.recommendation_id.in_(rec_ids)))
        await session.execute(delete(Recommendation).where(Recommendation.batch_id == batch_id))
        await session.commit()
    print(f"Cleared today's batch ({batch_id}). Running daily batch with real prices...")
    await job_daily_batch()
    print("Done. Check GET /api/recommendations")


if __name__ == "__main__":
    asyncio.run(main())
