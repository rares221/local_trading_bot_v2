"""
Backtesting metrics: Sharpe ratio, max drawdown, win rate per confidence bucket,
average R:R realized vs projected, and benchmark comparison.
"""
from __future__ import annotations

import math
from typing import Any


def _bucket(confidence: int) -> str:
    """Map confidence score to a label bucket."""
    if confidence < 40:
        return "<40"
    elif confidence < 50:
        return "40-49"
    elif confidence < 60:
        return "50-59"
    elif confidence < 70:
        return "60-69"
    elif confidence < 80:
        return "70-79"
    else:
        return "80+"


def compute_metrics(trades: list[dict]) -> dict[str, Any]:
    """
    Compute all backtesting metrics from a list of trade dicts.

    Each dict must have:
        outcome: str          # hit_tp | hit_sl | expired
        pnl_r: float          # P&L in R-multiples
        confidence_score: int
        risk_reward: float | None

    Returns a metrics dict suitable for JSON storage.
    """
    if not trades:
        return {"error": "no_trades"}

    closed = [t for t in trades if t.get("outcome") in ("hit_tp", "hit_sl")]
    all_outcomes = [t for t in trades if t.get("outcome")]

    total = len(all_outcomes)
    if total == 0:
        return {"error": "no_outcomes"}

    wins = [t for t in all_outcomes if t.get("outcome") == "hit_tp"]
    losses = [t for t in all_outcomes if t.get("outcome") == "hit_sl"]
    expired = [t for t in all_outcomes if t.get("outcome") == "expired"]

    win_rate = len(wins) / total if total > 0 else 0.0

    # Average R:R
    pnl_rs = [t["pnl_r"] for t in all_outcomes if t.get("pnl_r") is not None]
    avg_pnl_r = sum(pnl_rs) / len(pnl_rs) if pnl_rs else 0.0
    avg_rr_projected = _safe_avg([t.get("risk_reward") for t in closed])

    # Sharpe ratio: use per-trade R returns, annualise assuming 252 trading days
    sharpe = _sharpe(pnl_rs)

    # Max drawdown on cumulative R curve
    max_dd = _max_drawdown(pnl_rs)

    # Per confidence-bucket win rates
    buckets: dict[str, dict[str, Any]] = {}
    for t in all_outcomes:
        b = _bucket(t.get("confidence_score", 0))
        if b not in buckets:
            buckets[b] = {"total": 0, "wins": 0, "pnl_r_sum": 0.0}
        buckets[b]["total"] += 1
        if t.get("outcome") == "hit_tp":
            buckets[b]["wins"] += 1
        buckets[b]["pnl_r_sum"] += t.get("pnl_r") or 0.0

    bucket_stats = {}
    for b, bdata in sorted(buckets.items()):
        n = bdata["total"]
        w = bdata["wins"]
        bucket_stats[b] = {
            "total": n,
            "wins": w,
            "win_rate": round(w / n, 4) if n > 0 else 0.0,
            "avg_pnl_r": round(bdata["pnl_r_sum"] / n, 4) if n > 0 else 0.0,
        }

    # Per direction
    direction_stats: dict[str, dict] = {}
    for t in all_outcomes:
        d = t.get("direction", "long")
        if d not in direction_stats:
            direction_stats[d] = {"total": 0, "wins": 0}
        direction_stats[d]["total"] += 1
        if t.get("outcome") == "hit_tp":
            direction_stats[d]["wins"] += 1
    for d, ds in direction_stats.items():
        n = ds["total"]
        ds["win_rate"] = round(ds["wins"] / n, 4) if n > 0 else 0.0

    # Per symbol win rates (top losers/winners)
    symbol_stats: dict[str, dict] = {}
    for t in all_outcomes:
        sym = t.get("symbol", "?")
        if sym not in symbol_stats:
            symbol_stats[sym] = {"total": 0, "wins": 0, "pnl_r_sum": 0.0}
        symbol_stats[sym]["total"] += 1
        if t.get("outcome") == "hit_tp":
            symbol_stats[sym]["wins"] += 1
        symbol_stats[sym]["pnl_r_sum"] += t.get("pnl_r") or 0.0
    for sym, ss in symbol_stats.items():
        n = ss["total"]
        ss["win_rate"] = round(ss["wins"] / n, 4) if n > 0 else 0.0
        ss["avg_pnl_r"] = round(ss["pnl_r_sum"] / n, 4) if n > 0 else 0.0

    # Volume Profile analytics: compare performance inside vs outside value area
    va_in = [t for t in all_outcomes if t.get("in_value_area")]
    va_out = [t for t in all_outcomes if not t.get("in_value_area") and t.get("vpoc") is not None]
    def _vp_stats(subset: list[dict]) -> dict:
        if not subset:
            return {}
        w = sum(1 for t in subset if t.get("outcome") == "hit_tp")
        pnls = [t["pnl_r"] for t in subset if t.get("pnl_r") is not None]
        return {
            "total": len(subset),
            "win_rate": round(w / len(subset), 4),
            "avg_pnl_r": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
        }

    vp_metrics = {
        "in_value_area": _vp_stats(va_in),
        "outside_value_area": _vp_stats(va_out),
        "pct_signals_in_va": round(len(va_in) / total, 4) if total > 0 else 0.0,
    }

    return {
        "total_signals": len(trades),
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "expired": len(expired),
        "win_rate": round(win_rate, 4),
        "avg_pnl_r": round(avg_pnl_r, 4),
        "avg_rr_projected": round(avg_rr_projected, 4) if avg_rr_projected else None,
        "sharpe_ratio": round(sharpe, 4) if sharpe is not None else None,
        "max_drawdown_r": round(max_dd, 4),
        "total_r": round(sum(pnl_rs), 4),
        "by_confidence_bucket": bucket_stats,
        "by_direction": direction_stats,
        "by_symbol": symbol_stats,
        "volume_profile": vp_metrics,
    }


def _safe_avg(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _sharpe(returns: list[float], periods_per_year: int = 252) -> float | None:
    """Annualised Sharpe assuming each trade is ~1 period. Simplified but standard."""
    if len(returns) < 2:
        return None
    n = len(returns)
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(variance)
    if std == 0:
        return None
    return mean / std * math.sqrt(periods_per_year)


def _max_drawdown(returns: list[float]) -> float:
    """Max drawdown from peak on cumulative R curve."""
    if not returns:
        return 0.0
    peak = 0.0
    cumulative = 0.0
    max_dd = 0.0
    for r in returns:
        cumulative += r
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    return max_dd
