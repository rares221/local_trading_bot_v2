"""
Recommendation engine v3: ATR + S/R SL/TP, long/short direction, expiry estimation,
technical signal scoring, market regime awareness, multi-TF trend alignment,
confidence gating, cross-asset correlation. Best N per category (regime-dependent).

Per–asset-class tuning (commodities / FX / crypto / VIX vs equities) lives in
``asset_class_policy.py`` (Option A policy layer).

Structured skip forensics: grep ``[ENGINE_SKIP]``; each batch ends with ``[ENGINE_DIGEST]``.
Both include ``pipeline_run_id`` (UUID) for a single ``build_daily_candidates`` invocation.

Skip reasons include ``exclude_taken`` (``status=taken`` for that category+asset — logged with
``phase`` ideas|backfill), ``skip_backfill_already_in_ideas``, ``risk_tier_above_preference``,
``batch_duplicate_asset``, plus existing gates (confidence, R:R, correlation, etc.).
"""
import calendar
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional, Sequence
from uuid import uuid4

logger = logging.getLogger(__name__)

# Set only during ``build_daily_candidates`` so skips aggregate into one digest line.
_ENGINE_BATCH_LOG: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "ENGINE_BATCH_LOG", default=None
)


def _log_engine_skip(reason: str, **fields: Any) -> None:
    """Single-line JSON for grep / dashboards; counts toward ``[ENGINE_DIGEST]`` when in a batch."""
    payload: dict[str, Any] = {"reason": reason}
    for k, v in fields.items():
        if v is not None:
            payload[k] = v
    ctx = _ENGINE_BATCH_LOG.get()
    if ctx is not None:
        payload["batch_date"] = ctx["batch_date"]
        prid = ctx.get("pipeline_run_id")
        if prid:
            payload["pipeline_run_id"] = prid
        skips: dict[str, int] = ctx["skips"]
        skips[reason] = skips.get(reason, 0) + 1
        cat = fields.get("category")
        if isinstance(cat, str) and cat:
            by_cat: dict[str, dict[str, int]] = ctx["skips_by_cat"]
            bc = by_cat.setdefault(cat, {})
            bc[reason] = bc.get(reason, 0) + 1
    logger.info("[ENGINE_SKIP] %s", json.dumps(payload, default=str, separators=(",", ":")))

from app.services.sentiment.ticker_detection import get_blocklist, is_likely_ticker
from app.services.technicals import (
    TechnicalSnapshot,
    build_snapshot,
    calc_risk_reward,
    clear_cache,
    compute_atr_levels,
    compute_confidence,
    compute_technical_score,
    compute_relative_strength,
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
    get_sector_etf_for_asset,
    SectorRegime,
)
from app.services.macro_calendar import MACRO_DAY_CONFIDENCE_PENALTY, is_high_impact_day
from app.services.recommendations.asset_class_policy import (
    AssetClassPolicy,
    get_asset_class_policy,
)

RISK_TIER_ORDER = {"conservative": 0, "moderate": 1, "aggressive": 2}

# When indicator conflict penalty >= this, candidate is skipped (not recommended).
CONFLICT_SKIP_THRESHOLD = 25  # raised from 20 to allow more candidates through

# Skip adding a candidate if its return correlation with any already-selected recommendation exceeds this (avoid over-concentration).
CORRELATION_THRESHOLD = 0.75

# Minimum Risk:Reward ratio to include a recommendation (skip trades where reward doesn't justify risk).
MIN_RISK_REWARD = 2.0  # raised 1.6→2.0: breakeven WR drops from 38.5% to 33.3%, giving our 35% WR positive EV
# Short trades disabled: backtest shows 24.7% WR for shorts vs 35.8% for longs — shorts are net -EV.
# Re-enable once short signal quality improves (e.g. dedicated short scoring, bear-regime-only filter).
SHORTS_ENABLED = False
# Bear-regime exception: re-enable shorts when market_regime == "bear".
# Backtest aggregate short WR was 22.8% — but bear-regime-only shorts are expected to perform better.
# Confidence floor of BEAR_REGIME_SHORT_MIN_CONFIDENCE enforced further below in _build_candidate().
# Note: when SHORTS_ENABLED=True, this floor only applies in bear regime; non-bear shorts bypass it.
SHORTS_BEAR_REGIME_ENABLED = False

# Minimum confidence required for a bear-regime short to pass (vs ~35-40 for longs).
BEAR_REGIME_SHORT_MIN_CONFIDENCE = 50

# Macro-day confidence penalty: see `MACRO_DAY_CONFIDENCE_PENALTY` in `app.services.macro_calendar`.

# Daily batch: up to this many recommendations **per** category (stocks, commodities, other, indices).
# Four categories → up to 4 × MAX_RECOMMENDATIONS_PER_CATEGORY (default 20) before correlation / budget trims.
MAX_RECOMMENDATIONS_PER_CATEGORY = 5
# Max depth when ranking within each category (backfill + ideas); must be >= MAX_RECOMMENDATIONS_PER_CATEGORY.
POOL_PER_CATEGORY = 15

# Headline tags that pass ``is_likely_ticker`` but are not in ``SENTIMENT_TAG_TO_ASSETS``:
# require at least this many weighted mentions before we add a discovery stock idea, and cap per batch.
MIN_STOCK_TAG_MENTIONS_FOR_DISCOVERY = 2
MAX_DISCOVERY_STOCK_IDEAS_PER_BATCH = 48

# Sentiment tag stays the pipeline key (e.g. TSMC); Yahoo fetch uses the mapped symbol.
_DISCOVERY_STOCK_YAHOO_SYMBOL: dict[str, str] = {
    "TSMC": "TSM",
    "FB": "META",
}

# All stock cap-tier categories — use this tuple anywhere you'd otherwise hardcode "stocks".
_STOCK_CATEGORIES: tuple[str, ...] = ("stocks_large", "stocks_mid", "stocks_small")

BATCH_CATEGORIES: tuple[str, ...] = ("stocks_large", "stocks_mid", "stocks_small", "commodities", "other", "indices")

SENTIMENT_TAG_TO_ASSETS: dict[str, list[tuple[str, str]]] = {
    # Commodities + thematic energy
    "oil": [("commodities", "WTI"), ("commodities", "Brent"), ("stocks_large", "XOM"), ("stocks_large", "CVX")],
    "OIL": [("commodities", "WTI"), ("commodities", "Brent"), ("stocks_large", "XOM"), ("stocks_large", "CVX")],
    "gold": [("commodities", "XAUUSD"), ("stocks_large", "NEM")],
    "gas": [("commodities", "Henry Hub")],
    "uranium": [("stocks_mid", "CCJ")],
    "solar": [("stocks_mid", "ENPH"), ("stocks_mid", "FSLR")],
    "clean energy": [("stocks_mid", "ENPH"), ("stocks_mid", "FSLR")],
    "EV": [("stocks_large", "TSLA"), ("stocks_mid", "RIVN")],
    "electric vehicle": [("stocks_large", "TSLA"), ("stocks_mid", "RIVN")],
    "AI": [("stocks_large", "NVDA"), ("stocks_large", "MSFT"), ("stocks_large", "GOOGL"), ("stocks_mid", "PLTR")],
    "artificial intelligence": [("stocks_large", "NVDA"), ("stocks_large", "MSFT"), ("stocks_mid", "PLTR")],
    "semiconductor": [("stocks_large", "NVDA"), ("stocks_large", "AVGO")],
    "tech": [("stocks_large", "AAPL"), ("stocks_large", "MSFT"), ("stocks_large", "NVDA")],
    "banking": [("stocks_large", "JPM"), ("stocks_large", "BAC"), ("stocks_large", "GS")],
    "fintech": [("stocks_mid", "SOFI"), ("stocks_mid", "HOOD")],
    "space": [("stocks_mid", "RKLB"), ("stocks_small", "LUNR"), ("stocks_small", "ACHR"), ("stocks_small", "ASTS")],
    # Large cap direct tickers
    "AAPL": [("stocks_large", "AAPL")],
    "MSFT": [("stocks_large", "MSFT")],
    "GOOGL": [("stocks_large", "GOOGL")],
    "GOOG": [("stocks_large", "GOOGL")],
    "NVDA": [("stocks_large", "NVDA")],
    "META": [("stocks_large", "META")],
    "AMZN": [("stocks_large", "AMZN")],
    "TSLA": [("stocks_large", "TSLA")],
    "JPM": [("stocks_large", "JPM")],
    "BAC": [("stocks_large", "BAC")],
    "GS": [("stocks_large", "GS")],
    "JNJ": [("stocks_large", "JNJ")],
    "UNH": [("stocks_large", "UNH")],
    "XOM": [("stocks_large", "XOM")],
    "CVX": [("stocks_large", "CVX")],
    "BP": [("stocks_large", "BP")],
    "SHEL": [("stocks_large", "SHEL")],
    "NEM": [("stocks_large", "NEM")],
    "PG": [("stocks_large", "PG")],
    "V": [("stocks_large", "V")],
    "WMT": [("stocks_large", "WMT")],
    "AVGO": [("stocks_large", "AVGO")],
    # Mid cap direct tickers
    "PLTR": [("stocks_mid", "PLTR")],
    "SMCI": [("stocks_mid", "SMCI")],
    "IONQ": [("stocks_mid", "IONQ")],
    "RIVN": [("stocks_mid", "RIVN")],
    "ENPH": [("stocks_mid", "ENPH")],
    "FSLR": [("stocks_mid", "FSLR")],
    "CCJ": [("stocks_mid", "CCJ")],
    "SOFI": [("stocks_mid", "SOFI")],
    "HOOD": [("stocks_mid", "HOOD")],
    "RIG": [("stocks_mid", "RIG")],
    "HP": [("stocks_mid", "HP")],
    "MP": [("stocks_mid", "MP")],
    "HIMS": [("stocks_mid", "HIMS")],
    "RKLB": [("stocks_mid", "RKLB")],
    # Small cap direct tickers
    "BBAI": [("stocks_small", "BBAI")],
    "LUNR": [("stocks_small", "LUNR")],
    "ACHR": [("stocks_small", "ACHR")],
    "ASTS": [("stocks_small", "ASTS")],
    "JOBY": [("stocks_small", "JOBY")],
    "ACMR": [("stocks_small", "ACMR")],
    "ARRY": [("stocks_small", "ARRY")],
    "RXRX": [("stocks_small", "RXRX")],
    "SOUN": [("stocks_small", "SOUN")],
    "OPEN": [("stocks_small", "OPEN")],
    # Commodities
    "WTI": [("commodities", "WTI")],
    "Brent": [("commodities", "Brent")],
    "XAUUSD": [("commodities", "XAUUSD")],
    "Henry Hub": [("commodities", "Henry Hub")],
    "Silver": [("commodities", "Silver")],
    # Crypto
    "BTC": [("other", "BTC")],
    "ETH": [("other", "ETH")],
    # Indices
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
    "NIFTY": [("indices", "Nifty")],
    "Nifty": [("indices", "Nifty")],
}

