"""
Throwaway diagnostic (read-only, no production code changes): root-causes the 5 historical
stocks_large zero-output batch days (2026-04-08/09/10/12/14) found by
diagnose_large_indices_coverage.py. Replays each date by setting technicals.py's backtest cutoff
(which build_market_context respects too, via the shared fetch_ohlcv) and calling the REAL
engine.build_daily_candidates_for_category("stocks_large", ...) directly -- the exact production
code path, not replay.py's separate/simplified implementation -- capturing every [ENGINE_SKIP]
log line emitted.

Run: docker-compose exec -T app python -m scripts.diagnose_large_zero_output_days
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date

import pandas as pd

import app.services.technicals as tech
from app.services.backtesting.data_provider import prefetch_symbols
from app.services.recommendations.engine import build_daily_candidates_for_category
from app.services.market_context import build_market_context
from app.core.preferences import get_preferred_risk

LARGE_SYMBOLS = ["AAPL", "MSFT", "GOOGL", "NVDA", "META", "AMZN", "TSLA", "AVGO", "JPM", "BAC",
                  "GS", "JNJ", "UNH", "XOM", "CVX", "BP", "SHEL", "NEM", "PG", "WMT", "V"]
TARGET_DATES = [date(2026, 4, 8), date(2026, 4, 9), date(2026, 4, 10),
                 date(2026, 4, 12), date(2026, 4, 14)]


class _SkipCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if msg.startswith("[ENGINE_SKIP]"):
            try:
                payload = json.loads(msg[len("[ENGINE_SKIP] "):])
                self.records.append(payload)
            except Exception:
                pass


def main() -> None:
    preferred_risk = get_preferred_risk()
    print(f"[DIAG] preferred_risk (production value) = {preferred_risk}")

    engine_logger = logging.getLogger("app.services.recommendations.engine")
    capture = _SkipCapture()
    capture.setLevel(logging.INFO)
    engine_logger.addHandler(capture)
    engine_logger.setLevel(logging.INFO)

    tech.clear_cache()
    extra = ["^GSPC", "^VIX", "XLK", "XLE", "XLF", "XLV", "XLY", "XLI", "XLB", "XLU", "XLP", "XLRE"]
    prefetch_symbols(LARGE_SYMBOLS, period="2y", extra_symbols=extra)

    per_date_results = {}
    for d in TARGET_DATES:
        capture.records.clear()
        cutoff_ts = pd.Timestamp(d)
        tech.set_backtest_mode(cutoff_ts)
        try:
            market_ctx = build_market_context(include_sectors=True)
            candidates = build_daily_candidates_for_category(
                "stocks_large", max_count=5, by_asset_sentiment=None,
                preferred_risk=preferred_risk, market_ctx=market_ctx,
            )
        except Exception as e:
            print(f"[DIAG] {d}: EXCEPTION {e!r}")
            tech.clear_backtest_mode()
            continue
        tech.clear_backtest_mode()

        reason_counts: dict[str, int] = defaultdict(int)
        for rec in capture.records:
            if rec.get("category") == "stocks_large" or rec.get("category") is None:
                reason_counts[rec["reason"]] += 1

        per_date_results[d] = {
            "regime": market_ctx.regime.regime,
            "n_candidates": len(candidates),
            "reasons": dict(reason_counts),
        }
        print(f"\n--- {d} (regime={market_ctx.regime.regime}) ---")
        print(f"  candidates produced: {len(candidates)}")
        for c in candidates:
            print(f"    {c.asset_id} dir={c.direction} conf={c.confidence_score}")
        print(f"  skip reason tally: {dict(reason_counts)}")

    print("\n" + "=" * 90)
    print("SUMMARY across all 5 zero-output days")
    print("=" * 90)
    all_reasons: dict[str, int] = defaultdict(int)
    regimes_seen = set()
    for d, res in per_date_results.items():
        regimes_seen.add(res["regime"])
        for r, cnt in res["reasons"].items():
            all_reasons[r] += cnt
    print(f"Regimes across the 5 days: {regimes_seen}")
    print(f"Aggregated skip-reason counts: {dict(sorted(all_reasons.items(), key=lambda kv: -kv[1]))}")


if __name__ == "__main__":
    main()
