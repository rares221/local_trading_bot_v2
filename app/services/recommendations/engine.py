"""
Recommendation engine: builds trade ideas from the analysis we pulled (RSS, sentiment, tags),
then prices fixed universe + any dynamically detected ticker we can price. Best 5 per category by sentiment + risk.
"""
from dataclasses import dataclass
from typing import Optional

import yfinance as yf

from app.services.sentiment.ticker_detection import is_likely_ticker

# Risk tier order for filtering: only assets at or below user's preferred level are included.
RISK_TIER_ORDER = {"conservative": 0, "moderate": 1, "aggressive": 2}

# Tag (from headlines) -> (category, asset_id). Recommendation *ideas* come from tags that appear in analysis.
SENTIMENT_TAG_TO_ASSETS: dict[str, list[tuple[str, str]]] = {
    "oil": [("commodities", "WTI"), ("commodities", "Brent"), ("stocks", "XOM"), ("stocks", "CVX")],
    "gold": [("commodities", "XAUUSD"), ("stocks", "NEM")],
    "gas": [("commodities", "Henry Hub")],
    "XOM": [("stocks", "XOM")],
    "CVX": [("stocks", "CVX")],
    "BP": [("stocks", "BP")],
    "SHEL": [("stocks", "SHEL")],
    "NEM": [("stocks", "NEM")],
    "WTI": [("commodities", "WTI")],
    "Brent": [("commodities", "Brent")],
    "XAUUSD": [("commodities", "XAUUSD")],
    "Henry Hub": [("commodities", "Henry Hub")],
    "Silver": [("commodities", "Silver")],
    "BTC": [("other", "BTC")],
    "ETH": [("other", "ETH")],
    # Indices (headline tags -> indices category)
    "SPX": [("indices", "SPX")],
    "S&P 500": [("indices", "SPX")],
    "DJI": [("indices", "DJI")],
    "Dow": [("indices", "DJI")],
    "NDX": [("indices", "NDX")],
    "NASDAQ": [("indices", "NDX")],
    "FTSE": [("indices", "FTSE")],
    "VIX": [("indices", "VIX")],
}

# Tradeable universe: assets we can price (yfinance symbol) and their risk_tier. Ideas come from analysis; we only recommend from this set.
ASSETS_BY_CATEGORY = {
    "stocks": [
        ("XOM", "XOM", "conservative"),
        ("CVX", "CVX", "conservative"),
        ("BP", "BP", "conservative"),
        ("SHEL", "SHEL", "moderate"),
        ("NEM", "NEM", "moderate"),
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
    "indices": [
        ("SPX", "^GSPC", "moderate"),   # S&P 500
        ("DJI", "^DJI", "moderate"),    # Dow Jones
        ("NDX", "^IXIC", "moderate"),   # NASDAQ Composite
        ("FTSE", "^FTSE", "moderate"),  # FTSE 100
        ("VIX", "^VIX", "aggressive"),  # Volatility index
    ],
}

# (category, asset_id) -> (symbol, risk_tier) for fixed universe
def _universe_lookup() -> dict[tuple[str, str], tuple[str, str]]:
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for cat, items in ASSETS_BY_CATEGORY.items():
        for asset_id, symbol, tier in items:
            out[(cat, asset_id)] = (symbol, tier)
    return out


def _resolve_symbol_risk(
    category: str,
    asset_id: str,
    universe: dict[tuple[str, str], tuple[str, str]],
) -> Optional[tuple[str, str]]:
    """(symbol, risk_tier) for pricing. Fixed universe first; else dynamic ticker -> (asset_id, moderate)."""
    key = (category, asset_id)
    if key in universe:
        return universe[key]
    if category == "stocks" and is_likely_ticker(asset_id):
        return (asset_id, "moderate")
    return None


def _trade_ideas_from_analysis(by_asset_sentiment: Optional[dict]) -> set[tuple[str, str]]:
    """(category, asset_id) pairs from analysis: fixed tag mapping + dynamic tickers (stocks)."""
    if not by_asset_sentiment:
        return set()
    ideas: set[tuple[str, str]] = set()
    for tag in by_asset_sentiment:
        if tag in SENTIMENT_TAG_TO_ASSETS:
            for pair in SENTIMENT_TAG_TO_ASSETS[tag]:
                ideas.add(pair)
        elif is_likely_ticker(tag):
            ideas.add(("stocks", tag))
    return ideas


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
    """Return latest close price for symbol, or None if fetch fails. Suppresses yfinance log noise."""
    import logging
    try:
        # Reduce yfinance "possibly delisted" / "no data" log spam when we probe dynamic tickers
        log = logging.getLogger("yfinance")
        old_level = log.level
        log.setLevel(logging.WARNING)
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="5d")
            if hist is None or hist.empty or "Close" not in hist.columns:
                return None
            return float(hist["Close"].iloc[-1])
        finally:
            log.setLevel(old_level)
    except Exception:
        return None