ASSETS_BY_CATEGORY = {
    "stocks_large": [
        # Tech megacap
        ("AAPL", "AAPL", "moderate"),
        ("MSFT", "MSFT", "moderate"),
        ("GOOGL", "GOOGL", "moderate"),
        ("NVDA", "NVDA", "moderate"),
        ("META", "META", "moderate"),
        ("AMZN", "AMZN", "moderate"),
        ("TSLA", "TSLA", "aggressive"),
        ("AVGO", "AVGO", "moderate"),
        # Financials
        ("JPM", "JPM", "moderate"),
        ("BAC", "BAC", "moderate"),
        ("GS", "GS", "moderate"),
        # Healthcare
        ("JNJ", "JNJ", "conservative"),
        ("UNH", "UNH", "moderate"),
        # Energy
        ("XOM", "XOM", "conservative"),
        ("CVX", "CVX", "conservative"),
        ("BP", "BP", "conservative"),
        ("SHEL", "SHEL", "moderate"),
        # Mining / Precious metals
        ("NEM", "NEM", "moderate"),
        # Consumer / Staples
        ("PG", "PG", "conservative"),
        ("WMT", "WMT", "conservative"),
        ("V", "V", "conservative"),
    ],
    "stocks_mid": [
        # AI / Tech
        ("PLTR", "PLTR", "moderate"),
        ("SMCI", "SMCI", "aggressive"),
        ("IONQ", "IONQ", "aggressive"),
        # EV
        ("RIVN", "RIVN", "aggressive"),
        # Clean energy
        ("ENPH", "ENPH", "moderate"),
        ("FSLR", "FSLR", "moderate"),
        # Uranium / Materials
        ("CCJ", "CCJ", "moderate"),
        ("MP", "MP", "aggressive"),
        # Fintech
        ("SOFI", "SOFI", "aggressive"),
        ("HOOD", "HOOD", "aggressive"),
        # Energy services
        ("RIG", "RIG", "aggressive"),
        ("HP", "HP", "moderate"),
        # Health / Consumer
        ("HIMS", "HIMS", "aggressive"),
        ("RKLB", "RKLB", "aggressive"),
    ],
    "stocks_small": [
        # AI
        ("BBAI", "BBAI", "aggressive"),
        ("SOUN", "SOUN", "aggressive"),
        # Space / Mobility
        ("LUNR", "LUNR", "aggressive"),
        ("ACHR", "ACHR", "aggressive"),
        ("ASTS", "ASTS", "aggressive"),
        ("JOBY", "JOBY", "aggressive"),
        # Tech / Semiconductor
        ("ACMR", "ACMR", "aggressive"),
        # Clean energy
        ("ARRY", "ARRY", "aggressive"),
        # Biotech / Health
        ("RXRX", "RXRX", "aggressive"),
        # Real estate tech
        ("OPEN", "OPEN", "aggressive"),
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
        ("Nifty", "^NSEI", "moderate"),
        # Volatility
        ("VIX", "^VIX", "aggressive"),
    ],
}


ABSOLUTE_MIN_CONFIDENCE = 20  # hard floor — nothing below this ever reaches recommendations

def _effective_min_confidence(
    category: str,
    asset_id: str,
    market_ctx: Optional[MarketContext],
) -> int:
    """Regime floor + asset-class offset + dashboard prefs (commodities + optional equity/other).
    Never returns below ABSOLUTE_MIN_CONFIDENCE regardless of regime relief."""
    base = market_ctx.min_confidence_threshold() if market_ctx else 35
    pol = get_asset_class_policy(category, asset_id)
    m = base + pol.min_confidence_offset
    if market_ctx and market_ctx.regime.regime == "risk_off":
        m -= pol.risk_off_gate_relief
    if category == "commodities":
        from app.core.preferences import get_commodity_engine_gates

        m += int(get_commodity_engine_gates()["commodity_confidence_offset"])
    else:
        from app.core.preferences import get_non_commodity_engine_gates

        nc = get_non_commodity_engine_gates()
        if category in (*_STOCK_CATEGORIES, "indices") and pol.key != "indices_vix":
            m += int(nc["equity_confidence_offset"])
        elif category == "other":
            m += int(nc["other_confidence_offset"])
    return max(ABSOLUTE_MIN_CONFIDENCE, min(100, m))


def _min_rr_floor_for_build(category: str, pol: AssetClassPolicy) -> float:
    """R:R floor for candidate build; commodities use commodity prefs; optional equity/other overrides."""
    if category == "commodities":
        from app.core.preferences import get_commodity_engine_gates

        return float(get_commodity_engine_gates()["commodity_min_risk_reward"])
    from app.core.preferences import get_non_commodity_engine_gates

    nc = get_non_commodity_engine_gates()
    if category in (*_STOCK_CATEGORIES, "indices") and pol.key != "indices_vix":
        err = nc.get("equity_min_risk_reward")
        if err is not None:
            return float(err)
    if category == "other":
        orr = nc.get("other_min_risk_reward")
        if orr is not None:
            return float(orr)
    if pol.min_risk_reward is not None:
        return float(pol.min_risk_reward)
    return MIN_RISK_REWARD


def _universe_lookup() -> dict[tuple[str, str], tuple[str, str]]:
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for cat, items in ASSETS_BY_CATEGORY.items():
        for asset_id, symbol, tier in items:
            out[(cat, asset_id)] = (symbol, tier)
    return out


def _resolve_universe_entry(
    category: str,
    asset_id: str,
    universe: dict[tuple[str, str], tuple[str, str]],
) -> Optional[tuple[str, str, str]]:
    """
    Resolve Yahoo symbol and risk tier for (category, asset_id).

    Returns (symbol, risk_tier, canonical_asset_id). The canonical id matches universe spelling
    (e.g. commodities 'Brent') so sentiment maps and UI stay consistent; user input may differ
    in case (e.g. BRENT, brent).
    """
    key = (category, asset_id)
    if key in universe:
        sym, tier = universe[key]
        return (sym, tier, asset_id)
    if category in _STOCK_CATEGORIES and is_likely_ticker(asset_id):
        aid = asset_id.upper()
        yahoo = _DISCOVERY_STOCK_YAHOO_SYMBOL.get(aid, aid)
        return (yahoo, "moderate", aid)
    aid_norm = asset_id.strip().lower()
    for (cat, u_aid), (sym, tier) in universe.items():
        if cat == category and u_aid.lower() == aid_norm:
            return (sym, tier, u_aid)
    return None


def _resolve_symbol_risk(
    category: str,
    asset_id: str,
    universe: dict[tuple[str, str], tuple[str, str]],
) -> Optional[tuple[str, str]]:
    r = _resolve_universe_entry(category, asset_id, universe)
    if not r:
        return None
    return (r[0], r[1])


def _trade_ideas_from_analysis(by_asset_sentiment: Optional[dict]) -> set[tuple[str, str]]:
    if not by_asset_sentiment:
        return set()
    ideas: set[tuple[str, str]] = set()
    discovery: list[tuple[str, int]] = []
    for tag in by_asset_sentiment:
        if tag in SENTIMENT_TAG_TO_ASSETS:
            for pair in SENTIMENT_TAG_TO_ASSETS[tag]:
                ideas.add(pair)
            continue
        if not is_likely_ticker(tag):
            continue
        cnt = int((by_asset_sentiment.get(tag) or {}).get("count") or 0)
        avg = float((by_asset_sentiment.get(tag) or {}).get("avg") or 0.0)
        if cnt < MIN_STOCK_TAG_MENTIONS_FOR_DISCOVERY:
            continue
        if abs(avg) <= 0.05:
            continue  # near-neutral mentions: noise, not signal
        discovery.append((tag, cnt))
    discovery.sort(key=lambda t: (-t[1], t[0]))
    for tag, _ in discovery[:MAX_DISCOVERY_STOCK_IDEAS_PER_BATCH]:
        ideas.add(("stocks_large", tag))
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

