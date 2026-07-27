"""
Recommendation engine v3: ATR + S/R SL/TP, long/short direction, expiry estimation,
technical signal scoring, market regime awareness, multi-TF trend alignment,
confidence gating, cross-asset correlation. Best N per category (regime-dependent).
"""
import logging
from dataclasses import dataclass
from datetime import date, timedelta
import math
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

from app.services.sentiment.ticker_detection import get_blocklist, is_likely_ticker
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
    get_cached_previous_close,
    get_indicator_conflict_penalty,
    get_return_correlation,
)
from app.services.market_context import (
    MarketContext,
    build_market_context,
    check_trend_alignment,
    TrendAlignment,
    get_sector_for_asset,
    SectorRegime,
)
from app.services.macro_calendar import MACRO_DAY_CONFIDENCE_PENALTY, is_high_impact_day

RISK_TIER_ORDER = {"conservative": 0, "moderate": 1, "aggressive": 2}

# When indicator conflict penalty >= this, candidate is skipped (not recommended).
CONFLICT_SKIP_THRESHOLD = 25  # raised from 20 to allow more candidates through

# Skip adding a candidate if its return correlation with any already-selected recommendation exceeds this (avoid over-concentration).
CORRELATION_THRESHOLD = 0.75

# Minimum Risk:Reward ratio to include a recommendation (skip trades where reward doesn't justify risk).
MIN_RISK_REWARD = 1.5

# Macro-day confidence penalty: see `MACRO_DAY_CONFIDENCE_PENALTY` in `app.services.macro_calendar`.

# Daily batch: up to this many recommendations **per** category (stocks, commodities, other, indices).
# Four categories → up to 4 × MAX_RECOMMENDATIONS_PER_CATEGORY (default 20) before correlation / budget trims.
MAX_RECOMMENDATIONS_PER_CATEGORY = 5
# Max depth when ranking within each category (backfill + ideas); must be >= MAX_RECOMMENDATIONS_PER_CATEGORY.
POOL_PER_CATEGORY = 15

BATCH_CATEGORIES: tuple[str, ...] = ("stocks", "commodities", "other", "indices")

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

def _suggested_risk_pct(
    confidence: int,
    risk_reward: Optional[float],
    trend_aligned: bool = False,
    sector_aligned: bool = False,
) -> float:
    """
    Suggested risk per trade as % of capital (half-Kelly style when R:R known; else tier-based).
    
    Adjustments:
    - Trend aligned: +20% size boost (multi-timeframe confirmation)
    - Sector aligned: +10% size boost (sector supports direction)
    - Both aligned: +30% total boost
    
    Capped at 2.5%, floored at 0.1%.
    """
    p = (confidence or 0) / 100.0

    def _tier_fallback() -> float:
        if p >= 0.70:
            return 1.0
        if p >= 0.50:
            return 0.75
        if p >= 0.30:
            return 0.5
        return 0.25

    if risk_reward is not None and risk_reward > 1.0:
        # half-Kelly: f = 0.5 * (p * R - 1) / (R - 1)
        k = 0.5 * (p * risk_reward - 1.0) / (risk_reward - 1.0)
        base_pct = max(0.0, 100.0 * k)
        if base_pct < 0.01:
            base_pct = _tier_fallback()
    else:
        base_pct = _tier_fallback()
    
    # Apply alignment boosts
    multiplier = 1.0
    if trend_aligned:
        multiplier += 0.20  # +20% for multi-TF alignment
    if sector_aligned:
        multiplier += 0.10  # +10% for sector alignment
    
    final_pct = base_pct * multiplier
    
    # Cap at 2.5%, floor at 0.1%
    return round(min(2.5, max(0.1, final_pct)), 2)


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
    suggested_risk_pct: float = 0.5  # suggested risk % of capital (position sizing)
    # A/B shadow: live batch unchanged; optional alternate min-confidence gate (logged to DB)
    min_confidence_gate: Optional[int] = None
    shadow_b_threshold: Optional[int] = None
    shadow_b_pass: Optional[bool] = None


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