def _compute_levels(price: float, risk_tier: str) -> tuple[float, float]:
    """Return (stop_loss, take_profit) for a long bias. Price is buy_in."""
    sl_pct, tp_pct = SL_TP_BY_RISK.get(risk_tier, SL_TP_BY_RISK["moderate"])
    stop_loss = round(price * (1 - sl_pct), 2)
    take_profit = round(price * (1 + tp_pct), 2)
    return stop_loss, take_profit


def compute_sl_tp_for_price(price: float, risk_tier: str) -> tuple[float, float]:
    """
    Return (stop_loss, take_profit) for a given current price and risk tier.
    Used by active-trade update job to refresh SL/TP from latest price.
    """
    return _compute_levels(price, risk_tier)


def get_yf_symbol(category: str, asset_id: str) -> Optional[str]:
    """
    Return yfinance symbol for (category, asset_id), or None if not resolvable.
    Used by research context for fundamentals (e.g. sector, marketCap) and by briefing.
    """
    universe = _universe_lookup()
    resolved = _resolve_symbol_risk(category, asset_id, universe)
    if not resolved:
        return None
    return resolved[0]


def get_asset_price_and_risk(category: str, asset_id: str) -> Optional[tuple[float, str]]:
    """
    Return (current_price, risk_tier) for (category, asset_id) if we can resolve and fetch price.
    Used by research context builder.
    """
    universe = _universe_lookup()
    resolved = _resolve_symbol_risk(category, asset_id, universe)
    if not resolved:
        return None
    symbol, risk_tier = resolved
    price = _fetch_price(symbol)
    if price is None or price <= 0:
        return None
    return (round(price, 2), risk_tier)


def get_sentiment_for_asset(
    category: str,
    asset_id: str,
    by_asset_sentiment: dict[str, dict],
) -> Optional[float]:
    """
    Return best sentiment score for this (category, asset_id) from by_asset_sentiment.
    by_asset_sentiment: tag -> {"avg": float, "count": int}. Returns None if no tag maps.
    """
    pair = get_sentiment_score_and_count(category, asset_id, by_asset_sentiment)
    return pair[0] if pair else None


def get_sentiment_score_and_count(
    category: str,
    asset_id: str,
    by_asset_sentiment: dict[str, dict],
) -> Optional[tuple[float, int]]:
    """
    Return (best_avg_sentiment, total_mention_count) for this asset. Includes fixed tag mapping
    and direct tag match for dynamic tickers (by_asset_sentiment[asset_id]).
    """
    if not by_asset_sentiment:
        return None
    best_avg: Optional[float] = None
    total_count = 0
    for tag, targets in SENTIMENT_TAG_TO_ASSETS.items():
        if (category, asset_id) in targets:
            info = by_asset_sentiment.get(tag)
            if info:
                avg = info.get("avg")
                cnt = info.get("count", 0)
                if avg is not None:
                    if best_avg is None or avg > best_avg:
                        best_avg = avg
                    total_count += cnt
    # Dynamic ticker: tag is the asset_id itself (e.g. TSLA in headlines)
    if category == "stocks" and asset_id in by_asset_sentiment:
        info = by_asset_sentiment[asset_id]
        avg = info.get("avg")
        cnt = info.get("count", 0)
        if avg is not None:
            if best_avg is None or avg > best_avg:
                best_avg = avg
            total_count += cnt
    return (best_avg, total_count) if best_avg is not None else None


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


