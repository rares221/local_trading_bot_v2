"""
Throwaway diagnostic (read-only, no production code changes): tallies how often stocks_mid /
stocks_small symbols compute direction="short" (and therefore get dropped via the
short_disabled path, since SHORTS_ENABLED=False and SHORTS_BEAR_REGIME_ENABLED=False mean
ANY short is unconditionally dropped) over a recent walk-forward window, broken down by
regime. Follow-up to the live single-day finding (2026-10-22: 100% stocks_small, 86%
stocks_mid short_disabled) -- checks whether that was typical or an outlier.

Run: docker-compose exec -T app python -m scripts.diagnose_midsmall_short_skew
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import pandas as pd

import app.services.technicals as tech
from app.services.backtesting.data_provider import prefetch_symbols, get_trading_days
from app.services.backtesting.regime import build_regime_calendar
from app.services.technicals import build_snapshot, determine_direction

MID_SYMBOLS = ["PLTR", "SMCI", "IONQ", "RIVN", "ENPH", "FSLR", "CCJ", "MP", "SOFI", "HOOD", "RIG", "HP", "HIMS", "RKLB"]
SMALL_SYMBOLS = ["BBAI", "SOUN", "LUNR", "ACHR", "ASTS", "JOBY", "ACMR", "ARRY", "RXRX", "OPEN"]
LARGE_SYMBOLS = ["AAPL", "MSFT", "GOOGL", "NVDA", "META", "AMZN", "TSLA", "AVGO", "JPM", "BAC",
                  "GS", "JNJ", "UNH", "XOM", "CVX", "BP", "SHEL", "NEM", "PG", "WMT", "V"]

MIN_BARS_FOR_SIGNAL = 35


def main() -> None:
    all_symbols = MID_SYMBOLS + SMALL_SYMBOLS + LARGE_SYMBOLS
    tech.clear_cache()
    # ^GSPC and ^VIX MUST be in the returned ohlcv_data dict (not just tech's cache) --
    # build_regime_calendar reads them straight out of this dict by key.
    ohlcv_data = prefetch_symbols(
        all_symbols, period="1y",
        extra_symbols=["^GSPC", "^VIX", "XLK", "XLE", "XLF", "XLV", "XLY", "XLI", "XLB"],
    )

    end_date = date.today()
    start_date = end_date - timedelta(days=365)
    trading_days = get_trading_days(ohlcv_data, start_date, end_date)
    print(f"[DIAG] Trading days in window: {len(trading_days)} ({trading_days[0]} -> {trading_days[-1]})")

    regime_calendar = build_regime_calendar(trading_days, ohlcv_data)
    regime_counts = defaultdict(int)
    for d in trading_days:
        regime_counts[regime_calendar.get(d, "neutral")] += 1
    print(f"[DIAG] Regime distribution over window: {dict(regime_counts)}")

    # tally[category][regime][direction] = count
    tally: dict[str, dict[str, dict[str, int]]] = {
        "stocks_mid": defaultdict(lambda: defaultdict(int)),
        "stocks_small": defaultdict(lambda: defaultdict(int)),
        "stocks_large": defaultdict(lambda: defaultdict(int)),
    }
    cat_symbols = {"stocks_mid": MID_SYMBOLS, "stocks_small": SMALL_SYMBOLS, "stocks_large": LARGE_SYMBOLS}

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
                        continue  # hard-blocked before direction even matters
                    direction = determine_direction(None, snap, regime=day_regime)
                    tally[category][day_regime][direction] += 1
                except Exception as e:
                    print(f"[DIAG] error {category} {symbol} @{sim_date}: {e}")

    tech.clear_backtest_mode()

    print("\n" + "=" * 90)
    print("short_disabled rate by category x regime (SHORTS_ENABLED=False, SHORTS_BEAR_REGIME_ENABLED=False")
    print("-> ANY direction=='short' is unconditionally dropped, regardless of regime)")
    print("=" * 90)
    for category in ("stocks_large", "stocks_mid", "stocks_small"):
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


if __name__ == "__main__":
    main()