# Event types that affect specific tags (tag -> event_type). When active_events contains event_type, reduce confidence for that tag.
TAG_TO_EVENT_TYPE: dict[str, str] = {
    "Fed": "fed",
    "ECB": "macro",
    "BOJ": "macro",
    "BOE": "macro",
    "PBOC": "macro",
}
EVENT_CONFIDENCE_PENALTY = 5


def _event_penalty(
    category: str,
    asset_id: str,
    active_events: Optional[list[str]],
    by_asset_sentiment: Optional[dict],
) -> int:
    """Confidence penalty when asset is tied to an active high-impact event (e.g. Fed, earnings)."""
    if not active_events:
        return 0
    active_set = set(active_events)
    for tag, targets in SENTIMENT_TAG_TO_ASSETS.items():
        if (category, asset_id) not in targets:
            continue
        event_type = TAG_TO_EVENT_TYPE.get(tag)
        if event_type and event_type in active_set:
            return EVENT_CONFIDENCE_PENALTY
    if category == "stocks" and "earnings" in active_set:
        return EVENT_CONFIDENCE_PENALTY  # stocks more sensitive to earnings news
    return 0


def _has_sentiment_divergence(
    category: str,
    asset_id: str,
    by_asset_sentiment: Optional[dict],
) -> bool:
    """True if any sentiment tag for this asset has sentiment–price divergence."""
    if not by_asset_sentiment:
        return False
    for tag, targets in SENTIMENT_TAG_TO_ASSETS.items():
        if (category, asset_id) in targets:
            info = by_asset_sentiment.get(tag)
            if info and info.get("divergence") is True:
                return True
    if category == "stocks" and asset_id in by_asset_sentiment:
        if by_asset_sentiment[asset_id].get("divergence") is True:
            return True
    return False


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


def _tag_eligible_for_extra_yahoo_price(tag: str) -> bool:
    """
    Stricter than _looks_like_symbol for sentiment-derived tags: use blocklist + length
    rules so common headline words (BUY, THE, ECB-as-noise) do not hit Yahoo.
    Still allows BRK.B-style tickers.
    """
    if not tag or not tag.strip():
        return False
    tag = tag.strip()
    if is_likely_ticker(tag):
        return True
    # Dotted class-A style (e.g. BRK.B) — not covered by is_likely_ticker (non-alpha)
    if "." in tag and tag == tag.upper() and len(tag) <= 8:
        base, _, suf = tag.partition(".")
        if base.isalpha() and suf.isalpha() and 1 <= len(base) <= 5 and 1 <= len(suf) <= 2:
            if base not in get_blocklist():
                return True
    return False


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


def _try_previous_price(tag: str) -> Optional[float]:
    """Previous close for a sentiment tag (for divergence)."""
    if tag.upper() in _KNOWN_CRYPTO:
        for suffix in _CRYPTO_SUFFIXES:
            p = get_cached_previous_close(tag.upper() + suffix)
            if p is not None and p > 0:
                return p
        return None
    p = get_cached_previous_close(tag)
    if p is not None and p > 0:
        return p
    if "." in tag:
        p = get_cached_previous_close(tag.replace(".", "-"))
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
            if not _tag_eligible_for_extra_yahoo_price(tag):
                continue
            p = _try_price(tag)
            if p is not None:
                out[tag] = round(p, 2)
    return out


def snapshot_prices_for_recommendations(records: Sequence[Any]) -> dict[str, float]:
    """
    Current price keyed by asset_id for the given rows only (no full-universe scan).
    Use from API handlers to avoid blocking the asyncio loop on get_price_snapshot().
    """
    out: dict[str, float] = {}
    for r in records:
        aid = getattr(r, "asset_id", None) or ""
        if not aid:
            continue
        cat = getattr(r, "asset_category", None) or "stocks"
        sym = get_yf_symbol(cat, aid)
        if not sym:
            continue
        p = get_cached_price(sym)
        if p is not None and p > 0:
            out[aid] = round(float(p), 2)
    return out


