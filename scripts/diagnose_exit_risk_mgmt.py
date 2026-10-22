"""
Throwaway diagnostic (sub-project A, option 2, extended per coordinator follow-up): does
changing EXIT/risk-management, holding entries fixed, improve expectancy? Read-only, no app
code touched. Run: docker-compose exec -T app python -m scripts.diagnose_exit_risk_mgmt
"""
from collections import defaultdict

import pandas as pd
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.backtest import BacktestRun, BacktestTrade
from app.services import market_data
from app.services.backtesting.simulator import simulate_outcome, _pnl_r

MAX_BARS = 30  # same horizon the original run used


def _get_sync_session():
    sync_url = settings.database_url.replace("+aiosqlite", "").replace("+asyncpg", "")
    engine = sa.create_engine(sync_url)
    return sessionmaker(bind=engine)()


def tighter_wider_tp(entry, sl, tp, direction, mult, future_bars):
    reward = abs(tp - entry) * mult
    new_tp = entry + reward if direction == "long" else entry - reward
    return simulate_outcome(entry, sl, new_tp, direction, future_bars, max_bars=MAX_BARS)


def time_exit_with_sl(entry, sl, direction, future_bars, day_n):
    """Exit at day_n OR stop-loss, whichever comes first. No profit target at all."""
    risk = abs(entry - sl)
    bars = future_bars.iloc[:day_n].dropna(subset=["Open", "High", "Low", "Close"])
    if bars.empty or risk <= 0:
        return 0.0, "expired", 0
    for i, (idx, row) in enumerate(bars.iterrows()):
        bar_open, bar_high, bar_low = float(row["Open"]), float(row["High"]), float(row["Low"])
        bars_held = i + 1
        if direction == "long":
            if bar_low <= sl:
                closing = bar_open if bar_open <= sl else sl
                return _pnl_r(-1.0, entry, closing, sl, risk, direction), "hit_sl", bars_held
        else:
            if bar_high >= sl:
                closing = bar_open if bar_open >= sl else sl
                return _pnl_r(-1.0, entry, closing, sl, risk, direction), "hit_sl", bars_held
    last_close = float(bars["Close"].iloc[-1])
    r = (last_close - entry) / risk if direction == "long" else (entry - last_close) / risk
    return round(r, 4), "time_exit", len(bars)