def _candidates_for_pairs(
    category: str,
    pairs: list[tuple[str, str]],
    universe: dict[tuple[str, str], tuple[str, str]],
    max_risk_level: int,
    exclude_taken: Optional[set[tuple[str, str]]] = None,
) -> list[Candidate]:
    """Build candidates for (category, asset_id) pairs; skip pairs in exclude_taken."""
    out: list[Candidate] = []
    seen: set[str] = set()
    excluded = exclude_taken or set()
    for (c, asset_id) in pairs:
        if c != category or asset_id in seen or (category, asset_id) in excluded:
            continue
        seen.add(asset_id)
        resolved = _resolve_symbol_risk(category, asset_id, universe)
        if not resolved:
            continue
        symbol, risk_tier = resolved
        if RISK_TIER_ORDER.get(risk_tier, 99) > max_risk_level:
            continue
        price = _fetch_price(symbol)
        if price is None or price <= 0:
            continue
        price = round(price, 2)
        stop_loss, take_profit = _compute_levels(price, risk_tier)
        out.append(Candidate(
            asset_category=category,
            asset_id=asset_id,
            risk_tier=risk_tier,
            buy_in_price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
        ))
    return out


def build_daily_candidates_for_category(
    category: str,
    max_count: int = 5,
    by_asset_sentiment: Optional[dict] = None,
    preferred_risk: str = "moderate",
    tie_seed: Optional[str] = None,
    exclude_taken: Optional[set[tuple[str, str]]] = None,
) -> list[Candidate]:
    """
    Best trade ideas from analysis first (tags we pulled), then backfill from universe if needed.
    Only assets at or below preferred_risk. Skip (category, asset_id) in exclude_taken (already taken).
    """
    max_risk_level = RISK_TIER_ORDER.get(preferred_risk, RISK_TIER_ORDER["moderate"])
    universe = _universe_lookup()
    excluded = exclude_taken or set()
    idea_pairs = sorted(_trade_ideas_from_analysis(by_asset_sentiment))  # stable order
    idea_candidates = _candidates_for_pairs(category, idea_pairs, universe, max_risk_level, exclude_taken=excluded)
    def _rank_key(c: Candidate) -> tuple:
        pair = get_sentiment_score_and_count(c.asset_category, c.asset_id, by_asset_sentiment or {})
        if pair is None:
            avg, cnt = None, 0
        else:
            avg, cnt = pair
        tie = hash((tie_seed or "", c.asset_id)) % 10000
        return (avg is None, -(avg or -1), -cnt, tie)
    idea_candidates.sort(key=_rank_key)
    chosen_ids = {c.asset_id for c in idea_candidates[:max_count]}
    result = idea_candidates[:max_count]
    # Backfill from full category universe if we have fewer than max_count
    if len(result) < max_count:
        items = ASSETS_BY_CATEGORY.get(category, [])
        rest: list[Candidate] = []
        for asset_id, symbol, risk_tier in items:
            if asset_id in chosen_ids or (category, asset_id) in excluded or RISK_TIER_ORDER.get(risk_tier, 99) > max_risk_level:
                continue
            price = _fetch_price(symbol)
            if price is None or price <= 0:
                continue
            price = round(price, 2)
            stop_loss, take_profit = _compute_levels(price, risk_tier)
            rest.append(Candidate(
                asset_category=category,
                asset_id=asset_id,
                risk_tier=risk_tier,
                buy_in_price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
            ))
        rest.sort(key=lambda c: (hash((tie_seed or "", c.asset_id)) % 10000, c.asset_id))
        for c in rest:
            if len(result) >= max_count:
                break
            result.append(c)
    return result


def build_daily_candidates(
    max_count: int = 5,
    by_asset_sentiment: Optional[dict] = None,
    preferred_risk: str = "moderate",
    tie_seed: Optional[str] = None,
    exclude_taken: Optional[set[tuple[str, str]]] = None,
) -> list[Candidate]:
    """
    Build up to max_count per category (stocks, commodities, other, indices).
    Skip (category, asset_id) in exclude_taken (e.g. already have a taken recommendation).
    """
    out: list[Candidate] = []
    for category in ("stocks", "commodities", "other", "indices"):
        out.extend(build_daily_candidates_for_category(
            category,
            max_count=max_count,
            by_asset_sentiment=by_asset_sentiment,
            preferred_risk=preferred_risk,
            tie_seed=tie_seed,
            exclude_taken=exclude_taken,
        ))
    return out
