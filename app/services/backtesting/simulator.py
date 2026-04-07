"""
Trade outcome simulator for backtesting.

Given an entry (price, SL, TP, direction) and future OHLCV bars,
determines whether the trade hit take-profit, stop-loss, or expired.

Edge case: same bar hits both SL and TP (gap or wide-range day).
Heuristic: if direction=long and Low <= SL and High >= TP:
  - If Open < SL → gap down at open → hit_sl (worst case, realistic)
  - Else assume SL hit first (conservative assumption for equity)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd


@dataclass
class SimulatedOutcome:
    outcome: str           # hit_tp | hit_sl | expired
    closing_price: float
    outcome_date: date
    bars_held: int
    pnl_r: float          # P&L in R-multiples: +RR for hit_tp, -1.0 for hit_sl, 0+ for expired


def simulate_outcome(
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    direction: str,
    future_bars: pd.DataFrame,
    max_bars: int = 30,
) -> SimulatedOutcome:
    """
    Scan future_bars to determine outcome. Returns SimulatedOutcome.
    If no bars or SL/TP never hit within max_bars, returns 'expired'.
    """
    if future_bars is None or future_bars.empty:
        return SimulatedOutcome(
            outcome="expired",
            closing_price=entry_price,
            outcome_date=date.today(),
            bars_held=0,
            pnl_r=0.0,
        )

    risk = abs(entry_price - stop_loss)
    reward = abs(take_profit - entry_price)
    rr = reward / risk if risk > 0 else 0.0

    bars = future_bars.iloc[:max_bars]

    for i, (idx, row) in enumerate(bars.iterrows()):
        bar_date = idx.date() if hasattr(idx, "date") else idx
        bar_open = float(row.get("Open", entry_price))
        bar_high = float(row.get("High", entry_price))
        bar_low = float(row.get("Low", entry_price))
        bar_close = float(row.get("Close", entry_price))

        bars_held = i + 1

        if direction == "long":
            sl_hit = bar_low <= stop_loss
            tp_hit = bar_high >= take_profit

            if sl_hit and tp_hit:
                # Both hit same bar — conservative: check if open gapped below SL
                if bar_open <= stop_loss:
                    closing = min(bar_open, stop_loss)
                    pnl = _pnl_r(-1.0, entry_price, closing, stop_loss, risk)
                    return SimulatedOutcome("hit_sl", closing, bar_date, bars_held, pnl)
                # Assume SL hit first (intraday wick down before rally)
                return SimulatedOutcome("hit_sl", stop_loss, bar_date, bars_held, -1.0)
            elif sl_hit:
                # Gap slippage: if market opened past SL, exit at open (realistic fill)
                if bar_open <= stop_loss:
                    closing = bar_open
                else:
                    closing = stop_loss  # normal intraday stop trigger
                pnl = _pnl_r(-1.0, entry_price, closing, stop_loss, risk)
                return SimulatedOutcome("hit_sl", closing, bar_date, bars_held, pnl)
            elif tp_hit:
                return SimulatedOutcome("hit_tp", take_profit, bar_date, bars_held, rr)

        else:  # short
            sl_hit = bar_high >= stop_loss
            tp_hit = bar_low <= take_profit

            if sl_hit and tp_hit:
                if bar_open >= stop_loss:
                    closing = max(bar_open, stop_loss)
                    pnl = _pnl_r(-1.0, entry_price, closing, stop_loss, risk)
                    return SimulatedOutcome("hit_sl", closing, bar_date, bars_held, pnl)
                return SimulatedOutcome("hit_sl", stop_loss, bar_date, bars_held, -1.0)
            elif sl_hit:
                # Gap slippage: if market opened past SL, exit at open (realistic fill)
                if bar_open >= stop_loss:
                    closing = bar_open
                else:
                    closing = stop_loss  # normal intraday stop trigger
                pnl = _pnl_r(-1.0, entry_price, closing, stop_loss, risk)
                return SimulatedOutcome("hit_sl", closing, bar_date, bars_held, pnl)
            elif tp_hit:
                return SimulatedOutcome("hit_tp", take_profit, bar_date, bars_held, rr)

    # Expired — use closing price of last bar
    last_idx = bars.index[-1]
    last_date = last_idx.date() if hasattr(last_idx, "date") else last_idx
    last_close = float(bars["Close"].iloc[-1])
    bars_total = len(bars)

    if direction == "long":
        raw_pnl = (last_close - entry_price) / risk if risk > 0 else 0.0
    else:
        raw_pnl = (entry_price - last_close) / risk if risk > 0 else 0.0

    return SimulatedOutcome("expired", last_close, last_date, bars_total, round(raw_pnl, 4))


def _pnl_r(
    nominal_r: float,
    entry: float,
    actual_close: float,
    sl: float,
    risk: float,
) -> float:
    """
    More precise P&L in R-multiples when actual closing price differs from exact SL level
    (e.g. gap past SL). Negative for losses.
    """
    if risk <= 0:
        return nominal_r
    actual_r = (actual_close - entry) / risk
    return round(actual_r, 4)
