"""
Recommendation engine v3: ATR + S/R SL/TP, long/short direction, expiry estimation,
technical signal scoring, market regime awareness, multi-TF trend alignment,
confidence gating, cross-asset correlation. Best N per category (regime-dependent).
"""
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

from app.services.sentiment.ticker_detection import is_likely_ticker
from app.services.technicals import (
    TechnicalSnapshot,
    build_snapshot,
    calc_risk_reward,
    clear_cache,
    compute_atr_levels,
    compute_confidence,
    compute_technical_score,
    determine_direction,
    estimate_expiry_days,
    get_cached_price,
)
from app.services.market_context import (
    MarketContext,
    build_market_context,
    check_trend_alignment,
    TrendAlignment,
)

RISK_TIER_ORDER = {"conservative": 0, "moderate": 1, "aggressive": 2}

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
    "SPX": [("indices", "SPX")],
    "S&P 500": [("indices", "SPX")],
    "DJI": [("indices", "DJI")],
    "Dow": [("indices", "DJI")],
    "NDX": [("indices", "NDX")],
    "NASDAQ": [("indices", "NDX")],
    "FTSE": [("indices", "FTSE")],
    "DAX": [("indices", "DAX")],
    "CAC": [("indices", "CAC")],
    "STOXX": [("indices", "STOXX")],
    "Euro Stoxx": [("indices", "STOXX")],
    "Nikkei": [("indices", "Nikkei")],
    "HangSeng": [("indices", "HangSeng")],
    "Hang Seng": [("indices", "HangSeng")],
    "Shanghai": [("indices", "Shanghai")],
    "Kospi": [("indices", "Kospi")],
    "Sensex": [("indices", "Sensex")],
    "ASX": [("indices", "ASX")],
    "RUT": [("indices", "RUT")],
    "Russell": [("indices", "RUT")],
    "VIX": [("indices", "VIX")],
}

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
        # US
        ("SPX", "^GSPC", "moderate"),
        ("DJI", "^DJI", "moderate"),
        ("NDX", "^IXIC", "moderate"),
        ("RUT", "^RUT", "moderate"),
        # Europe
        ("FTSE", "^FTSE", "moderate"),
        ("DAX", "^GDAXI", "moderate"),
        ("CAC", "^FCHI", "moderate"),
        ("STOXX", "^STOXX50E", "moderate"),
        # Asia-Pacific
        ("Nikkei", "^N225", "moderate"),
        ("HangSeng", "^HSI", "moderate"),
        ("Shanghai", "000001.SS", "aggressive"),
        ("Kospi", "^KS11", "moderate"),
        ("Sensex", "^BSESN", "moderate"),
        ("ASX", "^AXJO", "moderate"),
        # Volatility
        ("VIX", "^VIX", "aggressive"),
    ],
}


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
    key = (category, asset_id)
    if key in universe:
        return universe[key]
    if category == "stocks" and is_likely_ticker(asset_id):
        return (asset_id, "moderate")
    return None


def _trade_ideas_from_analysis(by_asset_sentiment: Optional[dict]) -> set[tuple[str, str]]:
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


def _all_assets() -> list[tuple[str, str, str, str]]:
    out: list[tuple[str, str, str, str]] = []
    for cat, items in ASSETS_BY_CATEGORY.items():
        for asset_id, symbol, tier in items:
            out.append((cat, asset_id, symbol, tier))
    return out


# ---------------------------------------------------------------------------
# Candidate with direction + expiry + technicals
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    asset_category: str
    asset_id: str
    risk_tier: str
    buy_in_price: float
    stop_loss: float
    take_profit: float
    direction: str = "long"
    expiry_date: Optional[date] = None
    volatility_notes: Optional[str] = None
    confidence_score: int = 0
    risk_reward: Optional[float] = None
    trend_aligned: bool = False
    market_regime_note: Optional[str] = None
    _score: float = 0.0


# ---------------------------------------------------------------------------
# Public helpers (used by scheduler, research, briefing)
# ---------------------------------------------------------------------------

def get_yf_symbol(category: str, asset_id: str) -> Optional[str]:
    universe = _universe_lookup()
    resolved = _resolve_symbol_risk(category, asset_id, universe)
    if not resolved:
        return None
    return resolved[0]


def get_asset_price_and_risk(category: str, asset_id: str) -> Optional[tuple[float, str]]:
    universe = _universe_lookup()
    resolved = _resolve_symbol_risk(category, asset_id, universe)
    if not resolved:
        return None
    symbol, risk_tier = resolved
    price = get_cached_price(symbol)
    if price is None or price <= 0:
        return None
    return (round(price, 2), risk_tier)


