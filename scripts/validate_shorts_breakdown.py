"""
Phase 5a validation gate: run the breakdown-short generator over pooled bear/risk_off windows and
report signal frequency + short-only outcome metrics. Go/no-go for shipping breakdown-only.

Walk-forward safe: sets technicals backtest cutoff per day before building each snapshot, and scores
outcomes on FUTURE bars only.

Run in the container:
  docker-compose exec -T app python scripts/validate_shorts_breakdown.py
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from app.services import technicals as tech
from app.services.backtesting.data_provider import (
    prefetch_symbols,
    get_trading_days,
    get_future_bars,
)
from app.services.backtesting.regime import build_regime_calendar
from app.services.backtesting.simulator import simulate_outcome
from app.services.recommendations.shorts_signals import (
    generate_short_signal,
    _tape_deteriorating,
    _FADE_TOP_TRIGGERS,
)

# Short candidate watchlist (large-caps + sector leaders that distribute first — per plan;
# 15-20+ names multiply signal opportunity without changing signal logic).
SHORT_WATCHLIST = [
    # Mega-cap / high-beta individual names
    "AAPL", "MSFT", "NVDA", "META", "AMZN", "TSLA", "GOOGL", "NFLX", "AMD", "AVGO",
    "CRM", "COST", "HD", "JPM", "BAC",
    # Sector + broad ETFs (distribute together at tops)
    "XLF", "XLY", "XLK", "XLI", "XLE", "SMH", "HYG", "JNK", "IWM", "SPY", "QQQ", "DIA",
]

# Windows extended earlier to include the TOP-FORMATION period (market peak + early distribution)
# feeding into each bear — fade-tops fire at the top (above EMA200), breakdowns in the ensuing bear.
WINDOWS = [
    (date(2015, 5, 1), date(2016, 2, 29)),    # 2015 top -> China deval / oil crash selloff
    (date(2018, 8, 1), date(2018, 12, 31)),   # Sep-2018 top -> Q4 2018 drawdown
    (date(2020, 1, 1), date(2020, 4, 30)),    # Jan/Feb-2020 top -> COVID crash
    (date(2021, 11, 1), date(2022, 12, 31)),  # late-2021 distribution -> 2022 Fed bear
]

_OUTCOME_MAX_BARS = 30  # ~ the 14-day-prediction horizon in trading bars


def main() -> None:
    tech.clear_cache()
    tech.clear_backtest_mode()
    ohlcv = prefetch_symbols(SHORT_WATCHLIST + ["^GSPC", "^VIX"], period="max")
    signals: list[dict] = []

    skipped_overlap = 0
    try:
        for start, end in WINDOWS:
            days = get_trading_days(ohlcv, start, end)
            calendar = build_regime_calendar(days, ohlcv)
            # De-dup: one open position per symbol at a time. open_until[sym] = close date of the
            # currently-open trade; new signals for that symbol before it closes are skipped.
            vix_df = ohlcv.get("^VIX")
            spx_df = ohlcv.get("^GSPC")
            open_until: dict[str, pd.Timestamp] = {}
            for d in days:
                regime = calendar.get(d, "neutral")
                if regime in ("crisis", "high_vol"):
                    continue  # both families hard-blocked
                # Fade-top activation (Option 1): risk turning up while price still high.
                ts = pd.Timestamp(d)
                deteriorating = False
                if vix_df is not None and spx_df is not None:
                    vix_s = vix_df["Close"][vix_df.index <= ts]
                    spx_s = spx_df["Close"][spx_df.index <= ts]
                    deteriorating = _tape_deteriorating(vix_s, spx_s)
                if regime != "bear" and not deteriorating:
                    continue  # neither breakdown (bear) nor fade-top (deteriorating) active
                tech.set_backtest_mode(ts)  # walk-forward: snapshot sees data <= d
                for symbol in SHORT_WATCHLIST:
                    if symbol in open_until and ts <= open_until[symbol]:
                        skipped_overlap += 1
                        continue  # a position on this symbol is still open
                    df = ohlcv.get(symbol)
                    if df is None or df.empty:
                        continue
                    bars = df[df.index <= ts]
                    if len(bars) < 60:
                        continue
                    snap = tech.build_snapshot(symbol)
                    if snap is None or snap.price <= 0:
                        continue
                    sig = generate_short_signal(
                        symbol, snap, bars, regime_id=regime, category="stocks_large",
                        risk_tier="moderate", preferred_risk="moderate",
                        sentiment_avg=None, sentiment_count=0, today=d,
                        tape_deteriorating=deteriorating,
                    )
                    if sig is None:
                        continue
                    future = get_future_bars(symbol, d, _OUTCOME_MAX_BARS, ohlcv)
                    outcome = simulate_outcome(
                        sig.entry, sig.stop_loss, sig.take_profit, "short", future,
                        max_bars=_OUTCOME_MAX_BARS,
                    )
                    signals.append({
                        "date": d, "symbol": symbol, "trigger": sig.trigger,
                        "confidence": sig.confidence, "pnl_r": outcome.pnl_r,
                        "outcome": outcome.outcome,
                    })
                    if outcome.outcome_date is not None:
                        open_until[symbol] = pd.Timestamp(outcome.outcome_date)
    finally:
        tech.clear_backtest_mode()

    _FRICTION = 0.1  # per-short friction haircut (R), per the shorts-recovery-plan

    def _report(label: str, subset: list[dict]) -> None:
        n = len(subset)
        resolved = [s for s in subset if s["outcome"] in ("hit_tp", "hit_sl")]
        wins = sum(1 for s in resolved if s["outcome"] == "hit_tp")
        avg_r = (sum(s["pnl_r"] for s in subset) / n) if n else 0.0
        wr = (wins / len(resolved)) if resolved else 0.0
        net = avg_r - _FRICTION
        freq = "PASS" if n >= 30 else "FAIL"
        edge = "PASS" if (avg_r > 0 and wr > 0.33) else "FAIL"
        by_trigger: dict[str, int] = {}
        for s in subset:
            by_trigger[s["trigger"]] = by_trigger.get(s["trigger"], 0) + 1
        print(f"[{label}] signals={n} resolved={len(resolved)} WR={wr:.3f} "
              f"avg_pnl_r={avg_r:.4f} net_of_friction={net:.4f}")
        print(f"    GATE freq n>=30 -> {freq}   GATE edge avg_r>0 & WR>0.33 -> {edge}")
        print(f"    by_trigger: {by_trigger}")

    breakdown = [s for s in signals if s["trigger"] not in _FADE_TOP_TRIGGERS]
    fade_top = [s for s in signals if s["trigger"] in _FADE_TOP_TRIGGERS]

    print("=== Shorts engine validation (de-duped; 4 bear windows; net of 0.1R friction) ===")
    print(f"(overlap_skipped={skipped_overlap})")
    _report("ALL", signals)
    _report("BREAKDOWN", breakdown)
    _report("FADE_TOP", fade_top)


if __name__ == "__main__":
    main()
