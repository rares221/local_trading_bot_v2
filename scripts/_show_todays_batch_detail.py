import asyncio
from datetime import date
from sqlalchemy import select
from app.core.database import async_session_factory
from app.models.recommendation import Recommendation


async def main() -> None:
    batch_id = date.today().isoformat()
    async with async_session_factory() as session:
        result = await session.execute(
            select(Recommendation)
            .where(Recommendation.batch_id == batch_id)
            .order_by(Recommendation.asset_category, Recommendation.confidence_score.desc())
        )
        rows = result.scalars().all()

    for r in rows:
        print("=" * 70)
        print(f"{r.asset_id} ({r.asset_category}) dir={r.direction} risk_tier={r.risk_tier}")
        print(f"  confidence_score={r.confidence_score}")
        print(f"  buy_in_price={r.buy_in_price}  stop_loss={r.stop_loss}  take_profit={r.take_profit}")
        print(f"  risk_reward={r.risk_reward}  suggested_risk_pct={r.suggested_risk_pct}")
        print(f"  instrument_type={r.instrument_type}  expiry_date={r.expiry_date}  status={r.status}")
        print(f"  volatility_notes={r.volatility_notes}")


if __name__ == "__main__":
    asyncio.run(main())