def _compute_gap_risk(
    snap,
    expiry_days: int,
    near_earnings: bool,
    category: str,
    today,
    max_penalty: float = 0.50,
) -> tuple[str, float, str]:
    """
    Compute gap risk level and sizing multiplier from technical snapshot + trade context.

    Gap risk = probability that an overnight/weekend price gap blows past the SL,
    causing an actual loss larger than the modeled 1R.

    Returns:
        level: "low" | "medium" | "high"
        sizing_mult: float in [max_penalty, 1.0] — multiply suggested_risk_pct by this
        note: human-readable explanation of active risk factors

    Design:
    - Penalties are multiplicative (compound when multiple factors fire)
    - max_penalty caps the floor so sizing never collapses to zero
    - No changes to SL/TP geometry — only sizing is adjusted
    """
    factors: list[str] = []
    mult = 1.0

    # ── Factor 1: SL distance vs typical overnight gap ───────────────────────
    # If avg gap is >40% of SL distance, a single bad night can eat most of the risk buffer.
    avg_gap = getattr(snap, "overnight_gap_pct_avg", None)
    p95_gap = getattr(snap, "overnight_gap_pct_p95", None)
    atr_pct = getattr(snap, "atr_pct", None)
    if avg_gap and atr_pct and atr_pct > 0:
        # ATR multiplier for moderate tier is 2.0 → SL distance ≈ 2× ATR ≈ 2× atr_pct
        sl_dist_est = atr_pct * 2.0
        ratio = avg_gap / sl_dist_est
        if ratio > 0.5:
            mult *= 0.80
            factors.append(f"gap avg {avg_gap*100:.1f}% vs SL ~{sl_dist_est*100:.1f}%")
        elif ratio > 0.35:
            mult *= 0.90
            factors.append(f"gap avg {avg_gap*100:.1f}%")
    # 95th-pct gap exceeds SL entirely → tail risk is real
    if p95_gap and atr_pct and atr_pct > 0:
        sl_dist_est = atr_pct * 2.0
        if p95_gap > sl_dist_est:
            mult *= 0.85
            factors.append(f"p95 gap {p95_gap*100:.1f}% > SL (tail)")

    # ── Factor 2: Holding period — overnight gap exposure compounds ──────────
    if expiry_days >= 14:
        mult *= 0.75
        factors.append(f"{expiry_days}d hold")
    elif expiry_days >= 7:
        mult *= 0.85
        factors.append(f"{expiry_days}d hold")
    elif expiry_days >= 3:
        mult *= 0.92
        factors.append(f"{expiry_days}d hold")

    # ── Factor 3: Weekend gap (65h exposure vs 17h overnight) ───────────────
    weekday = today.weekday()  # 0=Mon, 4=Fri
    if weekday == 4:  # Friday entry — first gap is 65h
        mult *= 0.80
        factors.append("weekend gap (Friday entry)")
    elif weekday == 3 and expiry_days >= 2:  # Thursday, hold covers weekend
        mult *= 0.90
        factors.append("weekend exposure (Thu entry)")

    # ── Factor 4: Earnings proximity — gap can be catastrophic ──────────────
    # Already penalized confidence; also reduce sizing (independent risk dimension)
    if near_earnings:
        mult *= 0.70
        factors.append("earnings window")

    # ── Factor 5: Category — stocks gap more than indices/commodities ────────
    if category in _STOCK_CATEGORIES:
        mult *= 0.95

    # Apply floor: sizing can't drop below max_penalty of original
    mult = max(max_penalty, round(mult, 4))

    # Classify level
    if mult <= 0.70:
        level = "high"
    elif mult <= 0.87:
        level = "medium"
    else:
        level = "low"

    note = "; ".join(factors)
    return level, mult, note


