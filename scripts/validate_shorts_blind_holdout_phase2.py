"""
Shorts engine Phase 2 redesign — blind holdout validation.

2018 Q4 and 2020 COVID (used for Phase 1's blind holdout) are now SPENT — this session already
saw their outcomes, so re-using them here would be exactly the "peek, tweak, re-check"
contamination the whole validation discipline exists to prevent. Two genuinely fresh episodes,
never touched during Phase 2 design:

  - Aug-Oct 2011: US debt-ceiling crisis + European sovereign debt crisis, VIX >40.
  - Aug 2015-Feb 2016: China currency devaluation + oil crash, a genuine multi-month selloff.

Same success bar as Phase 1, carried forward unchanged: OOS-equivalent avg expectancy >=
+0.15R, n >= 40 resolved trades, holding on >= 2 of these episodes.

The confirmation window (_CONFIRMATION_WINDOW_DAYS=3) and EMA span (_CONFIRMATION_EMA_SPAN=10)
in shorts_signals.py were set from principled reasoning (standard "don't enter on the trigger
bar" practice), not tuned against this data. Run once; do not iterate against these episodes.

Run inside the container:
    docker-compose exec -T app python -m scripts.validate_shorts_blind_holdout_phase2
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

# Never touched during Phase 2 design/tuning. 2018 Q4 and 2020 COVID (Phase 1's holdout) are
# now spent -- do NOT reuse them here.
EPISODES = [
    ("2011_debt_ceiling_euro_crisis", date(2011, 8, 1), date(2011, 10, 31)),
    ("2015_16_china_oil_selloff", date(2015, 8, 1), date(2016, 2, 29)),
]

PREFETCH_PERIOD = "20y"  # must parse via the "Ny" fallback, not the Finnhub period dict
RISK_TIER = "moderate"
MIN_CONFIDENCE = 40
SYMBOLS = sorted({yf for (_c, _a, yf, _r) in SHORT_UNIVERSE} | {"^GSPC", "^VIX"})


def run_episode(name: str, start: date, end: date) -> list[dict]:
    print(f"\n[HOLDOUT2] === {name}: {start}..{end} ===")
    tech.clear_cache()
    ohlcv_data = prefetch_symbols(SYMBOLS, period=PREFETCH_PERIOD)
    for sym in ["^GSPC", "^VIX"]:
        tech.fetch_ohlcv(sym, period=PREFETCH_PERIOD)
    if not ohlcv_data:
        print(f"[HOLDOUT2] {name}: ERROR no data fetched")
        return []

    trading_days = get_trading_days(ohlcv_data, start, end)
    if not trading_days:
        print(f"[HOLDOUT2] {name}: ERROR no trading days in range (check prefetch period / "
              f"symbol IPO dates -- some SHORT_UNIVERSE names may not exist this far back)")
        return []
    print(f"[HOLDOUT2] {name}: {len(trading_days)} trading days, {len(SYMBOLS)} symbols")

    regime_calendar = build_regime_calendar(trading_days, ohlcv_data)
    signals = _replay_window(
        ohlcv_data, regime_calendar, SYMBOLS, start, end, RISK_TIER, MIN_CONFIDENCE, 14,
    )
    shorts = [s for s in signals if s.get("direction") == "short"]
    print(f"[HOLDOUT2] {name}: {len(shorts)} short signals")
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
    print("BLIND HOLDOUT VALIDATION — shorts engine Phase 2 redesign (confirmation-bar entry)")
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

    out_path = "/tmp/shorts_blind_holdout_phase2_raw.json"
    with open(out_path, "w") as f:
        json.dump(per_episode, f, default=str)
    print(f"\n[HOLDOUT2] Raw trades dumped to {out_path}")


if __name__ == "__main__":
    main()
