"""
Shorts engine Tier-1 diagnosis (systematic-debugging Phase 1: root-cause evidence gathering).

Replicates run_walkforward_backtest's exact IS/OOS windowing over the curated SHORT_UNIVERSE,
but captures RAW per-trade signal dicts (not just compute_metrics aggregates) so we can break the
OOS short losses down by regime / family / trigger / year / confidence bucket — the breakdown the
stored WalkForwardRun rows cannot provide (they only persist window-level aggregates).

Run inside the container:
    docker-compose exec -T app python -m scripts.diagnose_shorts_tier1
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

import app.services.recommendations.engine as engine_mod
from app.services import technicals as tech
from app.services.backtesting.data_provider import prefetch_symbols, get_trading_days
from app.services.backtesting.regime import build_regime_calendar
from app.services.backtesting.replay import _replay_window
from app.services.recommendations.shorts_engine_io import SHORT_UNIVERSE
from app.services.recommendations.shorts_signals import _BREAKDOWN_TRIGGERS, _FADE_TOP_TRIGGERS

START = date(2021, 11, 1)
END = date(2024, 12, 31)
IN_SAMPLE_DAYS = 252
OUT_SAMPLE_DAYS = 63
RISK_TIER = "moderate"
MIN_CONFIDENCE = 40

SYMBOLS = sorted({yf for (_c, _a, yf, _r) in SHORT_UNIVERSE} | {"^GSPC", "^VIX"})


def family_of(trigger: str) -> str:
    if trigger in _BREAKDOWN_TRIGGERS:
        return "breakdown"
    if trigger in _FADE_TOP_TRIGGERS:
        return "fade_top"
    return "unknown"


def conf_bucket(conf) -> str:
    if conf is None:
        return "n/a"
    if conf < 65:
        return "60-64"
    if conf < 75:
        return "65-74"
    if conf < 85:
        return "75-84"
    return "85-100"


def summarize(trades: list[dict], label: str) -> dict:
    total = len(trades)
    resolved = [t for t in trades if t.get("outcome") in ("hit_tp", "hit_sl")]
    wins = [t for t in trades if t.get("outcome") == "hit_tp"]
    losses = [t for t in trades if t.get("outcome") == "hit_sl"]
    expired = [t for t in trades if t.get("outcome") == "expired"]
    pnl = [t.get("pnl_r") for t in trades if t.get("pnl_r") is not None]
    avg_pnl = round(sum(pnl) / len(pnl), 4) if pnl else None
    resolved_wr = round(len(wins) / len(resolved), 4) if resolved else None
    raw_wr = round(len(wins) / total, 4) if total else None
    return {
        "label": label,
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "expired": len(expired),
        "raw_win_rate": raw_wr,
        "resolved_win_rate": resolved_wr,
        "avg_pnl_r": avg_pnl,
        "hit_tp_rate": round(len(wins) / total, 4) if total else None,
        "hit_sl_rate": round(len(losses) / total, 4) if total else None,
        "expired_rate": round(len(expired) / total, 4) if total else None,
    }


def breakdown_by(trades: list[dict], key_fn, label_prefix: str) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups[key_fn(t)].append(t)
    return [summarize(v, f"{label_prefix}={k}") for k, v in sorted(groups.items())]


def main() -> None:
    # Force the shorts engine on for this diagnostic run only (module attribute is re-read at
    # call time inside _replay_window — see replay.py's runtime import of SHORTS_ENGINE_ENABLED).
    engine_mod.SHORTS_ENGINE_ENABLED = True

    print(f"[DIAG] Prefetching {len(SYMBOLS)} symbols {START}..{END} (period=5y)...")
    tech.clear_cache()
    ohlcv_data = prefetch_symbols(SYMBOLS, period="5y")
    for sym in ["^GSPC", "^VIX"]:
        tech.fetch_ohlcv(sym, period="5y")
    if not ohlcv_data:
        print("[DIAG] ERROR: no data fetched")
        return

    all_trading_days = get_trading_days(ohlcv_data, START, END)
    if not all_trading_days:
        print("[DIAG] ERROR: no trading days in range")
        return
    print(f"[DIAG] {len(all_trading_days)} trading days")

    windows = []
    i = 0
    while i + IN_SAMPLE_DAYS + OUT_SAMPLE_DAYS <= len(all_trading_days):
        is_days = all_trading_days[i : i + IN_SAMPLE_DAYS]
        oos_days = all_trading_days[i + IN_SAMPLE_DAYS : i + IN_SAMPLE_DAYS + OUT_SAMPLE_DAYS]
        windows.append((is_days[0], is_days[-1], oos_days[0], oos_days[-1]))
        i += OUT_SAMPLE_DAYS
    print(f"[DIAG] {len(windows)} walk-forward windows")

    regime_calendar = build_regime_calendar(all_trading_days, ohlcv_data)

    all_is_short: list[dict] = []
    all_oos_short: list[dict] = []

    for w_idx, (is_start, is_end, oos_start, oos_end) in enumerate(windows):
        print(f"[DIAG] Window {w_idx + 1}/{len(windows)}: IS {is_start}..{is_end}  OOS {oos_start}..{oos_end}")
        is_signals = _replay_window(
            ohlcv_data, regime_calendar, SYMBOLS, is_start, is_end, RISK_TIER, MIN_CONFIDENCE, 14,
        )
        oos_signals = _replay_window(
            ohlcv_data, regime_calendar, SYMBOLS, oos_start, oos_end, RISK_TIER, MIN_CONFIDENCE, 14,
        )
        is_short = [s for s in is_signals if s.get("direction") == "short"]
        oos_short = [s for s in oos_signals if s.get("direction") == "short"]
        all_is_short.extend(is_short)
        all_oos_short.extend(oos_short)
        print(f"         IS shorts={len(is_short)}  OOS shorts={len(oos_short)}")

    print("\n" + "=" * 90)
    print("TIER-1 DIAGNOSIS — shorts engine, curated universe, 2021-11..2024-12")
    print("=" * 90)

    print("\n--- Overall IS vs OOS ---")
    print(json.dumps(summarize(all_is_short, "ALL_IS"), indent=2))
    print(json.dumps(summarize(all_oos_short, "ALL_OOS"), indent=2))

    is_per_day = len(all_is_short) / max(1, len(windows) * IN_SAMPLE_DAYS)
    oos_per_day = len(all_oos_short) / max(1, len(windows) * OUT_SAMPLE_DAYS)
    print(f"\n--- Activation frequency (signals per trading day) ---")
    print(f"IS:  {is_per_day:.4f}  OOS: {oos_per_day:.4f}  ratio(OOS/IS): {oos_per_day / is_per_day if is_per_day else None}")

    print("\n--- OOS by family ---")
    for row in breakdown_by(all_oos_short, lambda t: family_of(t.get("trigger", "")), "family"):
        print(json.dumps(row))

    print("\n--- OOS by trigger ---")
    for row in breakdown_by(all_oos_short, lambda t: t.get("trigger", "none"), "trigger"):
        print(json.dumps(row))

    print("\n--- OOS by regime ---")
    for row in breakdown_by(all_oos_short, lambda t: t.get("market_regime", "none"), "regime"):
        print(json.dumps(row))

    print("\n--- OOS by year (entry_date) ---")
    for row in breakdown_by(all_oos_short, lambda t: t.get("entry_date", "")[:4], "year"):
        print(json.dumps(row))

    print("\n--- OOS by confidence bucket ---")
    for row in breakdown_by(all_oos_short, lambda t: conf_bucket(t.get("confidence_score")), "conf"):
        print(json.dumps(row))

    print("\n--- OOS by family x year ---")
    for row in breakdown_by(all_oos_short, lambda t: f"{family_of(t.get('trigger',''))}|{t.get('entry_date','')[:4]}", "family|year"):
        print(json.dumps(row))

    print("\n--- OOS by symbol (top 10 by trade count) ---")
    by_sym = breakdown_by(all_oos_short, lambda t: t.get("symbol", "?"), "symbol")
    by_sym.sort(key=lambda r: -r["total"])
    for row in by_sym[:10]:
        print(json.dumps(row))

    print("\n--- IS by family (for comparison) ---")
    for row in breakdown_by(all_is_short, lambda t: family_of(t.get("trigger", "")), "family"):
        print(json.dumps(row))

    # Dump raw trades for further ad-hoc analysis if needed.
    out_path = "/tmp/shorts_tier1_raw.json"
    with open(out_path, "w") as f:
        json.dump({"is_short": all_is_short, "oos_short": all_oos_short}, f, default=str)
    print(f"\n[DIAG] Raw trades dumped to {out_path}")


if __name__ == "__main__":
    main()