def get_previous_price_snapshot(extra_tags: Optional[set[str]] = None) -> dict[str, float]:
    """Previous trading day close per asset (for sentiment–price divergence)."""
    out: dict[str, float] = {}
    for _cat, asset_id, symbol, _tier in _all_assets():
        p = get_cached_previous_close(symbol)
        if p is not None and p > 0:
            out[asset_id] = round(p, 2)
    if extra_tags:
        for tag in extra_tags:
            if tag in out:
                continue
            if not _tag_eligible_for_extra_yahoo_price(tag):
                continue
            p = _try_previous_price(tag)
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
    # ATR slope regime affects SL/TP width (see compute_atr_levels); expose for UI / research (Section 2 roadmap)
    vreg = getattr(snap, "volatility_regime", None) or "stable"
    parts.append(f"Vol regime: {vreg}")
    parts.append(f"trend: {snap.trend_bias}")
    parts.append(f"dir: {direction}")
    return "; ".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Sector regime adjustment
# ---------------------------------------------------------------------------

def _correlation_divergence_penalty(market_ctx: MarketContext, category: str, asset_id: str) -> int:
    """
    Calculate confidence penalty when cross-asset correlations are diverging.
    
    Divergences signal potential regime shifts or market stress, increasing uncertainty.
    
    Penalties:
    - SPX/VIX divergence (both moving same direction): -8 for stocks/indices
    - Gold/USD divergence (both moving same direction): -5 for commodities
    - Gold/SPX divergence: -5 for all (risk sentiment unclear)
    
    Assets directly involved in a diverging pair get higher penalties.
    """
    if not market_ctx.has_divergence_warning:
        return 0
    
    penalty = 0
    asset_upper = asset_id.upper()
    
    for cs in market_ctx.correlation_signals:
        if not cs.diverging:
            continue
        
        pair = cs.pair_name
        
        # SPX/VIX divergence — major warning for equities
        if pair == "spx_vix_inverse":
            if category in ("stocks", "indices"):
                penalty += 8
            else:
                penalty += 3  # still a warning for other assets
        
        # Gold/USD divergence — warning for commodities
        elif pair == "gold_usd_inverse":
            if category == "commodities" or "GOLD" in asset_upper or "XAU" in asset_upper:
                penalty += 6
            else:
                penalty += 2
        
        # Gold/SPX divergence — risk sentiment unclear
        elif pair == "gold_spx_risk":
            penalty += 5
    
    return min(penalty, 15)  # cap total penalty


