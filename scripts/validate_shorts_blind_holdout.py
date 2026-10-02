"""
Shorts engine Phase 1 redesign — blind holdout validation.

These two historical episodes were NEVER looked at during diagnosis or redesign (only
2021-11..2024-12 was used for that). Per the approved success bar: OOS-equivalent avg
expectancy >= +0.15R, n >= 40 resolved trades, holding on >= 2 of these episodes.

Thresholds in shorts_signals.py were set from principled reasoning (reusing the long
engine's own calibrated RS scale, standard R:R breakeven math) BEFORE this script was run —
not tuned against this data. Run once; do not iterate thresholds against these episodes.

Run inside the container:
    docker-compose exec -T app python -m scripts.validate_shorts_blind_holdout
"""
from __future__ import annotations

import json
from datetime import date

import app.services.recommendations.engine as engine_mod
from app.services import technicals as tech
from app.services.backtesting.data_provider import prefetch_symbols, get_trading_days
from app.services.backtesting.regime import build_regime_calendar
from app.services.backtesting.replay import _replay_window
from app.services.recommendations.shorts_engine_io import SHORT_UNIVERSE
from scripts.diagnose_shorts_tier1 import summarize, breakdown_by, family_of, conf_bucket

# Never touched during Phase 1 design/tuning of this session.
EPISODES = [
    ("2018_Q4_selloff", date(2018, 10, 1), date(2018, 12, 31)),
    ("2020_COVID_crash_and_recovery", date(2020, 2, 1), date(2020, 6, 30)),
]

PREFETCH_PERIOD = "10y"  # must parse via the "Ny" fallback, not the Finnhub period dict
RISK_TIER = "moderate"
MIN_CONFIDENCE = 40
SYMBOLS = sorted({yf for (_c, _a, yf, _r) in SHORT_UNIVERSE} | {"^GSPC", "^VIX"})


def run_episode(name: str, start: date, end: date) -> list[dict]:
    print(f"\n[HOLDOUT] === {name}: {start}..{end} ===")
    tech.clear_cache()
    ohlcv_data = prefetch_symbols(SYMBOLS, period=PREFETCH_PERIOD)
    for sym in ["^GSPC", "^VIX"]:
        tech.fetch_ohlcv(sym, period=PREFETCH_PERIOD)
    if not ohlcv_data:
        print(f"[HOLDOUT] {name}: ERROR no data fetched")
        return []

    trading_days = get_trading_days(ohlcv_data, start, end)
    if not trading_days:
        print(f"[HOLDOUT] {name}: ERROR no trading days in range (check prefetch period)")
        return []
    print(f"[HOLDOUT] {name}: {len(trading_days)} trading days, {len(SYMBOLS)} symbols")

    regime_calendar = build_regime_calendar(trading_days, ohlcv_data)
    signals = _replay_window(
        ohlcv_data, regime_calendar, SYMBOLS, start, end, RISK_TIER, MIN_CONFIDENCE, 14,
    )
    shorts = [s for s in signals if s.get("direction") == "short"]
    print(f"[HOLDOUT] {name}: {len(shorts)} short signals")
    return shorts


def main() -> None:
    engine_mod.SHORTS_ENGINE_ENABLED = True

    all_shorts: list[dict] = []
    per_episode: dict[str, list[dict]] = {}
    for name, start, end in EPISODES:
        shorts = run_episode(name, start, end)
        per_episode[name] = shorts
        all_shorts.extend(shorts)

    print("\n" + "=" * 90)
    print("BLIND HOLDOUT VALIDATION — shorts engine Phase 1 redesign")
    print("=" * 90)

    bar_results = []
    for name, shorts in per_episode.items():
        s = summarize(shorts, name)
        print(f"\n--- {name} ---")
        print(json.dumps(s, indent=2))
        resolved = (s["wins"] or 0) + (s["losses"] or 0)
        clears = s["avg_pnl_r"] is not None and s["avg_pnl_r"] >= 0.15
        bar_results.append((name, resolved, s["avg_pnl_r"], clears))
        print("--- by family ---")
        for row in breakdown_by(shorts, lambda t: family_of(t.get("trigger", "")), "family"):
            print(json.dumps(row))
        print("--- by trigger ---")
        for row in breakdown_by(shorts, lambda t: t.get("trigger", "none"), "trigger"):
            print(json.dumps(row))

    print("\n--- COMBINED (both episodes pooled) ---")
    combined = summarize(all_shorts, "COMBINED")
    print(json.dumps(combined, indent=2))

    print("\n" + "=" * 90)
    print("SUCCESS BAR CHECK: expectancy >= +0.15R, n>=40 resolved, holds on >=2 episodes")
    print("=" * 90)
    n_resolved_total = sum(r[1] for r in bar_results)
    n_episodes_clearing = sum(1 for r in bar_results if r[3])
    for name, resolved, avg_r, clears in bar_results:
        print(f"  {name}: resolved={resolved} avg_pnl_r={avg_r} clears_+0.15R={clears}")
    print(f"  TOTAL resolved (both episodes) = {n_resolved_total} (bar: >=40)")
    print(f"  Episodes clearing +0.15R = {n_episodes_clearing}/2 (bar: >=2)")
    overall_pass = (
        n_resolved_total >= 40
        and n_episodes_clearing >= 2
        and (combined["avg_pnl_r"] is not None and combined["avg_pnl_r"] >= 0.15)
    )
    print(f"  OVERALL BAR MET: {overall_pass}")

    out_path = "/tmp/shorts_blind_holdout_raw.json"
    with open(out_path, "w") as f:
        json.dump(per_episode, f, default=str)
    print(f"\n[HOLDOUT] Raw trades dumped to {out_path}")


if __name__ == "__main__":
    main()
