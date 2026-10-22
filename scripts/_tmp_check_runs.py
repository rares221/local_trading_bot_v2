import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from app.models.backtest import BacktestRun, BacktestTrade

sync_url = settings.database_url.replace("+aiosqlite", "").replace("+asyncpg", "")
engine = sa.create_engine(sync_url)
Session = sessionmaker(bind=engine)
s = Session()

runs = s.query(BacktestRun).filter(BacktestRun.status == "completed").all()
print(f"total completed runs: {len(runs)}")
for r in runs:
    n_mid_small = s.query(BacktestTrade).filter(
        BacktestTrade.run_id == r.id,
        BacktestTrade.asset_category.in_(["stocks_mid", "stocks_small"]),
        BacktestTrade.outcome.in_(["hit_tp", "hit_sl"]),
    ).count()
    n_total = s.query(BacktestTrade).filter(BacktestTrade.run_id == r.id).count()
    print(f"{r.id[:8]}  {r.start_date} -> {r.end_date}  total_trades={n_total}  "
          f"mid_small_resolved={n_mid_small}  min_conf={r.min_confidence} risk_tier={r.risk_tier}")