def main():
    session = _get_sync_session()
    run_row = session.query(BacktestRun).filter(BacktestRun.id.like("062af0ff%")).one()
    run_id = run_row.id
    trades = (
        session.query(BacktestTrade)
        .filter(BacktestTrade.run_id == run_id, BacktestTrade.outcome.in_(["hit_tp", "hit_sl"]))
        .all()
    )
    session.close()

    print(f"[DIAG] run_id={run_id} resolved_trades={len(trades)}")
    baseline_pnl = [t.pnl_r for t in trades if t.pnl_r is not None]
    baseline_wr = sum(1 for t in trades if t.outcome == "hit_tp") / len(trades)
    print(f"[DIAG] BASELINE (stored, actual): avg_pnl_r={sum(baseline_pnl)/len(baseline_pnl):.4f} "
          f"WR={baseline_wr:.1%} n={len(trades)}")

    by_symbol = defaultdict(list)
    for t in trades:
        by_symbol[t.symbol].append(t)

    # Per-trade record accumulator for all downstream analysis
    records = []

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

            sim0 = simulate_outcome(t.entry_price, t.stop_loss, t.take_profit, t.direction,
                                     future, max_bars=MAX_BARS)
            label_match = sim0.outcome == t.outcome

            # R-to-% conversion, derived from simulator._pnl_r's actual definition:
            # risk = abs(entry - sl) (price units); for long, R = (close - entry) / risk, so
            # price_change = R * risk, and pct_return = price_change / entry = R * (risk / entry).
            # For short, R = (entry - close) / risk -> price_change = -R * risk (sign flips).
            risk_price = abs(t.entry_price - t.stop_loss)
            risk_frac = risk_price / t.entry_price if t.entry_price else 0.0
            direction_sign = 1.0 if t.direction == "long" else -1.0

            rec = {
                "symbol": sym, "category": t.asset_category, "regime": t.market_regime,
                "entry_date": t.entry_date, "direction": t.direction, "risk_frac": risk_frac,
                "stored_pnl_r": t.pnl_r, "stored_outcome": t.outcome,
                "resim_pnl_r": sim0.pnl_r, "resim_outcome": sim0.outcome,
                "label_match": label_match,
            }

            for mult in [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0]:
                sim = tighter_wider_tp(t.entry_price, t.stop_loss, t.take_profit, t.direction,
                                       mult, future)
                rec[f"tp_{mult}x_pnl"] = sim.pnl_r
                rec[f"tp_{mult}x_resolved"] = sim.outcome in ("hit_tp", "hit_sl")

            for day_n in [5, 10, 15, 20, 30]:
                pnl, outc, _ = time_exit_with_sl(t.entry_price, t.stop_loss, t.direction, future, day_n)
                rec[f"time_sl_day{day_n}_pnl"] = pnl
                rec[f"time_sl_day{day_n}_hit_sl"] = outc == "hit_sl"

            records.append(rec)

    # --- SPY benchmark: matching-window buy-and-hold return per entry_date x horizon ---
    spy = market_data.fetch_ohlcv("SPY", period="5y")
    if spy.index.tz is not None:
        spy = spy.tz_localize(None)
    spy_cache: dict[tuple, float] = {}
    distinct_dates = {r["entry_date"] for r in records}
    for ed in distinct_dates:
        ed_ts = pd.Timestamp(ed)
        at_or_before = spy[spy.index <= ed_ts]
        if at_or_before.empty:
            continue
        spy_entry_px = float(at_or_before["Close"].iloc[-1])
        spy_future = spy[spy.index > ed_ts]
        for day_n in [5, 10, 15, 20, 30]:
            bars = spy_future.iloc[:day_n]
            if bars.empty:
                continue
            spy_exit_px = float(bars["Close"].iloc[-1])
            spy_cache[(ed, day_n)] = (spy_exit_px - spy_entry_px) / spy_entry_px

    for rec in records:
        for day_n in [5, 10, 15, 20, 30]:
            rec[f"spy_ret_day{day_n}"] = spy_cache.get((rec["entry_date"], day_n))

    df = pd.DataFrame(records)
    n = len(df)
    df["direction_sign"] = df["direction"].map({"long": 1.0, "short": -1.0})
    print(f"[DIAG] direction breakdown: {df['direction'].value_counts().to_dict()}")
    for day_n in [5, 10, 15, 20, 30]:
        r_col = f"time_sl_day{day_n}_pnl"
        df[f"pct_ret_day{day_n}"] = df["direction_sign"] * df[r_col] * df["risk_frac"]
    print(f"[DIAG] priced={n}/{len(trades)}  sanity_match_rate={df['label_match'].mean():.1%}")
    print(f"[DIAG] resim baseline (1.0x, all incl. expired mark-to-market) avg={df['tp_1.0x_pnl'].mean():.4f}")

    # --- 1. TP multiplier sweep: all-trades (incl. expired mark-to-market) vs resolved-only ---
    print("\n--- 1. TP multiplier sweep ---")
    for mult in [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0]:
        col_pnl, col_res = f"tp_{mult}x_pnl", f"tp_{mult}x_resolved"
        all_avg = df[col_pnl].mean()
        resolved_frac = df[col_res].mean()
        resolved_avg = df.loc[df[col_res], col_pnl].mean()
        print(f"  {mult:>4}x  avg_pnl_r(all,incl.expired)={all_avg:+.4f}  "
              f"resolved_frac={resolved_frac:.1%}  avg_pnl_r(resolved_only)={resolved_avg:+.4f}")

    # --- 2. Time-exit WITH stop-loss active ---
    print("\n--- 2. Time-exit WITH stop-loss active (exit day N or SL, whichever first) ---")
    for day_n in [5, 10, 15, 20, 30]:
        col_pnl, col_sl = f"time_sl_day{day_n}_pnl", f"time_sl_day{day_n}_hit_sl"
        print(f"  day {day_n:>2}: avg_pnl_r={df[col_pnl].mean():+.4f}  "
              f"stopped_out_frac={df[col_sl].mean():.1%}  win_rate(pnl>0)={(df[col_pnl] > 0).mean():.1%}")

    # --- 3. Root-cause the baseline discrepancy ---
    print("\n--- 3. Baseline discrepancy root-cause ---")
    mismatches = df[~df["label_match"]]
    print(f"  mismatched trades: {len(mismatches)}/{n} ({len(mismatches)/n:.1%})")
    print(f"  distinct symbols among mismatches: {mismatches['symbol'].nunique()} "
          f"(out of {df['symbol'].nunique()} total symbols)")
    by_sym_mismatch = mismatches.groupby("symbol").size().sort_values(ascending=False)
    print(f"  top 5 symbols by mismatch count: {by_sym_mismatch.head(5).to_dict()}")
    diff = (df["resim_pnl_r"] - df["stored_pnl_r"]).abs()
    print(f"  mean |resim_pnl_r - stored_pnl_r| overall: {diff.mean():.4f}; "
          f"on matched-only: {diff[df['label_match']].mean():.4f}; "
          f"on mismatched-only: {diff[~df['label_match']].mean():.4f}")
    matched = df[df["label_match"]]
    print(f"  [matched-only, n={len(matched)}] TP-widening check:")
    for mult in [1.0, 2.0, 3.0]:
        print(f"    {mult}x avg_pnl_r = {matched[f'tp_{mult}x_pnl'].mean():+.4f}")

    # --- 4. Category / regime breakdown of the TP-widening effect ---
    print("\n--- 4. Category x regime breakdown (tp_1.0x baseline vs tp_3.0x) ---")
    grp = df.groupby(["category", "regime"]).agg(
        n=("tp_1.0x_pnl", "size"),
        base_1x=("tp_1.0x_pnl", "mean"),
        tp_2x=("tp_2.0x_pnl", "mean"),
        tp_3x=("tp_3.0x_pnl", "mean"),
    ).reset_index()
    grp = grp[grp["n"] >= 20].sort_values(["category", "regime"])
    for _, row in grp.iterrows():
        flag = "  <-- stocks_large x bull" if row["category"] == "stocks_large" and row["regime"] == "bull" else ""
        print(f"  {row['category']:14s} {str(row['regime']):10s} n={int(row['n']):5d}  "
              f"1x={row['base_1x']:+.4f}  2x={row['tp_2x']:+.4f}  3x={row['tp_3x']:+.4f}{flag}")

    # --- 5. SPY benchmark, UNIT-MATCHED (strategy converted R -> actual % price return via
    # each trade's own risk_frac = |entry-sl|/entry, derived from simulator._pnl_r's real
    # definition of R; direction_sign flips sign for shorts, though this run is long-only) ---
    print("\n--- 5. SPY benchmark vs time-exit-with-SL, UNIT-MATCHED (both now %% return) ---")
    import numpy as np
    for day_n in [5, 10, 15, 20, 30]:
        strat_col, spy_col = f"pct_ret_day{day_n}", f"spy_ret_day{day_n}"
        sub = df[[strat_col, spy_col]].dropna()
        if len(sub) < 20:
            print(f"  day {day_n}: insufficient matched SPY data (n={len(sub)})")
            continue
        n_distinct_days = df.loc[sub.index, "entry_date"].nunique()
        corr = sub[strat_col].corr(sub[spy_col])
        spy_avg = sub[spy_col].mean()
        strat_avg = sub[strat_col].mean()
        real_alpha = (sub[strat_col] - sub[spy_col]).mean()
        beta, alpha_reg = np.polyfit(sub[spy_col], sub[strat_col], 1)
        print(f"  day {day_n:>2}: n={len(sub):5d} ({n_distinct_days} distinct entry dates)  "
              f"SPY_ret={spy_avg:+.4%}  strat_ret={strat_avg:+.4%}  corr={corr:+.3f}  "
              f"real_alpha={real_alpha:+.4%}  regression: beta={beta:+.3f} alpha={alpha_reg:+.4%}")

    print("\n--- 5b. Category breakdown (day 15, 30), UNIT-MATCHED ---")
    for day_n in [15, 30]:
        strat_col, spy_col = f"pct_ret_day{day_n}", f"spy_ret_day{day_n}"
        print(f"  [day {day_n}]")
        for cat, g in df.groupby("category"):
            sub = g[[strat_col, spy_col]].dropna()
            if len(sub) < 20:
                continue
            n_dd = g.loc[sub.index, "entry_date"].nunique()
            corr = sub[strat_col].corr(sub[spy_col])
            real_alpha = (sub[strat_col] - sub[spy_col]).mean()
            print(f"    {cat:14s} n={len(sub):5d} ({n_dd} dates)  SPY_ret={sub[spy_col].mean():+.4%}  "
                  f"strat_ret={sub[strat_col].mean():+.4%}  corr={corr:+.3f}  real_alpha={real_alpha:+.4%}")


if __name__ == "__main__":
    main()
