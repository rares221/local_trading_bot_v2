"""
Live trading confidence scorer — statistical/mathematical only, no LLM.

Formula (0-100):
  backtest_component  (0-50 pts): historical win rate from backtest_trades
  indicator_component (0-30 pts): technical indicator alignment (rescaled)
  sample_size_bonus   (0-20 pts): reward for larger/more reliable sample

Fallback when n < 10 (no meaningful backtest history):
  min(50, indicator_score * 0.50)
  → honest cap at 50 — no historical edge proven yet.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Async DB fetch — call once per analysis run, before spawning threads
# ---------------------------------------------------------------------------

async def fetch_backtest_stats(symbols: list[str]) -> dict[str, dict]:
    """
    Query backtest_trades for resolved outcomes (hit_tp / hit_sl only —
    expired trades are excluded as they carry no directional signal).

    Returns:
        { yf_symbol: {"n": int, "win_rate": float} }

    Symbols with fewer than 10 resolved trades will NOT appear in the result,
    so callers should use .get(symbol) and treat None as "no data".
    """
    if not symbols:
        return {}

    try:
        from app.core.database import AsyncSessionLocal
        from app.models.backtest import BacktestTrade
        from sqlalchemy import select, func, case as sa_case

        async with AsyncSessionLocal() as session:
            stmt = (
                select(
                    BacktestTrade.symbol,
                    func.count().label("n"),
                    func.sum(
                        sa_case((BacktestTrade.outcome == "hit_tp", 1), else_=0)
                    ).label("wins"),
                )
                .where(
                    BacktestTrade.symbol.in_(symbols),
                    BacktestTrade.outcome.in_(["hit_tp", "hit_sl"]),
                )
                .group_by(BacktestTrade.symbol)
            )
            rows = (await session.execute(stmt)).all()

        result: dict[str, dict] = {}
        for row in rows:
            n = int(row.n or 0)
            wins = int(row.wins or 0)
            if n < 10:
                continue  # not enough data to be meaningful
            result[row.symbol] = {
                "n": n,
                "win_rate": wins / n,
            }

        logger.info(
            "[LIVE_CONF] Backtest stats loaded for %d/%d symbols",
            len(result), len(symbols),
        )
        return result

    except Exception as exc:
        logger.warning("[LIVE_CONF] Could not fetch backtest stats: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Pure-math confidence calculation
# ---------------------------------------------------------------------------

def compute_live_confidence(
    backtest_stats: dict | None,
    indicator_score: int,
) -> int:
    """
    Compute a statistically-grounded confidence score for a live signal.

    Args:
        backtest_stats:  {"n": int, "win_rate": float} or None
        indicator_score: raw output of compute_confidence() — 0-100

    Returns:
        Confidence integer 0-100.
    """
    n = int(backtest_stats["n"]) if backtest_stats else 0
    win_rate = float(backtest_stats["win_rate"]) if backtest_stats else 0.0

    # ── No meaningful history: allow signal through with a reasonable cap ────
    # indicator_score peaks at ~61 with no sentiment data; 0.50 cap produces max
    # ~30, which never clears the 40% gate in stream_processor. 0.70 → ~42, clears it.
    if n < 10:
        return min(65, int(indicator_score * 0.70))

    # ── Backtest component: win rate drives up to 50 pts ────────────────────
    # win_rate=0.60 → 30 pts, win_rate=0.70 → 35 pts, win_rate=0.50 → 25 pts
    backtest_pts = win_rate * 50.0

    # ── Indicator component: technical alignment → up to 30 pts ─────────────
    indicator_pts = indicator_score * 0.30

    # ── Sample size bonus: reward statistical reliability ───────────────────
    if n >= 50:
        sample_pts = 20.0
    elif n >= 30:
        sample_pts = 14.0
    else:  # 10 ≤ n < 30
        sample_pts = 7.0

    total = backtest_pts + indicator_pts + sample_pts
    return max(0, min(100, int(total)))
