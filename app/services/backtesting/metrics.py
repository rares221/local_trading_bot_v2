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


def compute_metrics(trades: list[dict], total_trading_days: int = 0) -> dict[str, Any]:
    """
    Compute all backtesting metrics from a list of trade dicts.

    Each dict must have:
        outcome: str          # hit_tp | hit_sl | expired
        pnl_r: float          # P&L in R-multiples
        confidence_score: int
        risk_reward: float | None

    Args:
        total_trading_days: Number of trading days in the backtest window. Used to
            compute correct per-trade annualized Sharpe: (mean/std) * sqrt(N/T_years).
            If 0, falls back to raw per-trade Sharpe (mean/std) without annualization.

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

    # Resolved win rate: excludes expired trades — pure signal quality on trades that fully closed.
    # If this is significantly higher than win_rate, expiry drag is masking true signal accuracy.
    resolved_total = len(wins) + len(losses)
    resolved_win_rate = len(wins) / resolved_total if resolved_total > 0 else 0.0

    # Expired P&L breakdown: positive avg = timing issue (needed more bars); negative = weak signals
    expired_pnl = [t["pnl_r"] for t in expired if t.get("pnl_r") is not None and not math.isnan(t["pnl_r"])]
    expired_positive = sum(1 for p in expired_pnl if p > 0)
    avg_pnl_r_expired = sum(expired_pnl) / len(expired_pnl) if expired_pnl else 0.0

    # Average R:R — exclude None and NaN (NaN from missing OHLCV data poisons sharpe)
    pnl_rs = [t["pnl_r"] for t in all_outcomes if t.get("pnl_r") is not None and not math.isnan(t["pnl_r"])]
    avg_pnl_r = sum(pnl_rs) / len(pnl_rs) if pnl_rs else 0.0
    avg_rr_projected = _safe_avg([t.get("risk_reward") for t in closed])

    # Sharpe ratio: per-trade annualized Sharpe.
    # Correct formula: (mean/std) * sqrt(trades_per_year)
    # where trades_per_year = N / T_years, T_years = total_trading_days / 252.
    # Falls back to raw (mean/std) if total_trading_days not provided.
    n_trades = len(pnl_rs)
    if total_trading_days > 0 and n_trades > 1:
        t_years = total_trading_days / 252.0
        trades_per_year = n_trades / t_years
        sharpe = _sharpe(pnl_rs, periods_per_year=trades_per_year)
    else:
        sharpe = _sharpe(pnl_rs, periods_per_year=1)  # raw, no annualization

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

    # Relative Strength analytics: win rate for RS strong / neutral / weak signals
    rs_strong  = [t for t in all_outcomes if t.get("rs_20d") is not None and t["rs_20d"] > 1.2]
    rs_weak    = [t for t in all_outcomes if t.get("rs_20d") is not None and t["rs_20d"] < 0.8]
    rs_neutral = [t for t in all_outcomes if t.get("rs_20d") is not None and 0.8 <= t["rs_20d"] <= 1.2]
    rs_none    = [t for t in all_outcomes if t.get("rs_20d") is None]
    rs_metrics = {
        "strong": _vp_stats(rs_strong),
        "neutral": _vp_stats(rs_neutral),
        "weak": _vp_stats(rs_weak),
        "no_data": _vp_stats(rs_none),
        "pct_strong": round(len(rs_strong) / total, 4) if total > 0 else 0.0,
        "pct_weak": round(len(rs_weak) / total, 4) if total > 0 else 0.0,
    }

    # Market regime breakdown — win rate per regime (bull/bear/crisis/high_vol/neutral)
    regime_stats: dict[str, dict] = {}
    for t in all_outcomes:
        regime = t.get("market_regime") or "unknown"
        if regime not in regime_stats:
            regime_stats[regime] = {"total": 0, "wins": 0, "pnl_r_sum": 0.0}
        regime_stats[regime]["total"] += 1
        if t.get("outcome") == "hit_tp":
            regime_stats[regime]["wins"] += 1
        regime_stats[regime]["pnl_r_sum"] += t.get("pnl_r") or 0.0
    for regime, rs in regime_stats.items():
        n = rs["total"]
        w = rs["wins"]
        rs["win_rate"] = round(w / n, 4) if n > 0 else 0.0
        rs["avg_pnl_r"] = round(rs["pnl_r_sum"] / n, 4) if n > 0 else 0.0

    return {
        "total_signals": len(trades),
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "expired": len(expired),
        "win_rate": round(win_rate, 4),
        "resolved_win_rate": round(resolved_win_rate, 4),  # TP/(TP+SL) — excludes expired drag
        "expired_positive": expired_positive,               # expired trades that were in profit at expiry
        "avg_pnl_r_expired": round(avg_pnl_r_expired, 4),  # positive = timing issue; negative = weak signals
        "avg_pnl_r": round(avg_pnl_r, 4),
        "avg_rr_projected": round(avg_rr_projected, 4) if avg_rr_projected else None,
        "sharpe_ratio": round(sharpe, 4) if sharpe is not None else None,
        "max_drawdown_r": round(max_dd, 4),
        "total_r": round(sum(pnl_rs), 4),
        "by_confidence_bucket": bucket_stats,
        "by_direction": direction_stats,
        "by_symbol": symbol_stats,
        "volume_profile": vp_metrics,
        "relative_strength": rs_metrics,
        "by_regime": regime_stats,
    }


def _safe_avg(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _sharpe(returns: list[float], periods_per_year: float = 252) -> float | None:
    """
    Annualized Sharpe ratio.
    Pass periods_per_year = trades_per_year for per-trade backtest returns.
    Pass periods_per_year = 252 only for daily returns.
    Pass periods_per_year = 1 for raw (non-annualized) Sharpe.
    """
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
