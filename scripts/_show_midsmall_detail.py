import asyncio
from datetime import date
from sqlalchemy import select
from app.core.database import async_session_factory
from app.models.recommendation import Recommendation


async def main() -> None:
    batch_id = date.today().isoformat()
    async with async_session_factory() as session:
        result = await session.execute(
            select(Recommendation).where(
                Recommendation.batch_id == batch_id,
                Recommendation.asset_category.in_(["stocks_mid", "stocks_small"]),
            )
        )
        rows = result.scalars().all()

    for r in rows:
        print("=" * 70)
        print(f"{r.asset_id} ({r.asset_category}) dir={r.direction} risk_tier={r.risk_tier}")
        print(f"  confidence_score={r.confidence_score}  min_confidence_gate={r.min_confidence_gate}")
        print(f"  buy_in_price={r.buy_in_price}  stop_loss={r.stop_loss}  take_profit={r.take_profit}")
        print(f"  risk_reward={r.risk_reward}  suggested_risk_pct={r.suggested_risk_pct}")
        print(f"  instrument_type={r.instrument_type}  expiry_date={r.expiry_date}  status={r.status}")
        print(f"  shadow_b_threshold={r.shadow_b_threshold}  shadow_b_pass={r.shadow_b_pass}")
        print(f"  volatility_notes={r.volatility_notes}")
        print(f"  rationale_text={r.rationale_text}")


if __name__ == "__main__":
    asyncio.run(main())
