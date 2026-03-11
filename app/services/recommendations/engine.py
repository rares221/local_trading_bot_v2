"""
Recommendation engine: fetch prices (yfinance) and compute buy_in, stop_loss, take_profit.
Produces up to 5 stocks, 5 commodities, 5 other (forex/crypto) per day.
"""
from dataclasses import dataclass
from typing import Optional

import yfinance as yf


# (asset_id, yfinance symbol, risk_tier) per category. Up to 5 each for stocks, commodities, other.
ASSETS_BY_CATEGORY = {
    "stocks": [
        ("XOM", "XOM", "conservative"),
        ("CVX", "CVX", "conservative"),
        ("BP", "BP", "conservative"),
        ("SHEL", "SHEL", "moderate"),   # Shell
        ("NEM", "NEM", "moderate"),     # Newmont (gold)
    ],
    "commodities": [
        ("WTI", "CL=F", "conservative"),
        ("Brent", "BZ=F", "conservative"),
        ("XAUUSD", "GC=F", "moderate"),
        ("Henry Hub", "NG=F", "moderate"),
        ("Silver", "SI=F", "moderate"),
    ],
    "other": [
        ("EURUSD", "EURUSD=X", "moderate"),
        ("GBPUSD", "GBPUSD=X", "moderate"),
        ("USDJPY", "USDJPY=X", "moderate"),
        ("BTC", "BTC-USD", "aggressive"),
        ("ETH", "ETH-USD", "aggressive"),
    ],
}

# Flat list (category, asset_id, symbol, risk_tier) for get_price_snapshot
def _all_assets() -> list[tuple[str, str, str, str]]:
    out: list[tuple[str, str, str, str]] = []
    for cat, items in ASSETS_BY_CATEGORY.items():
        for asset_id, symbol, tier in items:
            out.append((cat, asset_id, symbol, tier))
    return out

# SL/TP as fraction of price (long bias: SL below, TP above)
# conservative: tighter; moderate: wider
SL_TP_BY_RISK = {
    "conservative": (0.015, 0.03),   # 1.5% SL, 3% TP
    "moderate": (0.02, 0.04),         # 2% SL, 4% TP
    "aggressive": (0.03, 0.06),       # 3% SL, 6% TP
}


@dataclass
class Candidate:
    asset_category: str
    asset_id: str
    risk_tier: str
    buy_in_price: float
    stop_loss: float
    take_profit: float


def _fetch_price(symbol: str) -> Optional[float]:
    """Return latest close price for symbol, or None if fetch fails."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="5d")
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def _compute_levels(price: float, risk_tier: str) -> tuple[float, float]:
    """Return (stop_loss, take_profit) for a long bias. Price is buy_in."""
    sl_pct, tp_pct = SL_TP_BY_RISK.get(risk_tier, SL_TP_BY_RISK["moderate"])
    stop_loss = round(price * (1 - sl_pct), 2)
    take_profit = round(price * (1 + tp_pct), 2)
    return stop_loss, take_profit


def get_price_snapshot() -> dict[str, float]:
    """
    Return {asset_id: price} for all assets across categories that fetch successfully.
    Used by market analysis to include current prices in the report payload.
    """
    out: dict[str, float] = {}
    for _cat, asset_id, symbol, _tier in _all_assets():
        p = _fetch_price(symbol)
        if p is not None and p > 0:
            out[asset_id] = round(p, 2)
    return out


def build_daily_candidates_for_category(
    category: str,
    max_count: int = 5,
) -> list[Candidate]:
    """
    Fetch prices for one category (stocks, commodities, other), compute SL/TP,
    return up to max_count candidates. Skips assets whose price fetch fails.
    """
    items = ASSETS_BY_CATEGORY.get(category, [])
    candidates: list[Candidate] = []
    for asset_id, symbol, risk_tier in items:
        if len(candidates) >= max_count:
            break
        price = _fetch_price(symbol)
        if price is None or price <= 0:
            continue
        price = round(price, 2)
        stop_loss, take_profit = _compute_levels(price, risk_tier)
        candidates.append(Candidate(
            asset_category=category,
            asset_id=asset_id,
            risk_tier=risk_tier,
            buy_in_price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
        ))
    return candidates


def build_daily_candidates(max_count: int = 5) -> list[Candidate]:
    """
    Build up to max_count per category (stocks, commodities, other).
    Returns 5 stocks + 5 commodities + 5 other (up to 15 total).
    """
    out: list[Candidate] = []
    for category in ("stocks", "commodities", "other"):
        out.extend(build_daily_candidates_for_category(category, max_count=max_count))
    return out