def _suggested_risk_pct(
    confidence: int,
    risk_reward: Optional[float],
    trend_aligned: bool = False,
    sector_aligned: bool = False,
    gap_sizing_mult: float = 1.0,
    gap_max_penalty: float = 0.50,
) -> float:
    """
    Suggested risk per trade as % of capital (half-Kelly style when R:R known; else tier-based).

    Adjustments applied in order:
    1. Trend aligned: +20% size boost
    2. Sector aligned: +10% size boost
    3. Gap risk multiplier: reduces sizing for high overnight/weekend gap exposure

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

    # Alignment boosts
    multiplier = 1.0
    if trend_aligned:
        multiplier += 0.20
    if sector_aligned:
        multiplier += 0.10

    # Cap first (Kelly gives large raw values; cap represents portfolio discipline)
    capped_pct = min(2.5, max(0.1, base_pct * multiplier))

    # Gap risk reduction applied to the already-capped value so it always has effect
    gap_mult = max(gap_max_penalty, float(gap_sizing_mult))
    final_pct = capped_pct * gap_mult

    # Reapply floor only (cap already applied above)
    return round(max(0.1, final_pct), 2)


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
    # Earnings proximity flag (stocks only)
    near_earnings: bool = False
    days_to_earnings: Optional[int] = None
    # Gap risk: overnight/weekend gap exposure vs SL distance
    gap_risk_level: str = "low"   # low | medium | high
    gap_risk_note: str = ""       # human-readable factors that fired
    # Volume Profile snapshot (daily approximation, 60-day lookback)
    vpoc: Optional[float] = None
    vah: Optional[float] = None
    val: Optional[float] = None
    vp_bias: str = "at_vpoc"          # above_vpoc | below_vpoc | at_vpoc
    vp_overextension: Optional[float] = None
    in_value_area: bool = False        # True = choppy equilibrium zone
    # Relative Strength vs SPX and sector ETF (stocks only)
    rs_20d: Optional[float] = None    # RS ratio vs ^GSPC over 20 trading days
    rs_60d: Optional[float] = None    # RS ratio vs ^GSPC over 60 trading days
    sector_rs: Optional[float] = None # RS ratio vs sector ETF (XLK/XLE/XLF/…) over 20 days


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
    if category in _STOCK_CATEGORIES and asset_id in by_asset_sentiment:
        info = by_asset_sentiment[asset_id]
        avg = info.get("avg")
        cnt = info.get("count", 0)
        if avg is not None:
            if best_avg is None or avg > best_avg:
                best_avg = avg
            total_count += cnt
    return (best_avg, total_count) if best_avg is not None else None


def get_sentiment_velocity_and_conviction(
    category: str,
    asset_id: str,
    by_asset_sentiment: dict[str, dict],
) -> tuple[Optional[float], Optional[float]]:
    """Return (velocity, conviction) for this asset from the latest aggregated sentiment.
    velocity  — rate of change: recent_avg - older_avg (positive = improving).
    conviction — consensus score 0-1 (1 = strong agreement, 0 = contradictory noise).
    Returns (None, None) when not enough data is available."""
    if not by_asset_sentiment:
        return None, None
    best_velocity: Optional[float] = None
    best_conviction: Optional[float] = None
    for tag, targets in SENTIMENT_TAG_TO_ASSETS.items():
        if (category, asset_id) in targets:
            info = by_asset_sentiment.get(tag)
            if info:
                v = info.get("velocity")
                c = info.get("conviction")
                if v is not None and (best_velocity is None or abs(v) > abs(best_velocity)):
                    best_velocity = v
                if c is not None and (best_conviction is None or c > best_conviction):
                    best_conviction = c
    if category in _STOCK_CATEGORIES and asset_id in by_asset_sentiment:
        info = by_asset_sentiment[asset_id]
        v = info.get("velocity")
        c = info.get("conviction")
        if v is not None and (best_velocity is None or abs(v) > abs(best_velocity)):
            best_velocity = v
        if c is not None and (best_conviction is None or c > best_conviction):
            best_conviction = c
    return best_velocity, best_conviction


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
    if category in _STOCK_CATEGORIES and "earnings" in active_set:
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
    if category in _STOCK_CATEGORIES and asset_id in by_asset_sentiment:
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
        cat = getattr(r, "asset_category", None) or "stocks_large"
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
            if category in (*_STOCK_CATEGORIES, "indices"):
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

def _stretch_levels_for_min_rr(
    price: float,
    stop_loss: float,
    take_profit: float,
    direction: str,
    min_rr: float,
) -> tuple[float, float]:
    """
    If R:R is below min_rr, move take_profit outward (keep stop) until ratio is met.
    Used for ad-hoc / trade-builder builds where S/R-aware SL/TP can otherwise
    produce unrealistically tight targets vs risk (common on commodities futures).
    """
    rr = calc_risk_reward(price, stop_loss, take_profit)
    if rr is not None and rr >= min_rr - 1e-6:
        return stop_loss, take_profit
    risk = abs(price - stop_loss)
    if risk <= 0:
        return stop_loss, take_profit
    # Small cushion so rounding to 2dp still clears min_rr (e.g. 1.06 vs 1.0625).
    need = min_rr * risk * 1.004
    d = (direction or "long").lower()
    if d == "short":
        cur_reward = abs(price - take_profit)
        if cur_reward >= need - 1e-9:
            return stop_loss, take_profit
        new_tp = price - need
        return stop_loss, round(new_tp, 2)
    cur_reward = abs(take_profit - price)
    if cur_reward >= need - 1e-9:
        return stop_loss, take_profit
    new_tp = price + need
    return stop_loss, round(new_tp, 2)


def _build_candidate(
    category: str,
    asset_id: str,
    symbol: str,
    risk_tier: str,
    by_asset_sentiment: Optional[dict],
    market_ctx: Optional[MarketContext] = None,
    active_events: Optional[list[str]] = None,
    min_rr_floor: float = MIN_RISK_REWARD,
    *,
    ad_hoc: bool = False,
    policy: Optional[AssetClassPolicy] = None,
) -> Optional[Candidate]:
    """Build a single Candidate with ATR-based levels, direction, expiry, score, trend alignment."""
    pol = policy or get_asset_class_policy(category, asset_id)
    snap = build_snapshot(symbol)
    if not snap or snap.price <= 0:
        _log_engine_skip(
            "no_snapshot",
            category=category,
            asset_id=asset_id,
            symbol=symbol,
            policy=pol.key,
            ad_hoc=ad_hoc,
        )
        return None

    # Stale snapshot: live fetch failed, using cached data from a previous run.
    # Apply a confidence penalty so the signal is visibly discounted.
    _stale_confidence_penalty = 8 if snap.is_stale else 0

    price = round(snap.price, 2)

    # Direction from sentiment + technicals (regime-dependent weights)
    pair = get_sentiment_score_and_count(category, asset_id, by_asset_sentiment or {})
    sentiment_avg = pair[0] if pair else None
    sentiment_count = pair[1] if pair else 0
    regime_id = market_ctx.regime.regime if market_ctx else None
    direction = determine_direction(
        sentiment_avg,
        snap,
        regime=regime_id,
        asset_direction_style=pol.direction_style,
    )

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

    # Hard-block signal generation in crisis and high_vol regimes.
    # Backtest: crisis=12.5% WR (-0.71R avg), high_vol=23.5% WR (-0.009R avg) — both net negative EV.
    # Uses extended_regime (5-label VIX-based) so live labels match backtest definitions.
    if market_ctx and market_ctx.regime.extended_regime in ("crisis", "high_vol"):
        return None

    # ADX trend-strength soft penalty: ADX < 20 = weak/no trend — momentum signals less reliable.
    # Soft penalty (-8) rather than hard gate preserves diversification while down-weighting choppy entries.
    # No penalty if ADX data unavailable (missing data ≠ no trend).
    _adx_confidence_penalty = 0
    if snap and snap.adx is not None:
        _adx_val, *_ = snap.adx
        if _adx_val < 20:
            _adx_confidence_penalty = 8
            logger.debug("[ENGINE] %s: ADX=%.1f < 20 (weak trend) → adx_penalty=-8", asset_id, _adx_val)

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
        
        # In risk_off, flip long → short for equity-like books when policy allows (excludes e.g. VIX).
        # Skip flip when SHORTS_ENABLED=False — flipping then blocking creates silent watchlist wipe.
        if (
            SHORTS_ENABLED
            and market_ctx.regime.regime == "risk_off"
            and direction == "long"
            and pol.apply_risk_off_long_flip
            and category in (*_STOCK_CATEGORIES, "indices")
        ):
            direction = "short"

    # Short trades globally disabled except in bear regime (SHORTS_BEAR_REGIME_ENABLED).
    # Bear-regime shorts require: market_regime == "bear", SHORTS_BEAR_REGIME_ENABLED=True,
    # AND confidence ≥ 50 (enforced below after full confidence calculation).
    # SHORTS_ENABLED=True overrides everything (global re-enable for future use).
    if direction == "short" and not SHORTS_ENABLED:
        _bear_regime_short_allowed = (
            SHORTS_BEAR_REGIME_ENABLED
            and market_ctx is not None
            and market_ctx.regime.regime == "bear"
        )
        if not _bear_regime_short_allowed:
            return None

    # EMA200 hard gate for short trades: do not short an asset that is above its 200-day EMA.
    # Structural market upward bias means shorting above EMA200 fights the macro trend —
    # backtest confirmed 20% win rate for shorts overall; EMA200 is the strongest available filter.
    if direction == "short" and snap and snap.ema200 is not None and snap.price > snap.ema200:
        return None

    # ── Load risk preference once — used by several gates below ─────────────────
    from app.core.preferences import get_preferred_risk as _get_preferred_risk
    _preferred_risk = _get_preferred_risk()

    # RSI exhaustion gate: block entries at extremes — chasing overbought longs and oversold shorts
    # leads to mean-reversion losses. Ceiling is regime + preference aware:
    #   base 65, +5 in risk_on regime, +5 for aggressive preference → max 75.
    #   risk_off hard-caps ceiling at 60 regardless of preference.
    _rsi_long_ceiling = 65
    if regime_id == "risk_on":
        _rsi_long_ceiling += 5
    if _preferred_risk == "aggressive":
        _rsi_long_ceiling += 5
    elif _preferred_risk == "conservative":
        _rsi_long_ceiling -= 2
    if regime_id == "risk_off":
        _rsi_long_ceiling = min(_rsi_long_ceiling, 60)
    _rsi_long_ceiling = max(58, min(75, _rsi_long_ceiling))
    _rsi_short_floor = 100 - _rsi_long_ceiling

    if snap and snap.rsi is not None:
        if direction == "long" and snap.rsi > _rsi_long_ceiling:
            return None
        if direction == "short" and snap.rsi < _rsi_short_floor:
            return None
        # RSI slope: soft penalty instead of hard gate (volatile markets punish hard skips)
        # Bonus for positive slope already handled in confidence scoring below.

    # MACD alignment penalty: bearish histogram on longs / bullish on shorts = momentum against trade.
    # Was a hard gate (4 skips/run) — converted to soft penalty; pooled in _new_filter_penalty below.
    _macd_alignment_penalty = 0
    if snap and snap.macd is not None:
        if direction == "long" and snap.macd_bearish:
            _macd_alignment_penalty = 8
        elif direction == "short" and snap.macd_bullish:
            _macd_alignment_penalty = 8

    # Volume confirmation: threshold is preference-aware.
    # Was a hard gate (18 skips/run, unreliable on non-equity) — soft penalty, equity-only; pooled below.
    _vol_floor = 0.80 if _preferred_risk == "conservative" else (0.65 if _preferred_risk == "aggressive" else 0.75)
    _volume_penalty = 0
    if snap and snap.volume_ratio is not None:
        if category in _STOCK_CATEGORIES and snap.volume_ratio < _vol_floor:
            _volume_penalty = 6

    # ATR-based stop loss / take profit
    sl, tp = compute_atr_levels(price, risk_tier, snapshot=snap, direction=direction)
    eff_min_rr = max(1.0, min_rr_floor * 0.85) if ad_hoc else min_rr_floor
    if ad_hoc:
        sl, tp = _stretch_levels_for_min_rr(price, sl, tp, direction, eff_min_rr)
    elif category == "commodities":
        rr_probe = calc_risk_reward(price, sl, tp)
        if rr_probe is not None and rr_probe < eff_min_rr:
            sl, tp = _stretch_levels_for_min_rr(price, sl, tp, direction, eff_min_rr)

    # Expiry estimation
    expiry_days = estimate_expiry_days(price, tp, snapshot=snap)
    expiry = date.today() + timedelta(days=expiry_days)

    # Ranking score
    score = compute_technical_score(sentiment_avg, sentiment_count, snap, direction=direction)

    # Sentiment velocity contribution: accelerating sentiment in trade direction boosts score
    vel, conv = get_sentiment_velocity_and_conviction(category, asset_id, by_asset_sentiment or {})
    if vel is not None:
        # Cap at ±0.3: velocity of 0.2 → +0.3 for longs, -0.3 for shorts
        vel_contribution = max(-0.3, min(0.3, vel * 1.5))
        score += vel_contribution if direction == "long" else -vel_contribution
        logger.debug("[ENGINE] %s: velocity=%.3f → score_adj=%.3f", asset_id, vel, vel_contribution)

    # Sentiment velocity gate: strongly reversing sentiment → hard skip.
    # Mild decline → store penalty; applied to confidence after it is computed below.
    vel_confidence_penalty = 0
    if vel is not None:
        vel_for_direction = vel if direction == "long" else -vel
        if vel_for_direction < -0.15:
            logger.debug("[ENGINE] %s: velocity=%.3f strongly against direction (%s) → skip", asset_id, vel, direction)
            return None
        elif vel_for_direction < -0.05:
            # -0.1 vel → -4 pts; -0.149 vel → -5 pts. Threshold -0.05 avoids penalising flat/noise.
            vel_confidence_penalty = int(abs(vel_for_direction) * 40)

    # Trend alignment bonus/penalty (scaled by asset class)
    if trend_aligned:
        score += pol.mtf_aligned_score_bonus
    elif alignment.alignment_score != 0:
        aligned_dir = "long" if alignment.alignment_score > 0 else "short"
        if aligned_dir != direction:
            score -= pol.mtf_misalign_score_penalty

    # Confidence score (0–100) with market context adjustment; regime-specific thresholds
    base_confidence = compute_confidence(
        sentiment_avg,
        sentiment_count,
        snap,
        direction,
        regime=regime_id,
        sentiment_signal_scale=pol.sentiment_signal_scale,
        indicator_conflict_multiplier=pol.indicator_conflict_multiplier,
    )
    confidence = base_confidence
    logger.debug(
        "[ENGINE] %s (%s): base_conf=%d, sent_avg=%s, sent_cnt=%d, dir=%s",
        asset_id, symbol, base_confidence, sentiment_avg, sentiment_count, direction,
    )
    # OBV distribution penalty (longs only): -5 pts when OBV slope is negative.
    # OBV already contributes +10 pts in compute_confidence when confirming direction;
    # explicit -5 for disagreement keeps total OBV swing at 15 pts — proportionate for one indicator.
    _obv_penalty = 0
    if direction == "long" and snap and snap.obv_confirming_down:
        _obv_penalty = 5

    # MACD histogram momentum decay penalty: histogram positive but slope negative = momentum fading.
    # Research shows decelerating entries fail ~60% of the time even when MACD is still technically bullish.
    # Only applies to longs where we rely on upward momentum continuation.
    _macd_decay_penalty = 0
    if direction == "long" and snap and snap.macd is not None and snap.macd_hist_slope is not None:
        if snap.macd[2] > 0 and snap.macd_hist_slope < 0:
            _macd_decay_penalty = 5

    # RSI slope penalty: fading momentum — pooled below. Bonus (+4) stays outside the cap.
    _rsi_slope_penalty = 0
    if snap and snap.rsi is not None and snap.rsi_slope is not None:
        if direction == "long":
            if snap.rsi_slope < -4.0:
                _rsi_slope_penalty = 8
            elif snap.rsi_slope < -2.0:
                _rsi_slope_penalty = 4
        elif direction == "short":
            if snap.rsi_slope > 2.0:
                _rsi_slope_penalty = 4

    # Cap-tier penalty — Step 6 is the confirmed sweet spot (Sharpe 2.486, +0.119R, 244R DD).
    # All variations tested (Step 6b/6c/6d) regressed vs Step 6. Mid -4 / small -6 is final.
    # "other" left ungated — hard gate caused perverse selection (3.7% WR remaining).
    _cap_tier_penalty = 0
    if category == "stocks_mid":
        _cap_tier_penalty = 4
    elif category == "stocks_small":
        _cap_tier_penalty = 6

    # Combined soft-penalty cap: all confidence-reducing signals pooled so no combination of weak
    # signals can stack into an invisible hard gate. Cap raised 20→25 with MACD+volume added.
    _new_filter_penalty = min(
        _adx_confidence_penalty + _obv_penalty + vel_confidence_penalty + _macd_decay_penalty
        + _rsi_slope_penalty + _macd_alignment_penalty + _volume_penalty + _cap_tier_penalty,
        25,
    )
    if _new_filter_penalty > 0:
        confidence = max(0, confidence - _new_filter_penalty)
        _nfp_parts = []
        if _adx_confidence_penalty > 0:
            _nfp_parts.append(f"adx_weak_trend=-{_adx_confidence_penalty}")
        if _obv_penalty > 0:
            _nfp_parts.append(f"obv_distribution=-{_obv_penalty}")
        if vel_confidence_penalty > 0:
            _nfp_parts.append(f"sentiment_fading=-{vel_confidence_penalty}")
        if _macd_decay_penalty > 0:
            _nfp_parts.append(f"macd_momentum_decay=-{_macd_decay_penalty}")
        if _rsi_slope_penalty > 0:
            _nfp_parts.append(f"rsi_slope=-{_rsi_slope_penalty}")
        if _macd_alignment_penalty > 0:
            _nfp_parts.append(f"macd_alignment=-{_macd_alignment_penalty}")
        if _volume_penalty > 0:
            _nfp_parts.append(f"volume_low=-{_volume_penalty}")
        if _cap_tier_penalty > 0:
            _nfp_parts.append(f"cap_tier=-{_cap_tier_penalty}")
        logger.debug(
            "[ENGINE] %s: new_filter_penalty=%d (%s) → conf=%d",
            asset_id, _new_filter_penalty, ", ".join(_nfp_parts), confidence,
        )

    # ── Bonus signals: items 3-5 — reward high-conviction setups ─────────────────────
    _bonus_parts: list[str] = []

    # Item 3 — MACD + RSI dual alignment bonus (+6 pts):
    # When MACD histogram > 0 (bullish momentum) AND RSI is in the 45-65 sweet spot (not overbought),
    # both indicators explicitly agree — research shows 73-84% WR for this combined setup.
    if direction == "long" and snap and snap.macd is not None and snap.rsi is not None:
        if snap.macd[2] > 0 and 45 <= snap.rsi <= 65:
            confidence = min(100, confidence + 6)
            _bonus_parts.append("macd_rsi_aligned=+6")

    # Item 4 — Volume surge bonus (+5 pts):
    # volume_ratio > 1.5 = genuine buying pressure (not just above the gate floor).
    # Research on breakout studies: volume >1.5× 20-day avg with price trending = 68-72% WR.
    if snap and snap.volume_ratio is not None and snap.volume_ratio > 1.5:
        confidence = min(100, confidence + 5)
        _bonus_parts.append(f"volume_surge=+5 ({snap.volume_ratio:.2f}x)")

    # Item 5 — RSI directional momentum bonus (+4 pts):
    # Positive slope + non-overbought = momentum continuation. Penalties pooled in _new_filter_penalty.
    if snap and snap.rsi is not None and snap.rsi_slope is not None:
        if direction == "long" and snap.rsi_slope > 0 and snap.rsi < 65:
            confidence = min(100, confidence + 4)
            _bonus_parts.append(f"rsi_momentum=+4 (slope={snap.rsi_slope:+.1f})")

    # Item 5b — Triple-confluence bonus (+5 pts): MACD histogram > 0 + RSI > 50 + volume above average.
    if direction == "long" and snap and snap.macd is not None and snap.rsi is not None and snap.volume_ratio is not None:
        if snap.macd[2] > 0 and snap.rsi > 50 and snap.volume_ratio > 1.0:
            confidence = min(100, confidence + 5)
            _bonus_parts.append("triple_confluence=+5")

    # Item 5c — ATR percentile regime: high-vol (>75th pct) penalizes longs; low-vol (<25th pct) rewards.
    if snap and snap.atr_percentile is not None:
        if snap.atr_percentile > 0.75:
            confidence = max(0, confidence - 3)
            _bonus_parts.append(f"atr_regime=-3 (high_vol pct={snap.atr_percentile:.2f})")
        elif snap.atr_percentile < 0.25:
            confidence = min(100, confidence + 3)
            _bonus_parts.append(f"atr_regime=+3 (low_vol pct={snap.atr_percentile:.2f})")

    # Turn-of-month bonus (+2 pts): first 3 / last 2 calendar days — institutional inflows / window dressing.
    _today = date.today()
    _last_dom = calendar.monthrange(_today.year, _today.month)[1]
    if _today.day <= 3 or _today.day >= _last_dom - 1:
        confidence = min(100, confidence + 2)
        _bonus_parts.append("turn_of_month=+2")

    # Item 6 — 52-week high proximity bonus (+8 pts):
    # Research: stocks within 5% of 52-week high with volume >1.5× show 72% WR over ~28 days.
    # Within 3% = very strong momentum signal. Gate: volume_ratio > 1.0 to avoid thin breakouts.
    if direction == "long" and snap and snap.pct_from_52w_high is not None:
        vol_ok = snap.volume_ratio is None or snap.volume_ratio >= 1.0
        if snap.pct_from_52w_high <= 0.03 and vol_ok:
            confidence = min(100, confidence + 8)
            _bonus_parts.append(f"near_52w_high=+8 ({snap.pct_from_52w_high*100:.1f}% below)")
        elif snap.pct_from_52w_high <= 0.08 and vol_ok:
            confidence = min(100, confidence + 4)
            _bonus_parts.append(f"near_52w_high=+4 ({snap.pct_from_52w_high*100:.1f}% below)")

    # Item 7 — EMA200 distance sweet spot (+3 pts / -3 pts):
    # Price 2-10% above EMA200 = momentum trend intact. <1% = borderline (fragile), >15% = overextended.
    # EMA200 already gates direction; this adds nuance within the passing zone.
    if direction == "long" and snap and snap.ema200 is not None and snap.ema200 > 0:
        ema_dist = (snap.price - snap.ema200) / snap.ema200
        if 0.02 <= ema_dist <= 0.10:
            confidence = min(100, confidence + 3)
            _bonus_parts.append(f"ema200_sweet_spot=+3 ({ema_dist*100:.1f}% above)")
        elif ema_dist > 0.15:
            confidence = max(0, confidence - 3)
            _bonus_parts.append(f"ema200_overextended=-3 ({ema_dist*100:.1f}% above)")

    if _bonus_parts:
        logger.debug("[ENGINE] %s: signal_bonuses %s → conf=%d", asset_id, ", ".join(_bonus_parts), confidence)

    # Order Flow Imbalance adjustment: OFI reflects institutional buying/selling pressure.
    # Accumulation (OFI > 0.6) boosts confidence for longs; distribution (OFI < 0.4) penalizes.
    # Strong distribution (OFI < 0.35) on a long → hard skip (sellers clearly in control).
    if snap and snap.ofi_ratio is not None:
        ofi = snap.ofi_ratio
        if direction == "long":
            if ofi < 0.35:
                logger.debug("[ENGINE] %s: OFI=%.2f strong distribution → skip long", asset_id, ofi)
                return None
            elif ofi < 0.4:
                confidence = max(0, confidence - 5)
            elif ofi > 0.6:
                confidence = min(100, confidence + 3)
        elif direction == "short":
            if ofi > 0.65:
                logger.debug("[ENGINE] %s: OFI=%.2f strong accumulation → skip short", asset_id, ofi)
                return None
            elif ofi > 0.6:
                confidence = max(0, confidence - 5)
            elif ofi < 0.4:
                confidence = min(100, confidence + 3)
    if trend_aligned:
        confidence = min(100, confidence + pol.mtf_aligned_confidence_bonus)
    if market_ctx:
        ra = market_ctx.regime_adjustment()
        da = market_ctx.divergence_adjustment()
        scaled = int(
            round(
                ra * pol.equity_regime_adjustment_scale
                + da * pol.cross_asset_divergence_scale
            )
        )
        confidence = max(0, min(100, confidence + scaled))
        if scaled != 0:
            logger.debug(
                "[ENGINE] %s: scaled regime/div conf adj=%d (raw ra=%d da=%d, policy=%s) → %d",
                asset_id,
                scaled,
                ra,
                da,
                pol.key,
                confidence,
            )
        
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
        
        # Cross-asset correlation divergence penalty (scaled by asset class)
        if market_ctx.has_divergence_warning:
            div_raw = _correlation_divergence_penalty(market_ctx, category, asset_id)
            div_penalty = int(round(div_raw * pol.correlation_divergence_penalty_scale))
            if div_penalty > 0:
                confidence = max(0, confidence - div_penalty)
                score -= div_penalty / 20
                logger.debug(
                    "[ENGINE] %s: correlation div penalty=%d (raw=%d, policy=%s) → conf=%d",
                    asset_id,
                    div_penalty,
                    div_raw,
                    pol.key,
                    confidence,
                )
                div_pairs = [cs.pair_name for cs in market_ctx.correlation_signals if cs.diverging]
                div_note = f"Correlation divergence ({', '.join(div_pairs)})"
                if regime_note:
                    regime_note = f"{regime_note}; {div_note}"
                else:
                    regime_note = div_note

        # USD / DXY macro nudge (FX + selected commodities; no per–asset-class engine split)
        dxy_delta = market_ctx.dxy_confidence_delta(category, asset_id, direction)
        if dxy_delta != 0:
            confidence = max(0, min(100, confidence + dxy_delta))
            logger.debug(
                "[ENGINE] %s: dxy adj=%d (trend=%s) → conf=%d",
                asset_id,
                dxy_delta,
                market_ctx.dxy.trend,
                confidence,
            )
            dxy_note = market_ctx.dxy_note_for_asset(category, asset_id, direction)
            if dxy_note:
                regime_note = f"{regime_note}; {dxy_note}" if regime_note else dxy_note

    # Stocks 60-69 vol-regime penalty: backtest confirms contracting/expanding vol in this
    # confidence band produces catastrophic results (12.9% and 27.3% win rates, ~-0.68 pnl_r).
    # Stable vol 60-69 stocks are excellent (+1.29 pnl_r) and must not be penalised.
    # Penalty reduced from -15 → -10 → -7: stacking with downstream penalties was killing healthy candidates.
    if category in _STOCK_CATEGORIES and 60 <= confidence < 70 and snap is not None:
        if snap.volatility_regime in ("contracting", "expanding"):
            confidence = max(0, confidence - 7)
            logger.debug(
                "[ENGINE] %s: stocks 60-69 vol-regime penalty (regime=%s) → conf=%d",
                asset_id, snap.volatility_regime, confidence,
            )

    # 50-59 stable vol penalty: stocks and indices both negative/near-zero pnl_r in quiet drift markets.
    # Reduced from -15 → -10 → -7 for same stacking reason.
    if category in (*_STOCK_CATEGORIES, "indices") and 50 <= confidence < 60 and snap is not None:
        if snap.volatility_regime == "stable":
            confidence = max(0, confidence - 7)
            logger.debug(
                "[ENGINE] %s: 50-59 stable-vol penalty (category=%s) → conf=%d",
                asset_id, category, confidence,
            )

    # Sentiment–price divergence: reduce confidence when sentiment and price move in opposite directions.
    # Reduced from -10 → -6 → -4: informative but shouldn't override a solid technical picture.
    if _has_sentiment_divergence(category, asset_id, by_asset_sentiment):
        confidence = max(0, confidence - 4)
        logger.debug("[ENGINE] %s: sentiment–price divergence → conf=%d", asset_id, confidence)
        if regime_note:
            regime_note = f"{regime_note}; sentiment–price divergence"
        else:
            regime_note = "Sentiment–price divergence"

    # Sentiment conviction: reward consensus, penalize contradictory signals.
    # Low conviction penalty reduced from -8 → -5 → -3.
    if conv is not None:
        if conv >= 0.7:
            confidence = min(100, confidence + 5)
            logger.debug("[ENGINE] %s: high conviction (%.2f) → conf=%d", asset_id, conv, confidence)
        elif conv < 0.3:
            confidence = max(0, confidence - 3)
            logger.debug("[ENGINE] %s: low conviction (%.2f) → conf=%d", asset_id, conv, confidence)

    # Event-type penalty: reduce confidence when asset is in an active high-impact event (Fed, earnings, etc.)
    event_pen = _event_penalty(category, asset_id, active_events, by_asset_sentiment)
    if event_pen > 0:
        confidence = max(0, confidence - event_pen)
        logger.debug("[ENGINE] %s: event penalty=%d → conf=%d", asset_id, event_pen, confidence)
        if regime_note:
            regime_note = f"{regime_note}; high-impact event"
        else:
            regime_note = "High-impact event"

    # Earnings proximity penalty: reduce confidence for stocks reporting within ±window_days
    near_earnings = False
    days_to_earnings: Optional[int] = None
    if category in _STOCK_CATEGORIES:
        from app.core.preferences import get_earnings_calendar_config
        from app.services.earnings_calendar import is_near_earnings as _is_near_earnings
        earnings_cfg = get_earnings_calendar_config()
        if earnings_cfg["earnings_window_days"] > 0:
            near_earnings, days_to_earnings = _is_near_earnings(symbol, earnings_cfg["earnings_window_days"])
            if near_earnings:
                # Scale penalty with risk preference: aggressive accepts more earnings risk.
                _base_pen = earnings_cfg["earnings_confidence_penalty"]
                pen = int(_base_pen * (0.6 if _preferred_risk == "aggressive" else (0.8 if _preferred_risk == "moderate" else 1.0)))
                confidence = max(0, confidence - pen)
                logger.debug("[ENGINE] %s: earnings penalty=%d (days=%s) → conf=%d", asset_id, pen, days_to_earnings, confidence)
                suffix = f"earnings ±{days_to_earnings}d" if days_to_earnings is not None else "earnings window"
                if regime_note:
                    regime_note = f"{regime_note}; {suffix}"
                else:
                    regime_note = suffix
                # Aggressive preference: apply penalty but don't hard-skip.
                # Earnings volatility can also be a catalyst (gap up on beat).
                # Conservative/moderate: respect the earnings_skip_enabled preference.
                if earnings_cfg["earnings_skip_enabled"] and _preferred_risk != "aggressive":
                    _log_engine_skip(
                        "earnings_proximity",
                        category=category,
                        asset_id=asset_id,
                        symbol=symbol,
                        policy=pol.key,
                        days_to_earnings=days_to_earnings,
                    )
                    return None

    # ── Volume Profile confidence adjustments ────────────────────────────────
    # Priority order: VP > swing S/R for signal quality assessment.
    # Daily-bar VP is an approximation — used as a filter/modifier, not hard gate.
    if snap and snap.vpoc is not None:
        vp_notes: list[str] = []

        # 1. Price inside value area → choppy equilibrium zone → reduce confidence
        # Reduced from -8 → -5 → -3: informative signal but shouldn't dominate.
        if snap.in_value_area():
            confidence = max(0, confidence - 3)
            vp_notes.append("in value area (choppy)")
            logger.debug("[ENGINE] %s: VP: price in value area → -3 conf=%d", asset_id, confidence)

        # 2. Price near a Low Volume Node → fast directional move expected → boost
        lvn_nearby = (
            (snap.nearest_lvn_above() is not None and abs(snap.nearest_lvn_above() - price) / price < 0.012)
            or (snap.nearest_lvn_below() is not None and abs(snap.nearest_lvn_below() - price) / price < 0.012)
        )
        if lvn_nearby:
            confidence = min(100, confidence + 5)
            vp_notes.append("near LVN (fast move zone)")
            logger.debug("[ENGINE] %s: VP: near LVN → +5 conf=%d", asset_id, confidence)

        # 3. Overextension from VPOC → mean reversion risk
        # Reduced from -10/-5 → -7/-4 → -5/-3.
        ext = abs(snap.vp_overextension) if snap.vp_overextension is not None else 0.0
        if ext > 2.0:
            confidence = max(0, confidence - 5)
            vp_notes.append(f"overextended {ext:.1f}x VA from VPOC")
            logger.debug("[ENGINE] %s: VP: overextension %.1f → -5 conf=%d", asset_id, ext, confidence)
        elif ext > 1.3:
            confidence = max(0, confidence - 3)
            vp_notes.append(f"extended {ext:.1f}x VA from VPOC")
            logger.debug("[ENGINE] %s: VP: overextension %.1f → -3 conf=%d", asset_id, ext, confidence)

        # 4. Direction aligns with VP bias → confirm
        if (direction == "long" and snap.vp_bias == "below_vpoc") or \
           (direction == "short" and snap.vp_bias == "above_vpoc"):
            # Price below VPOC + long = moving toward fair value = VP-confirmed
            confidence = min(100, confidence + 4)
            vp_notes.append(f"VP-confirmed ({snap.vp_bias})")
            logger.debug("[ENGINE] %s: VP: bias confirms direction → +4 conf=%d", asset_id, confidence)

        if vp_notes and regime_note:
            regime_note = f"{regime_note}; VP: {', '.join(vp_notes)}"
        elif vp_notes:
            regime_note = f"VP: {', '.join(vp_notes)}"

    if (
        pol.news_boost_min_mentions > 0
        and sentiment_count >= pol.news_boost_min_mentions
        and sentiment_avg is not None
        and abs(sentiment_avg) >= pol.news_boost_min_abs_sentiment
    ):
        confidence = min(100, confidence + pol.news_boost_points)

    # Skip candidate when key indicators strongly conflict with direction (e.g. RSI OB + long, MACD bear + long)
    conflict_penalty = get_indicator_conflict_penalty(
        snap,
        direction,
        regime=regime_id,
        penalty_scale=pol.indicator_conflict_multiplier,
    )
    logger.debug("[ENGINE] %s: conflict_penalty=%d", asset_id, conflict_penalty)
    conflict_cap = 999 if ad_hoc else pol.conflict_skip_threshold
    if conflict_penalty >= conflict_cap:
        _log_engine_skip(
            "indicator_conflict",
            category=category,
            asset_id=asset_id,
            symbol=symbol,
            policy=pol.key,
            direction=direction,
            conflict_penalty=conflict_penalty,
            conflict_cap=conflict_cap,
            ad_hoc=ad_hoc,
        )
        return None

    # Bear-regime short confidence floor: only high-conviction short signals are allowed.
    # Raised to 50 (vs ~35-40 for longs) to compensate for historically lower short WR.
    if direction == "short" and market_ctx and market_ctx.regime.regime == "bear":
        _bear_short_min_conf = BEAR_REGIME_SHORT_MIN_CONFIDENCE
        if confidence < _bear_short_min_conf:
            _log_engine_skip(
                "short_low_confidence_bear",
                category=category,
                asset_id=asset_id,
                symbol=symbol,
                policy=pol.key,
                confidence=confidence,
                min_confidence=_bear_short_min_conf,
            )
            return None

    # Risk:Reward ratio
    rr = calc_risk_reward(price, sl, tp)

    # Skip candidates with poor R:R (reward doesn't justify risk)
    if rr is not None and rr < eff_min_rr:
        _log_engine_skip(
            "risk_reward",
            category=category,
            asset_id=asset_id,
            symbol=symbol,
            policy=pol.key,
            direction=direction,
            risk_reward=round(rr, 4),
            min_risk_reward=eff_min_rr,
            ad_hoc=ad_hoc,
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
        suffix = f"indicator conflict penalty: {conflict_penalty} (skip if ≥{pol.conflict_skip_threshold})"
        vol_notes = f"{vol_notes}; {suffix}" if vol_notes else suffix

    # ── Relative Strength vs SPX and sector ETF (stocks only) ───────────────
    rs_20d: Optional[float] = None
    rs_60d: Optional[float] = None
    sector_rs: Optional[float] = None

    if category in _STOCK_CATEGORIES:
        symbol_for_rs = get_yf_symbol(category, asset_id) or asset_id.upper()
        try:
            rs_20d = compute_relative_strength(symbol_for_rs, "^GSPC", period=20)
            rs_60d = compute_relative_strength(symbol_for_rs, "^GSPC", period=60)
            sector_etf = get_sector_etf_for_asset(asset_id)
            if sector_etf:
                sector_rs = compute_relative_strength(symbol_for_rs, sector_etf, period=20)
        except Exception:
            pass

        if rs_20d is not None:
            # Direction-aware: strong RS confirms long, denies short — and vice versa
            if rs_20d > 1.2:
                rs_adj = +5 if direction == "long" else -5
                rs_note = f"RS strong ({rs_20d:.2f}× SPX 20d)"
            elif rs_20d < 0.8:
                rs_adj = -8 if direction == "long" else +5
                rs_note = f"RS weak ({rs_20d:.2f}× SPX 20d)"
            else:
                rs_adj = 0
                rs_note = None
            if rs_adj != 0:
                confidence = max(0, min(100, confidence + rs_adj))
                logger.debug("[ENGINE] %s: RS adj=%d (rs_20d=%.2f, dir=%s) → conf=%d",
                             asset_id, rs_adj, rs_20d, direction, confidence)
                if rs_note and regime_note:
                    regime_note = f"{regime_note}; {rs_note}"
                elif rs_note:
                    regime_note = rs_note

    # Determine if sector is aligned with trade direction
    sector_aligned = False
    if sector_regime:
        if sector_regime.regime == "risk_on" and direction == "long":
            sector_aligned = True
        elif sector_regime.regime == "risk_off" and direction == "short":
            sector_aligned = True

    # Extend sector_aligned with per-stock RS vs sector ETF
    if sector_aligned and sector_rs is not None and sector_rs < 0.9:
        # Sector is bullish but this stock is weak relative to its peers — not truly aligned
        sector_aligned = False
        logger.debug("[ENGINE] %s: sector_aligned revoked — sector_rs=%.2f < 0.9", asset_id, sector_rs)
    elif not sector_aligned and sector_rs is not None and sector_rs > 1.15:
        # Stock is outperforming its sector even if sector regime is neutral — grant alignment
        sector_aligned = True
        logger.debug("[ENGINE] %s: sector_aligned granted — sector_rs=%.2f > 1.15", asset_id, sector_rs)
    
    # Gap risk: compute from overnight gap distribution + holding period + timing
    from app.core.preferences import get_gap_risk_config
    gap_cfg = get_gap_risk_config()
    gap_risk_level = "low"
    gap_risk_note = ""
    gap_sizing_mult = 1.0
    if gap_cfg["gap_risk_enabled"]:
        gap_risk_level, gap_sizing_mult, gap_risk_note = _compute_gap_risk(
            snap=snap,
            expiry_days=expiry_days,
            near_earnings=near_earnings,
            category=category,
            today=date.today(),
            max_penalty=gap_cfg["gap_risk_max_penalty"],
        )
        if gap_risk_level != "low":
            logger.debug(
                "[ENGINE] %s: gap_risk=%s mult=%.2f note=%s",
                asset_id, gap_risk_level, gap_sizing_mult, gap_risk_note,
            )
            suffix = f"gap risk {gap_risk_level}"
            if gap_risk_note:
                suffix = f"gap risk {gap_risk_level} ({gap_risk_note})"
            if regime_note:
                regime_note = f"{regime_note}; {suffix}"
            else:
                regime_note = suffix

    suggested_risk = _suggested_risk_pct(
        confidence, rr,
        trend_aligned=trend_aligned,
        sector_aligned=sector_aligned,
        gap_sizing_mult=gap_sizing_mult,
        gap_max_penalty=gap_cfg["gap_risk_max_penalty"],
    )
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
        near_earnings=near_earnings,
        days_to_earnings=days_to_earnings,
        gap_risk_level=gap_risk_level,
        gap_risk_note=gap_risk_note,
        vpoc=snap.vpoc if snap else None,
        vah=snap.vah if snap else None,
        val=snap.val if snap else None,
        vp_bias=snap.vp_bias if snap else "at_vpoc",
        vp_overextension=snap.vp_overextension if snap else None,
        in_value_area=snap.in_value_area() if snap else False,
        rs_20d=rs_20d,
        rs_60d=rs_60d,
        sector_rs=sector_rs,
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
        if c != category:
            continue
        if asset_id in seen:
            continue
        if (category, asset_id) in excluded:
            _log_engine_skip(
                "exclude_taken",
                category=category,
                asset_id=asset_id,
                phase="ideas",
            )
            continue
        seen.add(asset_id)
        resolved = _resolve_symbol_risk(category, asset_id, universe)
        if not resolved:
            continue
        symbol, risk_tier = resolved
        if RISK_TIER_ORDER.get(risk_tier, 99) > max_risk_level:
            _log_engine_skip(
                "risk_tier_above_preference",
                category=category,
                asset_id=asset_id,
                symbol=symbol,
                risk_tier=risk_tier,
                max_preferred_tier_order=max_risk_level,
                phase="ideas",
            )
            continue
        pol = get_asset_class_policy(category, asset_id)
        min_rr = _min_rr_floor_for_build(category, pol)
        cand = _build_candidate(
            category,
            asset_id,
            symbol,
            risk_tier,
            by_asset_sentiment,
            market_ctx,
            active_events,
            min_rr_floor=min_rr,
            policy=pol,
        )
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

    regime_base_min = market_ctx.min_confidence_threshold() if market_ctx else 35
    logger.info(
        "[ENGINE][%s] regime_base_min_confidence=%d; per-asset gate adds policy offset (+ commodity prefs)",
        category,
        regime_base_min,
    )

    max_risk_level = RISK_TIER_ORDER.get(preferred_risk, RISK_TIER_ORDER["moderate"])
    universe = _universe_lookup()
    excluded = exclude_taken or set()

    idea_pairs = sorted(_trade_ideas_from_analysis(by_asset_sentiment))
    idea_candidates = _candidates_for_pairs(
        category,
        idea_pairs,
        universe,
        max_risk_level,
        by_asset_sentiment=by_asset_sentiment,
        exclude_taken=excluded,
        market_ctx=market_ctx,
        active_events=active_events,
    )

    # Confidence gate: drop low-conviction candidates (threshold varies by asset class)
    # Short trades require an additional +8 pts above the normal gate — backtest shows
    # shorts win only 25% of the time vs 43% for longs; structural market upward bias
    # means we need much stronger conviction before shorting.
    _SHORT_CONFIDENCE_PREMIUM = 8
    _gated_ideas: list[Candidate] = []
    for c in idea_candidates:
        eff_min = _effective_min_confidence(category, c.asset_id, market_ctx)
        if c.direction == "short":
            eff_min += _SHORT_CONFIDENCE_PREMIUM
        if c.confidence_score >= eff_min:
            _gated_ideas.append(c)
        else:
            _log_engine_skip(
                "confidence_gate_idea",
                category=category,
                asset_id=c.asset_id,
                policy=get_asset_class_policy(category, c.asset_id).key,
                confidence=c.confidence_score,
                min_confidence=eff_min,
            )
    idea_candidates = _gated_ideas

    # Rank: confidence-weighted score. Trend-aligned candidates get priority.
    def _rank_key(c: Candidate) -> tuple:
        tie = hash((tie_seed or "", c.asset_id)) % 10000
        aligned_bonus = 1 if c.trend_aligned else 0
        return (-aligned_bonus, -c._score, -c.confidence_score, tie)

    idea_candidates.sort(key=_rank_key)
    chosen_ids = {c.asset_id for c in idea_candidates[:effective_max]}
    result = idea_candidates[:effective_max]

    logger.info("[ENGINE][%s] idea_candidates after per-asset gate: %d", category, len(idea_candidates))
    # Backfill from universe — use a lower confidence floor since these are
    # curated tradeable assets where technicals alone carry enough signal.
    if len(result) < effective_max:
        items = ASSETS_BY_CATEGORY.get(category, [])
        rest: list[Candidate] = []
        for asset_id, symbol, risk_tier in items:
            if asset_id in chosen_ids:
                _log_engine_skip(
                    "skip_backfill_already_in_ideas",
                    category=category,
                    asset_id=asset_id,
                    symbol=symbol,
                )
                continue
            if (category, asset_id) in excluded:
                _log_engine_skip(
                    "exclude_taken",
                    category=category,
                    asset_id=asset_id,
                    symbol=symbol,
                    phase="backfill",
                )
                continue
            if RISK_TIER_ORDER.get(risk_tier, 99) > max_risk_level:
                _log_engine_skip(
                    "risk_tier_above_preference",
                    category=category,
                    asset_id=asset_id,
                    symbol=symbol,
                    risk_tier=risk_tier,
                    max_preferred_tier_order=max_risk_level,
                    phase="backfill",
                )
                continue
            pol = get_asset_class_policy(category, asset_id)
            min_rr = _min_rr_floor_for_build(category, pol)
            cand = _build_candidate(
                category,
                asset_id,
                symbol,
                risk_tier,
                by_asset_sentiment,
                market_ctx,
                active_events,
                min_rr_floor=min_rr,
                policy=pol,
            )
            eff_min = _effective_min_confidence(category, asset_id, market_ctx)
            backfill_min_conf = max(ABSOLUTE_MIN_CONFIDENCE, eff_min - 15)
            if not cand:
                # Failure reason already logged inside _build_candidate (no_snapshot / conflict / R:R).
                continue
            if cand.confidence_score < backfill_min_conf:
                _log_engine_skip(
                    "confidence_gate_backfill",
                    category=category,
                    asset_id=asset_id,
                    symbol=symbol,
                    policy=pol.key,
                    confidence=cand.confidence_score,
                    min_confidence=backfill_min_conf,
                )
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
    closed_trades: Optional[list] = None,
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
    pipeline_run_id = str(uuid4())
    logger.info(
        "[ENGINE] Batch run_id=%s: up to %d per category × %d categories (regime=%s)",
        pipeline_run_id,
        cap,
        len(BATCH_CATEGORIES),
        market_ctx.regime.regime,
    )
    _excl = exclude_taken or set()
    if _excl:
        sample = ", ".join(f"{a}:{b}" for a, b in sorted(_excl)[:15])
        more = f" (+{len(_excl) - 15} more)" if len(_excl) > 15 else ""
        logger.info(
            "[ENGINE] exclude_taken: %d pair(s) will not be re-recommended (status=taken). Sample: %s%s",
            len(_excl),
            sample,
            more,
        )

    # Circuit breaker: check risk mode before running the batch
    from app.core.preferences import get_circuit_breaker_config
    from app.services.circuit_breaker import CircuitBreakerState, compute_state as _cb_compute
    cb_state = CircuitBreakerState()  # default: normal
    cb_cfg = get_circuit_breaker_config()
    if cb_cfg["circuit_breaker_enabled"] and closed_trades is not None:
        cb_state = _cb_compute(
            closed_trades,
            max_consecutive_losses=cb_cfg["max_consecutive_losses"],
            max_daily_loss_r=cb_cfg["max_daily_loss_r"],
            drawdown_gate_r=cb_cfg["drawdown_gate_r"],
        )
        if cb_state.is_paused:
            logger.warning("[CIRCUIT_BREAKER] mode=paused — skipping batch generation")
            return []
        if cb_state.is_defensive:
            logger.info(
                "[CIRCUIT_BREAKER] mode=defensive — confidence penalty +%d, sizing ×%.1f",
                cb_state.confidence_penalty(),
                cb_state.sizing_multiplier(),
            )

    out: list[Candidate] = []
    batch_ctx: dict[str, Any] = {
        "batch_date": date.today().isoformat(),
        "pipeline_run_id": pipeline_run_id,
        "skips": {},
        "skips_by_cat": {},
    }
    _tok_batch_log = _ENGINE_BATCH_LOG.set(batch_ctx)
    try:
        seen_asset_key: set[tuple[str, str]] = set()
        total_risk_pct = 0.0

        def _try_add(c: Candidate) -> bool:
            nonlocal total_risk_pct
            key = (c.asset_category, c.asset_id)
            if key in seen_asset_key:
                _log_engine_skip(
                    "batch_duplicate_asset",
                    category=c.asset_category,
                    asset_id=c.asset_id,
                    note="same category+asset already added this batch",
                )
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
                    _log_engine_skip(
                        "batch_correlation",
                        category=c.asset_category,
                        asset_id=c.asset_id,
                        symbol=symbol_new,
                        policy=get_asset_class_policy(c.asset_category, c.asset_id).key,
                        max_correlation=round(max_corr, 4),
                        correlation_threshold=CORRELATION_THRESHOLD,
                    )
                    return False
            candidate_risk = c.suggested_risk_pct or 0.0
            if total_risk_pct + candidate_risk > risk_budget_1r:
                _log_engine_skip(
                    "batch_risk_budget",
                    category=c.asset_category,
                    asset_id=c.asset_id,
                    symbol=symbol_new,
                    policy=get_asset_class_policy(c.asset_category, c.asset_id).key,
                    suggested_risk_pct=round(candidate_risk, 4),
                    risk_used_pct=round(total_risk_pct, 4),
                    risk_budget_pct=risk_budget_1r,
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
                MACRO_DAY_CONFIDENCE_PENALTY,
                len(out),
            )

        # Circuit breaker defensive mode: penalize confidence + shrink sizing
        if cb_state.is_defensive:
            pen = cb_state.confidence_penalty()
            mul = cb_state.sizing_multiplier()
            for c in out:
                c.confidence_score = max(0, (c.confidence_score or 0) - pen)
                c.suggested_risk_pct = round((c.suggested_risk_pct or 0.0) * mul, 2)
            logger.info(
                "[CIRCUIT_BREAKER] defensive: applied conf penalty %d + sizing ×%.1f to %d candidates",
                pen, mul, len(out),
            )

        # Re-gate after post-hoc penalties (macro day, circuit breaker).
        # Penalties applied above can push candidates below the regime min threshold —
        # drop any that no longer meet it rather than saving low-confidence noise.
        regime_min = market_ctx.min_confidence_threshold()
        pre_regate = len(out)
        out = [c for c in out if (c.confidence_score or 0) >= regime_min]
        if len(out) < pre_regate:
            dropped = pre_regate - len(out)
            logger.info(
                "[ENGINE] Post-penalty re-gate (min=%d): dropped %d candidate(s) below threshold",
                regime_min, dropped,
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
    finally:
        _ENGINE_BATCH_LOG.reset(_tok_batch_log)
        digest = {
            "event": "engine_batch_digest",
            "pipeline_run_id": batch_ctx.get("pipeline_run_id"),
            "batch_date": batch_ctx["batch_date"],
            "regime": market_ctx.regime.regime,
            "regime_min_confidence": market_ctx.min_confidence_threshold(),
            "candidates_kept": len(out),
            "skip_totals": dict(sorted(batch_ctx["skips"].items(), key=lambda x: x[0])),
            "skip_by_category": {
                cat: dict(sorted(reasons.items(), key=lambda x: x[0]))
                for cat, reasons in sorted(batch_ctx["skips_by_cat"].items())
            },
        }
        logger.info("[ENGINE_DIGEST] %s", json.dumps(digest, default=str, separators=(",", ":")))

    return out


def build_trade_builder_candidate(
    asset_id: str,
    category: str,
    *,
    by_asset_sentiment: Optional[dict] = None,
    preferred_risk: str = "moderate",
    active_events: Optional[list[str]] = None,
) -> tuple[Optional[Candidate], Optional[str]]:
    """
    Build a single Candidate for the trade-builder "start from asset" flow.
    Uses slightly relaxed conflict / R:R gates (ad_hoc) so users still get a plan when batch filters would drop the row.
    Returns (candidate, error_message); on success error_message is None.
    """
    cat = (category or "").strip().lower()
    if cat not in BATCH_CATEGORIES:
        return None, f"category must be one of: {', '.join(BATCH_CATEGORIES)}"

    aid = (asset_id or "").strip()
    if not aid:
        return None, "asset_id is required"
    if cat in _STOCK_CATEGORIES:
        aid = aid.upper()

    clear_cache()
    market_ctx = build_market_context(include_sectors=True)
    universe = _universe_lookup()
    resolved = _resolve_universe_entry(cat, aid, universe)
    if not resolved:
        return None, (
            f"Could not resolve '{aid}' for category '{cat}'. "
            "For stocks use a valid US ticker. For commodities/indices/other, pick a symbol from the bot universe "
            "(see the Analysis page asset lists), or try Stocks + your equity ticker."
        )

    symbol, risk_tier, canonical_aid = resolved
    max_risk_level = RISK_TIER_ORDER.get(preferred_risk, RISK_TIER_ORDER["moderate"])
    if RISK_TIER_ORDER.get(risk_tier, 99) > max_risk_level:
        return None, (
            f"This asset's risk tier ({risk_tier}) is above your preferred risk setting ({preferred_risk}). "
            "Lower preferred risk on the Dashboard or choose another symbol."
        )

    pol = get_asset_class_policy(cat, canonical_aid)
    min_rr_floor = _min_rr_floor_for_build(cat, pol)

    cand = _build_candidate(
        cat,
        canonical_aid,
        symbol,
        risk_tier,
        by_asset_sentiment,
        market_ctx,
        active_events,
        min_rr_floor=min_rr_floor,
        ad_hoc=True,
        policy=pol,
    )
    if not cand:
        return None, (
            "Could not build a plan: no usable Yahoo chart data, indicator conflict too high, "
            "or risk/reward still below your minimum after level adjustment. "
            "The Analysis page only tags headlines with names like Brent — the builder needs live OHLCV for the mapped symbol (Brent → BZ=F). "
            "Try again, another symbol/category, lower commodity min R:R on the Dashboard, or refresh data from the Dashboard."
        )
    return cand, None


# For external access to the latest market context
_last_market_context: Optional[MarketContext] = None


def get_last_market_context() -> Optional[MarketContext]:
    return _last_market_context
