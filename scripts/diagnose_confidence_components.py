"""
Throwaway diagnostic (read-only): for backtest run 062af0ff-8a44-44ed-bbc6-e691280db486,
recompute compute_confidence()'s per-component point contributions for every resolved trade
and correlate each component against the actual outcome (hit_tp=1 / hit_sl=0).

Does NOT modify app/services/technicals.py, scoring.py, or engine.py -- duplicates the
component arithmetic locally (verbatim copy of compute_confidence's logic, lines 1467-1627)
so each component's raw point value can be extracted instead of only the final summed score.

Run: docker-compose exec -T app python -m scripts.diagnose_confidence_components
"""
from __future__ import annotations

import math
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.services import technicals as tech
from app.services.backtesting.data_provider import prefetch_symbols

RUN_ID = "062af0ff-8a44-44ed-bbc6-e691280db486"


def _components(snapshot, direction: str, regime):
    """Verbatim reimplementation of compute_confidence's indicator components (sentiment
    forced to None/0 since it is not persisted per-trade -- see report caveat), returning
    each component's raw point contribution separately instead of the summed/normalized total."""
    is_long = direction == "long"
    rsi_bull, rsi_bear, rsi_ob, rsi_os = tech._regime_rsi_thresholds(regime)
    st_bull, st_bear, st_ob, st_os = tech._regime_stoch_thresholds(regime)

    c = {"sentiment": 0.0, "mentions": 0.0, "adx": 0.0, "rsi": 0.0, "macd": 0.0,
         "stochastic": 0.0, "obv": 0.0, "bollinger": 0.0, "vwap": 0.0, "ema200": 0.0,
         "consensus_penalty": 0.0, "conflict_penalty": 0.0}

    snap = snapshot
    if snap is None:
        return c

    if snap.adx is not None:
        adx_val, plus_di, minus_di = snap.adx
        trend_aligns = (is_long and plus_di > minus_di) or (not is_long and minus_di > plus_di)
        if trend_aligns:
            if adx_val >= 40:
                c["adx"] = 15
            elif adx_val >= 25:
                c["adx"] = 10
            elif adx_val >= 15:
                c["adx"] = 5

    if snap.rsi is not None:
        if is_long:
            if rsi_bear <= snap.rsi <= rsi_ob:
                c["rsi"] = 15
            elif snap.rsi < rsi_os:
                c["rsi"] = 12
            elif snap.rsi < rsi_bear:
                c["rsi"] = 8
        else:
            if rsi_os <= snap.rsi <= rsi_bull:
                c["rsi"] = 15
            elif snap.rsi > rsi_ob:
                c["rsi"] = 12
            elif snap.rsi > rsi_bull:
                c["rsi"] = 8

    if snap.macd is not None:
        _, _, hist = snap.macd
        adx_now = snap.adx[0] if snap.adx is not None else 25
        macd_max = 15 if adx_now >= 20 else 7
        if (is_long and hist > 0) or (not is_long and hist < 0):
            c["macd"] = macd_max
        elif abs(hist) / max(snap.price, 1) < 0.001:
            c["macd"] = min(5, macd_max // 2)

    if snap.stochastic is not None:
        k, d = snap.stochastic
        if is_long:
            if st_os <= k <= st_ob and k > d:
                c["stochastic"] = 10
            elif k < st_os:
                c["stochastic"] = 7
        else:
            if st_os <= k <= st_ob and k < d:
                c["stochastic"] = 10
            elif k > st_ob:
                c["stochastic"] = 7

    if snap.obv is not None:
        if (is_long and snap.obv[1] > 0) or (not is_long and snap.obv[1] < 0):
            c["obv"] = 10

    if snap.bollinger is not None:
        upper, mid, lower = snap.bollinger
        if is_long:
            if snap.price > mid and not snap.near_upper_band:
                c["bollinger"] = 10
            elif snap.near_lower_band:
                c["bollinger"] = 8
        else:
            if snap.price < mid and not snap.near_lower_band:
                c["bollinger"] = 10
            elif snap.near_upper_band:
                c["bollinger"] = 8

    if snap.vwap is not None and snap.price != snap.vwap:
        if (is_long and snap.price_above_vwap) or (not is_long and not snap.price_above_vwap):
            c["vwap"] = 5

    if snap.ema200 is not None:
        price_above_ema200 = snap.price > snap.ema200
        if (is_long and price_above_ema200) or (not is_long and not price_above_ema200):
            c["ema200"] = 10
        else:
            c["ema200"] = -8

    agree = 0
    if snap.macd is not None:
        agree += 1 if (is_long and snap.macd[2] > 0) or (not is_long and snap.macd[2] < 0) else 0
    if snap.adx is not None:
        _, plus_di, minus_di = snap.adx
        agree += 1 if (is_long and plus_di > minus_di) or (not is_long and minus_di > plus_di) else 0
    if snap.obv is not None:
        agree += 1 if (is_long and snap.obv[1] > 0) or (not is_long and snap.obv[1] < 0) else 0
    if snap.rsi is not None:
        agree += 1 if (is_long and snap.rsi >= 50) or (not is_long and snap.rsi < 50) else 0
    if snap.stochastic is not None:
        k, _ = snap.stochastic
        agree += 1 if (is_long and k >= 50) or (not is_long and k < 50) else 0
    if agree < 3:
        c["consensus_penalty"] = -12

    conflict_penalty = tech.get_indicator_conflict_penalty(snap, direction, regime, penalty_scale=1.0)
    c["conflict_penalty"] = -conflict_penalty

    return c


def point_biserial(points: list[float], outcomes: list[int]) -> float:
    n = len(points)
    if n < 10:
        return float("nan")
    mean_p = sum(points) / n
    mean_o = sum(outcomes) / n
    var_p = sum((p - mean_p) ** 2 for p in points)
    var_o = sum((o - mean_o) ** 2 for o in outcomes)
    if var_p == 0 or var_o == 0:
        return float("nan")
    cov = sum((points[i] - mean_p) * (outcomes[i] - mean_o) for i in range(n))
    return cov / math.sqrt(var_p * var_o)


def main():
    sync_url = settings.database_url.replace("+aiosqlite", "").replace("+asyncpg", "")
    engine = sa.create_engine(sync_url)
    Session = sessionmaker(bind=engine)
    s = Session()

    rows = s.execute(sa.text(
        "select symbol, asset_category, direction, entry_date, outcome, pnl_r, market_regime, "
        "confidence_score from backtest_trades where run_id=:rid and outcome in ('hit_tp','hit_sl')"
    ), {"rid": RUN_ID}).fetchall()
    s.close()
    print(f"[DIAG] {len(rows)} resolved trades loaded")

    symbols = sorted({r.symbol for r in rows})
    print(f"[DIAG] {len(symbols)} distinct symbols -- prefetching OHLCV (period=4y)...")
    tech.clear_cache()
    prefetch_symbols(symbols, period="4y")

    records = []
    skipped = 0
    import pandas as pd
    for i, r in enumerate(rows):
        tech.set_backtest_mode(pd.Timestamp(r.entry_date))
        snap = tech.build_snapshot(r.symbol)
        if snap is None or snap.price <= 0:
            skipped += 1
            continue
        comp = _components(snap, r.direction, r.market_regime)
        win = 1 if r.outcome == "hit_tp" else 0
        records.append({
            "symbol": r.symbol, "category": r.asset_category, "direction": r.direction,
            "regime": r.market_regime, "win": win, "pnl_r": r.pnl_r,
            "stored_confidence": r.confidence_score, **comp,
        })
        if (i + 1) % 1000 == 0:
            print(f"[DIAG] {i + 1}/{len(rows)} processed, {skipped} skipped (no snapshot)")

    tech.clear_backtest_mode()
    print(f"[DIAG] DONE: {len(records)} records built, {skipped} skipped\n")

    import json
    with open("/tmp/confidence_components_raw.json", "w") as f:
        json.dump(records, f, default=str)

    components = ["sentiment", "mentions", "adx", "rsi", "macd", "stochastic", "obv",
                  "bollinger", "vwap", "ema200", "consensus_penalty", "conflict_penalty"]

    outcomes = [rec["win"] for rec in records]
    overall_wr = sum(outcomes) / len(outcomes)
    print(f"Overall WR (resolved trades, this sample): {overall_wr:.1%}  n={len(records)}\n")

    print(f"{'component':<18}{'corr':>8}{'n_nonzero':>10}{'WR@max':>10}{'WR@0':>10}{'n_max':>8}{'n_zero':>8}")
    results = []
    for comp in components:
        pts = [rec[comp] for rec in records]
        nonzero = [p for p in pts if p != 0]
        corr = point_biserial(pts, outcomes)
        maxval = max(pts) if nonzero else 0
        at_max = [i for i, p in enumerate(pts) if p == maxval and maxval != 0]
        at_zero = [i for i, p in enumerate(pts) if p == 0]
        wr_max = sum(outcomes[i] for i in at_max) / len(at_max) if at_max else float("nan")
        wr_zero = sum(outcomes[i] for i in at_zero) / len(at_zero) if at_zero else float("nan")
        results.append((comp, corr, len(nonzero), wr_max, wr_zero, len(at_max), len(at_zero)))
        print(f"{comp:<18}{corr:>8.3f}{len(nonzero):>10}{wr_max:>9.1%}{wr_zero:>9.1%}{len(at_max):>8}{len(at_zero):>8}")

    print("\n-- Ranked by |correlation| --")
    for comp, corr, nz, wrmax, wrzero, nmax, nzero in sorted(results, key=lambda t: abs(t[1]) if t[1] == t[1] else 0, reverse=True):
        print(f"  {comp:<18} corr={corr:+.3f}  WR@max={wrmax:.1%} (n={nmax})  WR@0={wrzero:.1%} (n={nzero})")

    # Category breakdown for the top 3 components by |corr|
    print("\n-- Category breakdown for top components --")
    top3 = [t[0] for t in sorted(results, key=lambda t: abs(t[1]) if t[1] == t[1] else 0, reverse=True)[:3]]
    cats = sorted({rec["category"] for rec in records})
    for comp in top3:
        print(f"\n  {comp}:")
        for cat in cats:
            sub = [rec for rec in records if rec["category"] == cat]
            if len(sub) < 30:
                continue
            pts = [rec[comp] for rec in sub]
            outs = [rec["win"] for rec in sub]
            corr = point_biserial(pts, outs)
            print(f"    {cat:<14} n={len(sub):<6} corr={corr:+.3f}")


if __name__ == "__main__":
    main()