def _sector_regime_adjustment(sector_regime: SectorRegime, direction: str) -> int:
    """
    Calculate confidence adjustment based on sector regime and trade direction.
    
    - Sector risk_on + long direction: +5 (sector supports the trade)
    - Sector risk_off + short direction: +5 (sector supports the trade)
    - Sector risk_on + short direction: -5 (trading against sector trend)
    - Sector risk_off + long direction: -8 (trading against weak sector)
    - Sector cautious: 0 (neutral)
    
    Also considers relative strength vs SPX:
    - Outperforming sector (rs > 2%): additional +3 for longs
    - Underperforming sector (rs < -2%): additional +3 for shorts, -3 for longs
    """
    adj = 0
    regime = sector_regime.regime
    rs = sector_regime.relative_strength
    
    # Base adjustment from sector regime
    if regime == "risk_on":
        if direction == "long":
            adj += 5  # sector bullish, going long = aligned
        else:
            adj -= 5  # sector bullish, going short = against trend
    elif regime == "risk_off":
        if direction == "short":
            adj += 5  # sector bearish, going short = aligned
        else:
            adj -= 8  # sector bearish, going long = risky
    # cautious = 0 adjustment
    
    # Relative strength adjustment
    if rs is not None:
        if rs > 0.02:  # outperforming SPX by >2%
            if direction == "long":
                adj += 3
        elif rs < -0.02:  # underperforming SPX by >2%
            if direction == "short":
                adj += 3
            else:
                adj -= 3
    
    return adj


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
    active_events: Optional[list[str]] = None,
) -> Optional[Candidate]:
    """Build a single Candidate with ATR-based levels, direction, expiry, score, trend alignment."""
    snap = build_snapshot(symbol)
    if not snap or snap.price <= 0 or math.isnan(snap.price):
        return None

    price = round(snap.price, 2)

    # Direction from sentiment + technicals (regime-dependent weights)
    pair = get_sentiment_score_and_count(category, asset_id, by_asset_sentiment or {})
    sentiment_avg = pair[0] if pair else None
    sentiment_count = pair[1] if pair else 0
    regime_id = market_ctx.regime.regime if market_ctx else None
    direction = determine_direction(sentiment_avg, snap, regime=regime_id)

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
    sector_regime: Optional[SectorRegime] = None
    if market_ctx:
        regime_note = market_ctx.regime.description
        
        # Check sector-specific regime if available
        sector = get_sector_for_asset(asset_id, category)
        if sector and sector in market_ctx.sector_regimes:
            sector_regime = market_ctx.sector_regimes[sector]
            logger.debug("[ENGINE] %s: sector=%s, sector_regime=%s", asset_id, sector, sector_regime.regime)
        
        # In risk_off, don't go long on stocks/indices unless very strong signal
        if market_ctx.regime.regime == "risk_off" and direction == "long":
            if category in ("stocks", "indices"):
                direction = "short"  # flip to match regime

    # ATR-based stop loss / take profit
    sl, tp = compute_atr_levels(price, risk_tier, snapshot=snap, direction=direction)
    if math.isnan(sl) or math.isnan(tp):
        logger.warning("[ENGINE] Skip %s (%s): NaN SL/TP from compute_atr_levels", asset_id, symbol)
        return None

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

    # Confidence score (0–100) with market context adjustment; regime-specific thresholds
    base_confidence = compute_confidence(sentiment_avg, sentiment_count, snap, direction, regime=regime_id)
    confidence = base_confidence
    logger.debug(
        "[ENGINE] %s (%s): base_conf=%d, sent_avg=%s, sent_cnt=%d, dir=%s",
        asset_id, symbol, base_confidence, sentiment_avg, sentiment_count, direction,
    )
    if trend_aligned:
        confidence = min(100, confidence + 8)
    if market_ctx:
        adj = market_ctx.confidence_adjustment()
        confidence = max(0, min(100, confidence + adj))
        if adj != 0:
            logger.debug("[ENGINE] %s: regime conf adj=%d → %d", asset_id, adj, confidence)
        
        # Sector-specific regime adjustment
        if sector_regime:
            sector_adj = _sector_regime_adjustment(sector_regime, direction)
            if sector_adj != 0:
                confidence = max(0, min(100, confidence + sector_adj))
                score += sector_adj / 20  # also adjust ranking score
                logger.debug("[ENGINE] %s: sector regime adj=%d → conf=%d", asset_id, sector_adj, confidence)
                # Add sector info to regime note
                sector_note = f"Sector ({sector_regime.sector}): {sector_regime.regime.replace('_', ' ')}"
                if sector_regime.relative_strength is not None:
                    rs_pct = sector_regime.relative_strength * 100
                    sector_note += f" ({rs_pct:+.1f}% vs SPX)"
                if regime_note:
                    regime_note = f"{regime_note}; {sector_note}"
                else:
                    regime_note = sector_note
        
        # Cross-asset correlation divergence penalty
        if market_ctx.has_divergence_warning:
            div_penalty = _correlation_divergence_penalty(market_ctx, category, asset_id)
            if div_penalty > 0:
                confidence = max(0, confidence - div_penalty)
                score -= div_penalty / 20
                logger.debug("[ENGINE] %s: correlation divergence penalty=%d → conf=%d", asset_id, div_penalty, confidence)
                div_pairs = [cs.pair_name for cs in market_ctx.correlation_signals if cs.diverging]
                div_note = f"Correlation divergence ({', '.join(div_pairs)})"
                if regime_note:
                    regime_note = f"{regime_note}; {div_note}"
                else:
                    regime_note = div_note
    
    # Sentiment–price divergence: reduce confidence when sentiment and price move in opposite directions
    if _has_sentiment_divergence(category, asset_id, by_asset_sentiment):
        confidence = max(0, confidence - 10)
        logger.debug("[ENGINE] %s: sentiment–price divergence → conf=%d", asset_id, confidence)
        if regime_note:
            regime_note = f"{regime_note}; sentiment–price divergence"
        else:
            regime_note = "Sentiment–price divergence"

    # Event-type penalty: reduce confidence when asset is in an active high-impact event (Fed, earnings, etc.)
    event_pen = _event_penalty(category, asset_id, active_events, by_asset_sentiment)
    if event_pen > 0:
        confidence = max(0, confidence - event_pen)
        logger.debug("[ENGINE] %s: event penalty=%d → conf=%d", asset_id, event_pen, confidence)
        if regime_note:
            regime_note = f"{regime_note}; high-impact event"
        else:
            regime_note = "High-impact event"

    # Skip candidate when key indicators strongly conflict with direction (e.g. RSI OB + long, MACD bear + long)
    conflict_penalty = get_indicator_conflict_penalty(snap, direction, regime=regime_id)
    logger.debug("[ENGINE] %s: conflict_penalty=%d", asset_id, conflict_penalty)
    if conflict_penalty >= CONFLICT_SKIP_THRESHOLD:
        logger.info(
            "[ENGINE] Skip %s (%s): indicator conflict penalty=%d (direction=%s)",
            asset_id, symbol, conflict_penalty, direction,
        )
        return None

    # Risk:Reward ratio
    rr = calc_risk_reward(price, sl, tp)

    # Skip candidates with poor R:R (reward doesn't justify risk)
    if rr is not None and rr < MIN_RISK_REWARD:
        logger.info(
            "[ENGINE] Skip %s (%s): R:R=%.2f < %.2f minimum",
            asset_id, symbol, rr, MIN_RISK_REWARD,
        )
        return None

    # Volatility notes (include trend alignment info)
    vol_notes = _build_volatility_notes(snap, direction)
    if vol_notes:
        align_label = f"MTF: {'aligned' if trend_aligned else 'conflicting'}"
        ema_label = f"EMA: {alignment.ema_stack}"
        vol_notes = f"{vol_notes}; {align_label}; {ema_label}"
    # Survivors only: show how close this row was to being dropped for indicator conflict
    if conflict_penalty > 0:
        suffix = f"indicator conflict penalty: {conflict_penalty} (skip if ≥{CONFLICT_SKIP_THRESHOLD})"
        vol_notes = f"{vol_notes}; {suffix}" if vol_notes else suffix

    # Determine if sector is aligned with trade direction
    sector_aligned = False
    if sector_regime:
        if sector_regime.regime == "risk_on" and direction == "long":
            sector_aligned = True
        elif sector_regime.regime == "risk_off" and direction == "short":
            sector_aligned = True
    
    suggested_risk = _suggested_risk_pct(confidence, rr, trend_aligned=trend_aligned, sector_aligned=sector_aligned)
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
        suggested_risk_pct=suggested_risk,
    )