def get_sentiment_for_asset(
    category: str,
    asset_id: str,
    by_asset_sentiment: dict[str, dict],
) -> Optional[float]:
    pair = get_sentiment_score_and_count(category, asset_id, by_asset_sentiment)
    return pair[0] if pair else None


def get_sentiment_score_and_count(
    category: str,
    asset_id: str,
    by_asset_sentiment: dict[str, dict],
) -> Optional[tuple[float, int]]:
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
    if category == "stocks" and asset_id in by_asset_sentiment:
        info = by_asset_sentiment[asset_id]
        avg = info.get("avg")
        cnt = info.get("count", 0)
        if avg is not None:
            if best_avg is None or avg > best_avg:
                best_avg = avg
            total_count += cnt
    return (best_avg, total_count) if best_avg is not None else None


_CRYPTO_SUFFIXES = ("-USD", "-USDT")
_KNOWN_CRYPTO = frozenset({
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "DOT", "AVAX", "MATIC",
    "LINK", "UNI", "ATOM", "LTC", "FIL", "NEAR", "APT", "ARB", "OP", "SHIB",
    "HBAR", "ICP", "XLM", "ALGO", "VET", "MANA", "SAND",
})


def _looks_like_symbol(s: str) -> bool:
    """Broader than is_likely_ticker: allows dots (BRK.B) and is not blocklist-filtered."""
    if not s or len(s) < 1 or len(s) > 6:
        return False
    if s != s.upper():
        return False
    return all(c.isalpha() or c == '.' for c in s)


def _try_price(tag: str) -> Optional[float]:
    """Try multiple yfinance symbol variants for a sentiment tag."""
    if tag.upper() in _KNOWN_CRYPTO:
        for suffix in _CRYPTO_SUFFIXES:
            p = get_cached_price(tag.upper() + suffix)
            if p is not None and p > 0:
                return p
        return None
    p = get_cached_price(tag)
    if p is not None and p > 0:
        return p
    if "." in tag:
        p = get_cached_price(tag.replace(".", "-"))
        if p is not None and p > 0:
            return p
    return None


def get_price_snapshot(extra_tags: Optional[set[str]] = None) -> dict[str, float]:
    """
    Price snapshot for the hardcoded universe + any extra sentiment tags that
    look like tickers (e.g. AAPL, TSLA, BRK.B, BNB discovered from news).
    """
    out: dict[str, float] = {}
    for _cat, asset_id, symbol, _tier in _all_assets():
        p = get_cached_price(symbol)
        if p is not None and p > 0:
            out[asset_id] = round(p, 2)
    if extra_tags:
        for tag in extra_tags:
            if tag in out:
                continue
            if not _looks_like_symbol(tag):
                continue
            p = _try_price(tag)
            if p is not None:
                out[tag] = round(p, 2)
    return out


# Keep backward-compat for active-trade update job
def compute_sl_tp_for_price(price: float, risk_tier: str, direction: str = "long") -> tuple[float, float]:
    """
    ATR-aware SL/TP recompute from current price. Tries to use cached snapshot
    for the symbol; falls back to fixed % if no data.
    """
    return compute_atr_levels(price, risk_tier, snapshot=None, direction=direction)


def compute_sl_tp_for_symbol(
    symbol: str, risk_tier: str, direction: str = "long"
) -> Optional[tuple[float, float, float, TechnicalSnapshot]]:
    """
    Full ATR-based computation for a symbol.
    Returns (price, sl, tp, snapshot) or None.
    """
    snap = build_snapshot(symbol)
    if not snap:
        return None
    sl, tp = compute_atr_levels(snap.price, risk_tier, snapshot=snap, direction=direction)
    return (snap.price, sl, tp, snap)


# ---------------------------------------------------------------------------
# Volatility notes builder
# ---------------------------------------------------------------------------

def _build_volatility_notes(snap: Optional[TechnicalSnapshot], direction: str) -> Optional[str]:
    if not snap:
        return None
    parts: list[str] = []
    if snap.atr_pct is not None:
        parts.append(f"ATR: {snap.atr_pct:.2%}")
    if snap.rsi is not None:
        label = "overbought" if snap.rsi > 70 else ("oversold" if snap.rsi < 30 else "neutral")
        parts.append(f"RSI: {snap.rsi:.0f} ({label})")
    if snap.macd is not None:
        _, _, hist = snap.macd
        parts.append(f"MACD: {'bullish' if hist > 0 else 'bearish'}")
    if snap.adx is not None:
        adx_val, plus_di, minus_di = snap.adx
        parts.append(f"ADX: {adx_val:.0f} ({snap.trend_strength} trend)")
    if snap.stochastic is not None:
        k, d = snap.stochastic
        label = "overbought" if k > 80 else ("oversold" if k < 20 else "mid")
        parts.append(f"Stoch: {k:.0f}/{d:.0f} ({label})")
    if snap.obv is not None:
        parts.append(f"OBV: {'rising' if snap.obv[1] > 0 else 'falling'}")
    if snap.bollinger is not None:
        if snap.near_upper_band:
            parts.append("near upper BB")
        elif snap.near_lower_band:
            parts.append("near lower BB")
    if snap.supports:
        parts.append(f"S: {snap.supports[0]}")
    if snap.resistances:
        parts.append(f"R: {snap.resistances[0]}")
    parts.append(f"trend: {snap.trend_bias}")
    parts.append(f"dir: {direction}")
    return "; ".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Candidate builder
