"""
Throwaway diagnostic (read-only, no production code changes): follow-up to the mid/small-cap
short-skew audit -- checks whether stocks_large (44.3% short-rate, unfixed) and indices (not
measured before) have a real, fixable coverage shortage, or whether they're already fine.

Run: docker-compose exec -T app python -m scripts.diagnose_large_indices_coverage
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, timedelta

import pandas as pd

import app.services.technicals as tech
from app.services.backtesting.data_provider import prefetch_symbols, get_trading_days
from app.services.backtesting.regime import build_regime_calendar
from app.services.technicals import build_snapshot, determine_direction

LARGE_SYMBOLS = ["AAPL", "MSFT", "GOOGL", "NVDA", "META", "AMZN", "TSLA", "AVGO", "JPM", "BAC",
                  "GS", "JNJ", "UNH", "XOM", "CVX", "BP", "SHEL", "NEM", "PG", "WMT", "V"]
INDICES_SYMBOLS = ["^GSPC", "^DJI", "^IXIC", "^RUT", "^FTSE", "^GDAXI", "^FCHI", "^STOXX50E",
                     "^N225", "^HSI", "000001.SS", "^KS11", "^BSESN", "^AXJO", "^NSEI", "^VIX"]

MIN_BARS_FOR_SIGNAL = 35


def short_skew_tally() -> None:
    all_symbols = LARGE_SYMBOLS + INDICES_SYMBOLS
    tech.clear_cache()
    ohlcv_data = prefetch_symbols(
        all_symbols, period="1y",
        extra_symbols=["^GSPC", "^VIX", "XLK", "XLE", "XLF", "XLV", "XLY", "XLI", "XLB"],
    )

    end_date = date.today()
    start_date = end_date - timedelta(days=365)
    trading_days = get_trading_days(ohlcv_data, start_date, end_date)
    print(f"[DIAG] Trading days in window: {len(trading_days)} ({trading_days[0]} -> {trading_days[-1]})")

    regime_calendar = build_regime_calendar(trading_days, ohlcv_data)

    tally: dict[str, dict[str, dict[str, int]]] = {
        "stocks_large": defaultdict(lambda: defaultdict(int)),
        "indices": defaultdict(lambda: defaultdict(int)),
    }
    cat_symbols = {"stocks_large": LARGE_SYMBOLS, "indices": INDICES_SYMBOLS}

    for sim_date in trading_days:
        cutoff_ts = pd.Timestamp(sim_date)
        tech.set_backtest_mode(cutoff_ts)
        day_regime = regime_calendar.get(sim_date, "neutral")

        for category, syms in cat_symbols.items():
            for symbol in syms:
                try:
                    snap = build_snapshot(symbol)
                    if snap is None or snap.price <= 0:
                        continue
                    df = tech._ohlcv_cache.get(symbol)
                    if df is not None and not df.empty:
                        available = df[df.index <= cutoff_ts]
                        if len(available) < MIN_BARS_FOR_SIGNAL:
                            continue
                    if day_regime in ("crisis", "high_vol"):
                        continue
                    direction = determine_direction(None, snap, regime=day_regime)
                    tally[category][day_regime][direction] += 1
                except Exception as e:
                    print(f"[DIAG] error {category} {symbol} @{sim_date}: {e}")

    tech.clear_backtest_mode()

    print("\n" + "=" * 90)
    print("short_disabled rate by category x regime")
    print("=" * 90)
    for category in ("stocks_large", "indices"):
        print(f"\n--- {category} ---")
        overall_long = overall_short = 0
        for regime in sorted(tally[category].keys()):
            counts = tally[category][regime]
            n_long = counts.get("long", 0)
            n_short = counts.get("short", 0)
            total = n_long + n_short
            overall_long += n_long
            overall_short += n_short
            pct = (n_short / total * 100) if total else 0.0
            print(f"  regime={regime:10s} long={n_long:4d} short={n_short:4d} total={total:4d} short_rate={pct:5.1f}%")
        total_all = overall_long + overall_short
        pct_all = (overall_short / total_all * 100) if total_all else 0.0
        print(f"  {'OVERALL':17s} long={overall_long:4d} short={overall_short:4d} total={total_all:4d} short_rate={pct_all:5.1f}%")


async def recent_output_counts() -> None:
    from sqlalchemy import select, func
    from app.core.database import async_session_factory
    from app.models.recommendation import Recommendation

    async with async_session_factory() as session:
        res = await session.execute(
            select(Recommendation.batch_id, Recommendation.asset_category,
                   func.count(Recommendation.id))
            .where(Recommendation.asset_category.in_(["stocks_large", "indices", "stocks_mid", "stocks_small"]))
            .group_by(Recommendation.batch_id, Recommendation.asset_category)
            .order_by(Recommendation.batch_id)
        )
        rows = res.all()

    by_batch: dict[str, dict[str, int]] = defaultdict(dict)
    for batch_id, category, cnt in rows:
        by_batch[batch_id][category] = cnt

    print("\n" + "=" * 90)
    print("Recommendation OUTPUT count per batch, by category (all stored batches)")
    print("=" * 90)
    for batch_id in sorted(by_batch.keys()):
        counts = by_batch[batch_id]
        print(f"  {batch_id}: large={counts.get('stocks_large', 0):2d} "
              f"indices={counts.get('indices', 0):2d} mid={counts.get('stocks_mid', 0):2d} "
              f"small={counts.get('stocks_small', 0):2d}")

    for cat in ("stocks_large", "indices", "stocks_mid", "stocks_small"):
        vals = [by_batch[b].get(cat, 0) for b in by_batch]
        if vals:
            avg = sum(vals) / len(vals)
            zero_days = sum(1 for v in vals if v == 0)
            print(f"\n  {cat}: avg={avg:.2f}/batch, zero-output days={zero_days}/{len(vals)}")


def main() -> None:
    short_skew_tally()
    asyncio.run(recent_output_counts())


if __name__ == "__main__":
    main()