def _candidates_for_pairs(
    category: str,
    pairs: list[tuple[str, str]],
    universe: dict[tuple[str, str], tuple[str, str]],
    max_risk_level: int,
    by_asset_sentiment: Optional[dict] = None,
    exclude_taken: Optional[set[tuple[str, str]]] = None,
    market_ctx: Optional[MarketContext] = None,
    active_events: Optional[list[str]] = None,
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
        cand = _build_candidate(category, asset_id, symbol, risk_tier, by_asset_sentiment, market_ctx, active_events)
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
    active_events: Optional[list[str]] = None,
) -> list[Candidate]:
    """
    Best trade ideas from analysis first, then backfill from universe.
    Regime-aware: adjusts max_count, filters by confidence gate, applies trend alignment.
    """
    # Regime-aware max count
    effective_max = max_count
    if market_ctx:
        effective_max = min(max_count, market_ctx.max_recommendations_per_category())

    min_confidence = 35  # default gate (lowered from 40)
    if market_ctx:
        min_confidence = market_ctx.min_confidence_threshold()
    logger.info("[ENGINE][%s] min_confidence=%d (regime=%s)", category, min_confidence, market_ctx.regime.regime if market_ctx else None)

    max_risk_level = RISK_TIER_ORDER.get(preferred_risk, RISK_TIER_ORDER["moderate"])
    universe = _universe_lookup()
    excluded = exclude_taken or set()

    idea_pairs = sorted(_trade_ideas_from_analysis(by_asset_sentiment))
    idea_candidates = _candidates_for_pairs(
        category, idea_pairs, universe, max_risk_level,
        by_asset_sentiment=by_asset_sentiment, exclude_taken=excluded,
        market_ctx=market_ctx, active_events=active_events,
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

    logger.info("[ENGINE][%s] idea_candidates after gate (min_conf=%d): %d", category, min_confidence, len(idea_candidates))
    # Backfill from universe — use a lower confidence floor since these are
    # curated tradeable assets where technicals alone carry enough signal.
    backfill_min_conf = max(15, min_confidence - 20)  # lowered from max(20, min-15)
    if len(result) < effective_max:
        items = ASSETS_BY_CATEGORY.get(category, [])
        rest: list[Candidate] = []
        for asset_id, symbol, risk_tier in items:
            if asset_id in chosen_ids or (category, asset_id) in excluded:
                continue
            if RISK_TIER_ORDER.get(risk_tier, 99) > max_risk_level:
                continue
            cand = _build_candidate(category, asset_id, symbol, risk_tier, by_asset_sentiment, market_ctx, active_events)
            if not cand:
                logger.debug("[ENGINE][%s] %s (%s): no candidate built (no price or skipped)", category, asset_id, symbol)
            elif cand.confidence_score < backfill_min_conf:
                logger.debug("[ENGINE][%s] %s: conf=%d < %d — skipped", category, asset_id, cand.confidence_score, backfill_min_conf)
            else:
                rest.append(cand)
        rest.sort(key=lambda c: (-c._score, hash((tie_seed or "", c.asset_id)) % 10000))
        for c in rest:
            if len(result) >= effective_max:
                break
            result.append(c)
    logger.info("[ENGINE][%s] final: %d candidates (max=%d)", category, len(result), effective_max)
    return result


def build_daily_candidates(
    max_per_category: Optional[int] = None,
    pool_per_category: int = POOL_PER_CATEGORY,
    by_asset_sentiment: Optional[dict] = None,
    preferred_risk: str = "moderate",
    tie_seed: Optional[str] = None,
    exclude_taken: Optional[set[tuple[str, str]]] = None,
    active_events: Optional[list[str]] = None,
    risk_budget_1r: float = 25.0,
) -> list[Candidate]:
    """
    Build today's batch: up to ``max_per_category`` recommendations **per** category
    (stocks, commodities, other, indices), i.e. up to **4 × max_per_category** (default 20) before
    correlation and risk-budget filters.

    For each category, candidates are ranked internally; we then walk in order and keep up to
    ``max_per_category`` that pass **global** correlation (vs already chosen rows) and **global**
    risk budget (sum of Size %).
    """
    global _last_market_context
    clear_cache()

    market_ctx = build_market_context(include_sectors=True)
    _last_market_context = market_ctx
    logger.info("Market regime: %s", market_ctx.regime.description)
    if market_ctx.sector_regimes:
        sector_summary = ", ".join(f"{s}={sr.regime}" for s, sr in market_ctx.sector_regimes.items())
        logger.info("Sector regimes: %s", sector_summary)

    cap = max_per_category if max_per_category is not None else MAX_RECOMMENDATIONS_PER_CATEGORY
    cap = max(1, int(cap))
    logger.info(
        "[ENGINE] Batch: up to %d per category × %d categories (regime=%s)",
        cap,
        len(BATCH_CATEGORIES),
        market_ctx.regime.regime,
    )

    out: list[Candidate] = []
    seen_asset_key: set[tuple[str, str]] = set()
    total_risk_pct = 0.0

    def _try_add(c: Candidate) -> bool:
        nonlocal total_risk_pct
        key = (c.asset_category, c.asset_id)
        if key in seen_asset_key:
            return False
        symbol_new = get_yf_symbol(c.asset_category, c.asset_id)
        if symbol_new and out:
            max_corr: Optional[float] = None
            for c_old in out:
                symbol_old = get_yf_symbol(c_old.asset_category, c_old.asset_id)
                if not symbol_old or symbol_old == symbol_new:
                    continue
                corr = get_return_correlation(symbol_old, symbol_new)
                if corr is not None:
                    max_corr = corr if max_corr is None else max(max_corr, corr)
            if max_corr is not None and max_corr >= CORRELATION_THRESHOLD:
                logger.info(
                    "[ENGINE] Skip %s (%s): correlation %.2f with existing recs (threshold %.2f)",
                    c.asset_id, symbol_new, max_corr, CORRELATION_THRESHOLD,
                )
                return False
        candidate_risk = c.suggested_risk_pct or 0.0
        if total_risk_pct + candidate_risk > risk_budget_1r:
            logger.info(
                "[ENGINE] Skip %s (%s): risk budget %.2f%% + %.2f%% > %.2f%% (budget)",
                c.asset_id, symbol_new, total_risk_pct, candidate_risk, risk_budget_1r,
            )
            return False
        seen_asset_key.add(key)
        out.append(c)
        total_risk_pct += candidate_risk
        return True

    for category in BATCH_CATEGORIES:
        ranked = build_daily_candidates_for_category(
            category,
            max_count=max(pool_per_category, cap),
            by_asset_sentiment=by_asset_sentiment,
            preferred_risk=preferred_risk,
            tie_seed=tie_seed,
            exclude_taken=exclude_taken,
            market_ctx=market_ctx,
            active_events=active_events,
        )
        taken = 0
        for c in ranked:
            if taken >= cap:
                break
            if _try_add(c):
                taken += 1
        logger.info("[ENGINE][%s] added %d / target %d to batch", category, taken, cap)

    if is_high_impact_day(date.today(), window_days=1):
        for c in out:
            current = c.confidence_score or 0
            c.confidence_score = max(0, current - MACRO_DAY_CONFIDENCE_PENALTY)
        logger.info(
            "[ENGINE] High-impact macro day: applied confidence penalty %d to %d candidates",
            MACRO_DAY_CONFIDENCE_PENALTY, len(out),
        )

    # Shadow profile B: alternate min-confidence gate (does not change who is in the batch)
    from app.core.preferences import get_shadow_mode_config

    sm = get_shadow_mode_config()
    if sm.get("enabled"):
        min_live = market_ctx.min_confidence_threshold()
        delta = int(sm.get("confidence_delta", 0))
        min_b = max(0, min(100, min_live + delta))
        for c in out:
            conf = c.confidence_score or 0
            c.min_confidence_gate = min_live
            c.shadow_b_threshold = min_b
            c.shadow_b_pass = conf >= min_b
        n_pass_b = sum(1 for c in out if c.shadow_b_pass)
        logger.info(
            "[ENGINE] Shadow mode B: live_min=%d delta=%d → B_threshold=%d | %d/%d rows would pass B",
            min_live,
            delta,
            min_b,
            n_pass_b,
            len(out),
        )
    else:
        for c in out:
            c.min_confidence_gate = None
            c.shadow_b_threshold = None
            c.shadow_b_pass = None

    logger.info(
        "[ENGINE] Final batch: %d candidates (≤%d per category), total risk budget used: %.2f%% / %.2f%%",
        len(out),
        cap,
        total_risk_pct,
        risk_budget_1r,
    )
    return out


# For external access to the latest market context
_last_market_context: Optional[MarketContext] = None


def get_last_market_context() -> Optional[MarketContext]:
    return _last_market_context