# ---------------------------------------------------------------------------

def _build_candidate(
    category: str,
    asset_id: str,
    symbol: str,
    risk_tier: str,
    by_asset_sentiment: Optional[dict],
    market_ctx: Optional[MarketContext] = None,
) -> Optional[Candidate]:
    """Build a single Candidate with ATR-based levels, direction, expiry, score, trend alignment."""
    snap = build_snapshot(symbol)
    if not snap or snap.price <= 0:
        return None

    price = round(snap.price, 2)

    # Direction from sentiment + technicals
    pair = get_sentiment_score_and_count(category, asset_id, by_asset_sentiment or {})
    sentiment_avg = pair[0] if pair else None
    sentiment_count = pair[1] if pair else 0
    direction = determine_direction(sentiment_avg, snap)

    # Multi-timeframe trend alignment check
    alignment = check_trend_alignment(symbol)
    trend_aligned = alignment.aligned

    # If trend alignment conflicts with direction, penalize or flip
    if alignment.aligned and alignment.alignment_score != 0:
        aligned_dir = "long" if alignment.alignment_score > 0 else "short"
        if aligned_dir != direction:
            # Weekly trend disagrees — reduce confidence, don't flip direction outright
            # but note the conflict
            trend_aligned = False

    # Market regime direction override for equities
    regime_note = None
    if market_ctx:
        regime_note = market_ctx.regime.description
        # In risk_off, don't go long on stocks/indices unless very strong signal
        if market_ctx.regime.regime == "risk_off" and direction == "long":
            if category in ("stocks", "indices"):
                direction = "short"  # flip to match regime

    # ATR-based stop loss / take profit
    sl, tp = compute_atr_levels(price, risk_tier, snapshot=snap, direction=direction)

    # Expiry estimation
    expiry_days = estimate_expiry_days(price, tp, snapshot=snap)
    expiry = date.today() + timedelta(days=expiry_days)

    # Ranking score
    score = compute_technical_score(sentiment_avg, sentiment_count, snap)
    # Trend alignment bonus/penalty
    if trend_aligned:
        score += 0.5
    elif alignment.alignment_score != 0:
        aligned_dir = "long" if alignment.alignment_score > 0 else "short"
        if aligned_dir != direction:
            score -= 0.3

    # Confidence score (0–100) with market context adjustment
    confidence = compute_confidence(sentiment_avg, sentiment_count, snap, direction)
    if trend_aligned:
        confidence = min(100, confidence + 8)
    if market_ctx:
        confidence = max(0, min(100, confidence + market_ctx.confidence_adjustment()))

    # Risk:Reward ratio
    rr = calc_risk_reward(price, sl, tp)

    # Volatility notes (include trend alignment info)
    vol_notes = _build_volatility_notes(snap, direction)
    if vol_notes:
        align_label = f"MTF: {'aligned' if trend_aligned else 'conflicting'}"
        ema_label = f"EMA: {alignment.ema_stack}"
        vol_notes = f"{vol_notes}; {align_label}; {ema_label}"

    return Candidate(
        asset_category=category,
        asset_id=asset_id,
        risk_tier=risk_tier,
        buy_in_price=price,
        stop_loss=sl,
        take_profit=tp,
        direction=direction,
        expiry_date=expiry,
        volatility_notes=vol_notes,
        confidence_score=confidence,
        risk_reward=rr,
        trend_aligned=trend_aligned,
        market_regime_note=regime_note,
        _score=score,
    )


