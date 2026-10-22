"""
Out-of-sample check: does the stocks_mid/stocks_small time-exit-with-SL alpha found on run
`062af0ff` (2023-05-27 -> 2026-05-27) hold on a genuinely non-overlapping window? Reuses
run `619e7aa5` (2022-01-28 -> 2022-10-28, pre-dates the original window, includes 2022 bear
market). Read-only, no app code touched. Run:
  docker-compose exec -T app python -m scripts.diagnose_exit_risk_mgmt_oos
"""
import numpy as np
import pandas as pd
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.backtest import BacktestRun, BacktestTrade
from app.services import market_data
from app.services.backtesting.simulator import _pnl_r

MAX_BARS = 30
OOS_RUN_PREFIX = "619e7aa5"
DAYS = [15, 30]


def _get_sync_session():
    sync_url = settings.database_url.replace("+aiosqlite", "").replace("+asyncpg", "")
    engine = sa.create_engine(sync_url)
    return sessionmaker(bind=engine)()


def time_exit_with_sl(entry, sl, direction, future_bars, day_n):
    risk = abs(entry - sl)
    bars = future_bars.iloc[:day_n].dropna(subset=["Open", "High", "Low", "Close"])
    if bars.empty or risk <= 0:
        return 0.0
    for i, (idx, row) in enumerate(bars.iterrows()):
        bar_open, bar_high, bar_low = float(row["Open"]), float(row["High"]), float(row["Low"])
        if direction == "long":
            if bar_low <= sl:
                closing = bar_open if bar_open <= sl else sl
                return _pnl_r(-1.0, entry, closing, sl, risk, direction)
        else:
            if bar_high >= sl:
                closing = bar_open if bar_open >= sl else sl
                return _pnl_r(-1.0, entry, closing, sl, risk, direction)
    last_close = float(bars["Close"].iloc[-1])
    r = (last_close - entry) / risk if direction == "long" else (entry - last_close) / risk
    return round(r, 4)


def main():
    session = _get_sync_session()
    run_row = session.query(BacktestRun).filter(BacktestRun.id.like(f"{OOS_RUN_PREFIX}%")).one()
    run_id = run_row.id
    print(f"[OOS] run_id={run_id} window={run_row.start_date}..{run_row.end_date}")
    trades = (
        session.query(BacktestTrade)
        .filter(
            BacktestTrade.run_id == run_id,
            BacktestTrade.outcome.in_(["hit_tp", "hit_sl"]),
            BacktestTrade.asset_category.in_(["stocks_mid", "stocks_small"]),
        )
        .all()
    )
    session.close()
    print(f"[OOS] mid/small resolved trades: {len(trades)}")
    print(f"[OOS] by category: "
          f"{pd.Series([t.asset_category for t in trades]).value_counts().to_dict()}")

    records = []
    by_symbol = {}
    for t in trades:
        by_symbol.setdefault(t.symbol, []).append(t)

    for sym, sym_trades in by_symbol.items():
        px = market_data.fetch_ohlcv(sym, period="5y")
        if px is None or px.empty:
            continue
        if px.index.tz is not None:
            px = px.tz_localize(None)
        for t in sym_trades:
            entry_ts = pd.Timestamp(t.entry_date)
            future = px[px.index > entry_ts]
            if future.empty:
                continue
            risk_frac = abs(t.entry_price - t.stop_loss) / t.entry_price if t.entry_price else 0.0
            sign = 1.0 if t.direction == "long" else -1.0
            rec = {
                "symbol": sym, "category": t.asset_category, "entry_date": t.entry_date,
                "risk_frac": risk_frac, "sign": sign,
            }
            for day_n in DAYS:
                r_val = time_exit_with_sl(t.entry_price, t.stop_loss, t.direction, future, day_n)
                rec[f"r_day{day_n}"] = r_val
                rec[f"pct_day{day_n}"] = sign * r_val * risk_frac
            records.append(rec)

    df = pd.DataFrame(records)
    print(f"[OOS] priced={len(df)}/{len(trades)}")
    if df.empty:
        print("[OOS] no priceable trades -- cannot proceed.")
        return

    spy = market_data.fetch_ohlcv("SPY", period="5y")
    if spy.index.tz is not None:
        spy = spy.tz_localize(None)
    spy_cache = {}
    for ed in df["entry_date"].unique():
        ed_ts = pd.Timestamp(ed)
        before = spy[spy.index <= ed_ts]
        if before.empty:
            continue
        entry_px = float(before["Close"].iloc[-1])
        future = spy[spy.index > ed_ts]
        for day_n in DAYS:
            bars = future.iloc[:day_n]
            if bars.empty:
                continue
            exit_px = float(bars["Close"].iloc[-1])
            spy_cache[(ed, day_n)] = (exit_px - entry_px) / entry_px

    for day_n in DAYS:
        df[f"spy_day{day_n}"] = df["entry_date"].map(lambda ed: spy_cache.get((ed, day_n)))

    print(f"\n--- OOS (2022 window) stocks_mid/stocks_small time-exit-with-SL vs SPY, unit-matched ---")
    for day_n in DAYS:
        strat_col, spy_col = f"pct_day{day_n}", f"spy_day{day_n}"
        sub = df[[strat_col, spy_col, "category"]].dropna()
        n_dates = df.loc[sub.index, "entry_date"].nunique()
        if len(sub) < 10:
            print(f"  day {day_n}: n={len(sub)} -- too small to draw any conclusion")
            continue
        corr = sub[strat_col].corr(sub[spy_col])
        spy_avg, strat_avg = sub[spy_col].mean(), sub[strat_col].mean()
        real_alpha = (sub[strat_col] - sub[spy_col]).mean()
        try:
            beta, alpha_reg = np.polyfit(sub[spy_col], sub[strat_col], 1)
        except Exception:
            beta, alpha_reg = float("nan"), float("nan")
        print(f"  day {day_n:>2}: n={len(sub):4d} ({n_dates} dates)  SPY={spy_avg:+.4%}  "
              f"strat={strat_avg:+.4%}  corr={corr:+.3f}  real_alpha={real_alpha:+.4%}  "
              f"beta={beta:+.3f} alpha={alpha_reg:+.4%}")
        for cat, g in sub.groupby("category"):
            if len(g) < 10:
                print(f"      {cat:14s} n={len(g):4d} -- too small to draw any conclusion")
                continue
            c_corr = g[strat_col].corr(g[spy_col])
            c_alpha = (g[strat_col] - g[spy_col]).mean()
            print(f"      {cat:14s} n={len(g):4d}  SPY={g[spy_col].mean():+.4%}  "
                  f"strat={g[strat_col].mean():+.4%}  corr={c_corr:+.3f}  alpha={c_alpha:+.4%}")


if __name__ == "__main__":
    main()
