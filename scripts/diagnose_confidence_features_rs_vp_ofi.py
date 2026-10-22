"""
Throwaway diagnostic (read-only): for backtest run 062af0ff-8a44-44ed-bbc6-e691280db486,
correlate four already-built-but-underused features against actual outcome (hit_tp=1/hit_sl=0),
reusing the replay-and-reconstruct approach from scripts/diagnose_confidence_components.py:
  1. Cross-sectional relative strength: rs_20d, rs_60d, sector_rs (compute_relative_strength)
  2. Volume-profile positioning: vp_bias, in_value_area, vp_overextension (TechnicalSnapshot)
  3. OFI as a continuous feature: ofi_ratio (TechnicalSnapshot)
  4. Sentiment: checked for feasibility only (see report) -- feed_items has real historical
     sentiment data (2022-11-07 .. 2026-09-18) but no stored backtest run ever consumed it, and
     replicating app/services/analysis/aggregate.py's decay/source/entity-weighted aggregation
     per-trade (7124 distinct historical as-of dates) is a materially bigger lift than 1-3 --
     flagged, not attempted here.

Does NOT modify app/services/technicals.py, scoring.py, market_context.py or engine.py.

Run: docker-compose exec -T app python -m scripts.diagnose_confidence_features_rs_vp_ofi
"""
from __future__ import annotations

import math
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.services import technicals as tech
from app.services.technicals import compute_relative_strength
from app.services.market_context import get_sector_etf_for_asset
from app.services.recommendations.engine import get_yf_symbol, _STOCK_CATEGORIES
from app.services.backtesting.data_provider import prefetch_symbols

RUN_ID = "062af0ff-8a44-44ed-bbc6-e691280db486"


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
        "select symbol, asset_category, direction, entry_date, outcome, pnl_r, market_regime "
        "from backtest_trades where run_id=:rid and outcome in ('hit_tp','hit_sl')"
    ), {"rid": RUN_ID}).fetchall()
    s.close()
    print(f"[DIAG] {len(rows)} resolved trades loaded")

    symbols = sorted({r.symbol for r in rows})
    # Resolve RS benchmark/sector symbols up front so they're in the prefetch batch too.
    extra = {"^GSPC"}
    sector_map: dict[str, str | None] = {}
    yf_map: dict[tuple, str] = {}
    for r in rows:
        key = (r.asset_category, r.symbol)
        if key not in yf_map:
            yf_map[key] = get_yf_symbol(r.asset_category, r.symbol) or r.symbol.upper()
        if r.symbol not in sector_map:
            try:
                etf = get_sector_etf_for_asset(r.symbol)
            except Exception:
                etf = None
            sector_map[r.symbol] = etf
            if etf:
                extra.add(etf)

    all_symbols = sorted(set(symbols) | extra | {v for v in yf_map.values()})
    print(f"[DIAG] {len(all_symbols)} distinct symbols (incl. benchmark/sector ETFs) -- prefetching OHLCV (period=4y)...")
    tech.clear_cache()
    prefetch_symbols(all_symbols, period="4y")

    records = []
    skipped = 0
    import pandas as pd
    for i, r in enumerate(rows):
        yf_sym = yf_map[(r.asset_category, r.symbol)]
        tech.set_backtest_mode(pd.Timestamp(r.entry_date))
        snap = tech.build_snapshot(yf_sym)
        if snap is None or snap.price <= 0:
            skipped += 1
            continue

        rs_20d = rs_60d = sector_rs = None
        if r.asset_category in _STOCK_CATEGORIES:
            try:
                rs_20d = compute_relative_strength(yf_sym, "^GSPC", period=20)
                rs_60d = compute_relative_strength(yf_sym, "^GSPC", period=60)
                etf = sector_map.get(r.symbol)
                if etf:
                    sector_rs = compute_relative_strength(yf_sym, etf, period=20)
            except Exception:
                pass

        win = 1 if r.outcome == "hit_tp" else 0
        records.append({
            "symbol": r.symbol, "category": r.asset_category, "direction": r.direction,
            "regime": r.market_regime, "win": win, "pnl_r": r.pnl_r,
            "rs_20d": rs_20d, "rs_60d": rs_60d, "sector_rs": sector_rs,
            "ofi_ratio": snap.ofi_ratio,
            "vp_overextension": snap.vp_overextension,
            "in_value_area": 1 if snap.in_value_area else 0,
            "vp_bias_above": 1 if snap.vp_bias == "above_vpoc" else (0 if snap.vp_bias == "below_vpoc" else None),
        })
        if (i + 1) % 1000 == 0:
            print(f"[DIAG] {i + 1}/{len(rows)} processed, {skipped} skipped (no snapshot)")

    tech.clear_backtest_mode()
    print(f"[DIAG] DONE: {len(records)} records built, {skipped} skipped\n")

    import json
    with open("/tmp/confidence_features_rs_vp_ofi_raw.json", "w") as f:
        json.dump(records, f, default=str)

    overall_wr = sum(rec["win"] for rec in records) / len(records)
    print(f"Overall WR (resolved trades, this sample): {overall_wr:.1%}  n={len(records)}\n")

    numeric_feats = ["rs_20d", "rs_60d", "sector_rs", "ofi_ratio", "vp_overextension",
                      "in_value_area", "vp_bias_above"]

    print(f"{'feature':<18}{'corr':>8}{'n_nonnull':>12}")
    results = []
    for feat in numeric_feats:
        pairs = [(rec[feat], rec["win"]) for rec in records if rec[feat] is not None]
        if len(pairs) < 30:
            print(f"{feat:<18}{'n/a':>8}{len(pairs):>12}  (too few non-null)")
            continue
        pts = [p for p, _ in pairs]
        outs = [o for _, o in pairs]
        corr = point_biserial(pts, outs)
        results.append((feat, corr, len(pairs)))
        print(f"{feat:<18}{corr:>8.3f}{len(pairs):>12}")

    print("\n-- Ranked by |correlation| --")
    for feat, corr, n in sorted(results, key=lambda t: abs(t[1]) if t[1] == t[1] else 0, reverse=True):
        print(f"  {feat:<18} corr={corr:+.3f}  n={n}")

    # Category breakdown for RS features (stocks only, by construction) and the overall top feature
    print("\n-- Category breakdown (stocks-only features: rs_20d, rs_60d, sector_rs) --")
    cats = sorted({rec["category"] for rec in records if rec["category"] in _STOCK_CATEGORIES})
    for feat in ("rs_20d", "rs_60d", "sector_rs"):
        print(f"\n  {feat}:")
        for cat in cats:
            sub = [rec for rec in records if rec["category"] == cat and rec[feat] is not None]
            if len(sub) < 30:
                continue
            pts = [rec[feat] for rec in sub]
            outs = [rec["win"] for rec in sub]
            corr = point_biserial(pts, outs)
            print(f"    {cat:<14} n={len(sub):<6} corr={corr:+.3f}")

    print("\n-- Category breakdown (ofi_ratio, vp_overextension -- all categories) --")
    all_cats = sorted({rec["category"] for rec in records})
    for feat in ("ofi_ratio", "vp_overextension"):
        print(f"\n  {feat}:")
        for cat in all_cats:
            sub = [rec for rec in records if rec["category"] == cat and rec[feat] is not None]
            if len(sub) < 30:
                continue
            pts = [rec[feat] for rec in sub]
            outs = [rec["win"] for rec in sub]
            corr = point_biserial(pts, outs)
            print(f"    {cat:<14} n={len(sub):<6} corr={corr:+.3f}")


if __name__ == "__main__":
    main()