def _candidates_for_pairs(
    category: str,
    pairs: list[tuple[str, str]],
    universe: dict[tuple[str, str], tuple[str, str]],
    max_risk_level: int,
    by_asset_sentiment: Optional[dict] = None,
    exclude_taken: Optional[set[tuple[str, str]]] = None,
    market_ctx: Optional[MarketContext] = None,
) -> list[Candidate]:
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
        cand = _build_candidate(category, asset_id, symbol, risk_tier, by_asset_sentiment, market_ctx)
        if cand:
            out.append(cand)
    return out


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def build_daily_candidates_for_category(
    category: str,
    max_count: int = 10,
    by_asset_sentiment: Optional[dict] = None,
    preferred_risk: str = "moderate",
    tie_seed: Optional[str] = None,
    exclude_taken: Optional[set[tuple[str, str]]] = None,
    market_ctx: Optional[MarketContext] = None,
) -> list[Candidate]:
    """
    Best trade ideas from analysis first, then backfill from universe.
    Regime-aware: adjusts max_count, filters by confidence gate, applies trend alignment.
    """
    # Regime-aware max count
    effective_max = max_count
    if market_ctx:
        effective_max = min(max_count, market_ctx.max_recommendations_per_category())

    min_confidence = 40  # default gate
    if market_ctx:
        min_confidence = market_ctx.min_confidence_threshold()

    max_risk_level = RISK_TIER_ORDER.get(preferred_risk, RISK_TIER_ORDER["moderate"])
    universe = _universe_lookup()
    excluded = exclude_taken or set()

    idea_pairs = sorted(_trade_ideas_from_analysis(by_asset_sentiment))
    idea_candidates = _candidates_for_pairs(
        category, idea_pairs, universe, max_risk_level,
        by_asset_sentiment=by_asset_sentiment, exclude_taken=excluded,
        market_ctx=market_ctx,
    )

    # Confidence gate: drop low-conviction candidates
    idea_candidates = [c for c in idea_candidates if c.confidence_score >= min_confidence]

    # Rank: confidence-weighted score. Trend-aligned candidates get priority.
    def _rank_key(c: Candidate) -> tuple:
        tie = hash((tie_seed or "", c.asset_id)) % 10000
        aligned_bonus = 1 if c.trend_aligned else 0
        return (-aligned_bonus, -c._score, -c.confidence_score, tie)

    idea_candidates.sort(key=_rank_key)
    chosen_ids = {c.asset_id for c in idea_candidates[:effective_max]}
    result = idea_candidates[:effective_max]

    print(f"[ENGINE][{category}] idea_candidates after gate (min_conf={min_confidence}): {len(idea_candidates)}")
    # Backfill from universe — use a lower confidence floor since these are
    # curated tradeable assets where technicals alone carry enough signal.
    backfill_min_conf = max(20, min_confidence - 15)
    if len(result) < effective_max:
        items = ASSETS_BY_CATEGORY.get(category, [])
        rest: list[Candidate] = []
        for asset_id, symbol, risk_tier in items:
            if asset_id in chosen_ids or (category, asset_id) in excluded:
                continue
            if RISK_TIER_ORDER.get(risk_tier, 99) > max_risk_level:
                continue
            cand = _build_candidate(category, asset_id, symbol, risk_tier, by_asset_sentiment, market_ctx)
            if not cand:
                print(f"[ENGINE][{category}] {asset_id} ({symbol}): no price data — skipped")
            elif cand.confidence_score < backfill_min_conf:
                print(f"[ENGINE][{category}] {asset_id}: conf={cand.confidence_score} < {backfill_min_conf} — skipped")
            else:
                rest.append(cand)
        rest.sort(key=lambda c: (-c._score, hash((tie_seed or "", c.asset_id)) % 10000))
        for c in rest:
            if len(result) >= effective_max:
                break
            result.append(c)
    print(f"[ENGINE][{category}] final: {len(result)} candidates (max={effective_max})")
    return result


def build_daily_candidates(
    max_count: int = 10,
    by_asset_sentiment: Optional[dict] = None,
    preferred_risk: str = "moderate",
    tie_seed: Optional[str] = None,
    exclude_taken: Optional[set[tuple[str, str]]] = None,
) -> list[Candidate]:
    """
    Build up to max_count per category. Clears price cache, detects market regime,
    applies confidence gating, and adjusts recommendations to current conditions.
    Returns (candidates, market_context).
    """
    global _last_market_context
    clear_cache()

    # Build market context once for the whole batch
    market_ctx = build_market_context()
    _last_market_context = market_ctx
    logger.info("Market regime: %s", market_ctx.regime.description)

    out: list[Candidate] = []
    for category in ("stocks", "commodities", "other", "indices"):
        out.extend(build_daily_candidates_for_category(
            category,
            max_count=max_count,
            by_asset_sentiment=by_asset_sentiment,
            preferred_risk=preferred_risk,
            tie_seed=tie_seed,
            exclude_taken=exclude_taken,
            market_ctx=market_ctx,
        ))
    return out


# For external access to the latest market context
_last_market_context: Optional[MarketContext] = None


def get_last_market_context() -> Optional[MarketContext]:
    return _last_market_context
