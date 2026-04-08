"""
Live Trading Math Analyzer.

Generates trading signals using the SAME quantitative math as the main
recommendations engine — build_snapshot, determine_direction, compute_atr_levels,
compute_confidence, calc_risk_reward — not LLM.

Universe:
  Base: 35 hardcoded assets (stocks/commodities/fx/crypto/etfs/indices)
  +Dynamic: tickers discovered from LiveNewsItem asset_tags (≥2 mentions/24h)

LLM role: receives math signals, returns trade_type + rationale + llm_sl + llm_tp.
"""
from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Base universe ─────────────────────────────────────────────────────────────
# (asset_id, yf_symbol, risk_tier, live_category)

_BASE_UNIVERSE: list[tuple[str, str, str, str]] = [
    # Stocks — energy + mining (same as engine hardcoded)
    ("XOM",   "XOM",     "conservative", "stocks"),
    ("CVX",   "CVX",     "conservative", "stocks"),
    ("BP",    "BP",      "conservative", "stocks"),
    ("SHEL",  "SHEL",    "moderate",     "stocks"),
    ("NEM",   "NEM",     "moderate",     "stocks"),
    # Commodities
    ("WTI",       "CL=F",    "conservative", "commodities"),
    ("Brent",     "BZ=F",    "conservative", "commodities"),
    ("XAUUSD",    "GC=F",    "moderate",     "commodities"),
    ("Henry Hub", "NG=F",    "moderate",     "commodities"),
    ("Silver",    "SI=F",    "moderate",     "commodities"),
    # FX
    ("EURUSD", "EURUSD=X", "moderate", "fx"),
    ("GBPUSD", "GBPUSD=X", "moderate", "fx"),
    ("USDJPY", "USDJPY=X", "moderate", "fx"),
    ("AUDUSD", "AUDUSD=X", "moderate", "fx"),
    ("USDCAD", "USDCAD=X", "moderate", "fx"),
    # Crypto
    ("BTC", "BTC-USD",  "aggressive", "crypto"),
    ("ETH", "ETH-USD",  "aggressive", "crypto"),
    ("BNB", "BNB-USD",  "aggressive", "crypto"),
    ("SOL", "SOL-USD",  "aggressive", "crypto"),
    # ETFs
    ("SPY", "SPY", "conservative", "etfs"),
    ("QQQ", "QQQ", "moderate",     "etfs"),
    ("GLD", "GLD", "moderate",     "etfs"),
    ("TLT", "TLT", "moderate",     "etfs"),
    ("XLE", "XLE", "moderate",     "etfs"),
    ("XLF", "XLF", "moderate",     "etfs"),
    # Indices — US
    ("SPX",  "^GSPC",    "moderate",     "indices"),
    ("DJI",  "^DJI",     "moderate",     "indices"),
    ("NDX",  "^IXIC",    "moderate",     "indices"),
    ("RUT",  "^RUT",     "moderate",     "indices"),
    # Indices — Europe
    ("FTSE", "^FTSE",    "moderate",     "indices"),
    ("DAX",  "^GDAXI",   "moderate",     "indices"),
    ("CAC",  "^FCHI",    "moderate",     "indices"),
    # Indices — Asia
    ("Nikkei",  "^N225",   "moderate",  "indices"),
    ("HangSeng","^HSI",    "moderate",  "indices"),
    # Volatility
    ("VIX", "^VIX", "aggressive", "indices"),
]

# yf symbols already in base (fast lookup for dedup)
_BASE_YF_SYMBOLS: set[str] = {row[1] for row in _BASE_UNIVERSE}
_BASE_ASSET_IDS: set[str] = {row[0] for row in _BASE_UNIVERSE}

_MIN_MENTIONS_FOR_DISCOVERY = 2
_MAX_DISCOVERY_STOCKS       = 25

# direction style per category
_DIRECTION_STYLE: dict[str, str | None] = {
    "commodities": "macro_linked",
    "fx":          "macro_linked",
    "stocks":      None,
    "crypto":      None,
    "etfs":        None,
    "indices":     None,
}

_snap_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="live_snap")


# ── Sentiment aggregation from live news ──────────────────────────────────────

def _aggregate_live_sentiment_sync() -> dict[str, dict]:
    """
    Query LiveNewsItem for last 24h, parse asset_tags, aggregate sentiment per tag.
    Returns: {tag: {"avg": float, "count": int}}
    """
    try:
        from sqlalchemy import create_engine, text
        from app.core.config import settings
        db_url = str(settings.database_url)
        # Convert async URL to sync for blocking query
        db_url = db_url.replace("+asyncpg", "").replace("+aiosqlite", "")
        engine = create_engine(db_url)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT asset_tags, sentiment_score FROM live_news_items WHERE fetched_at >= :cutoff"),
                {"cutoff": cutoff},
            ).fetchall()
        engine.dispose()

        agg: dict[str, list[float]] = {}
        for asset_tags_json, sentiment in rows:
            if not asset_tags_json:
                continue
            try:
                tags = json.loads(asset_tags_json)
            except Exception:
                continue
            score = float(sentiment or 0.0)
            for tag in tags:
                if not tag:
                    continue
                tag = str(tag).strip()
                if tag not in agg:
                    agg[tag] = []
                agg[tag].append(score)

        return {
            tag: {"avg": sum(scores) / len(scores), "count": len(scores)}
            for tag, scores in agg.items()
        }
    except Exception as exc:
        logger.warning("[LIVE_ANALYZER] Could not aggregate sentiment: %s", exc)
        return {}


def _discover_tickers(sentiment_data: dict[str, dict]) -> list[tuple[str, str, str, str]]:
    """
    Find tickers from sentiment data that aren't in the base universe.
    Returns list of (asset_id, yf_symbol, risk_tier, "stocks") tuples.
    """
    try:
        from app.services.sentiment.ticker_detection import is_likely_ticker
    except Exception:
        return []

    candidates: list[tuple[str, int]] = []
    for tag, info in sentiment_data.items():
        if tag in _BASE_ASSET_IDS:
            continue
        if not is_likely_ticker(tag):
            continue
        if info.get("count", 0) < _MIN_MENTIONS_FOR_DISCOVERY:
            continue
        candidates.append((tag, info["count"]))

    candidates.sort(key=lambda x: -x[1])
    result = []
    for tag, _ in candidates[:_MAX_DISCOVERY_STOCKS]:
        result.append((tag, tag, "moderate", "stocks"))
    return result


def get_auto_universe() -> list[tuple[str, str, str, str]]:
    """
    Returns full live universe: base assets + sentiment-discovered stocks.
    Each entry: (asset_id, yf_symbol, risk_tier, live_category)
    """
    sentiment_data = _aggregate_live_sentiment_sync()
    discovered = _discover_tickers(sentiment_data)
    return list(_BASE_UNIVERSE) + discovered


def get_auto_watchlist_symbols() -> list[str]:
    """Quick helper: returns just the yf_symbols for price tracking."""
    return [row[1] for row in get_auto_universe()]


# ── Per-ticker signal computation (blocking, runs in thread) ──────────────────

def _compute_signal_sync(
    asset_id: str,
    yf_symbol: str,
    risk_tier: str,
    category: str,
    sentiment_avg: float,
    sentiment_count: int,
    regime: str,
) -> dict | None:
    """
    Blocking computation of a full math signal for one asset.
    Returns signal dict or None if insufficient data.
    """
    try:
        from app.services.technicals import (
            build_snapshot,
            compute_atr_levels,
            compute_confidence,
            compute_technical_score,
            determine_direction,
            calc_risk_reward,
        )
    except Exception as exc:
        logger.warning("[LIVE_ANALYZER] Could not import technicals: %s", exc)
        return None

    try:
        snap = build_snapshot(yf_symbol)
    except Exception as exc:
        logger.warning("[LIVE_ANALYZER] build_snapshot failed for %s: %s", yf_symbol, exc)
        return None

    if snap is None or not snap.price:
        return None

    direction_style = _DIRECTION_STYLE.get(category)

    try:
        direction = determine_direction(
            sentiment_avg=sentiment_avg if sentiment_count > 0 else None,
            snapshot=snap,
            regime=regime,
            asset_direction_style=direction_style,
        )
    except Exception:
        direction = "long"

    try:
        math_sl, math_tp = compute_atr_levels(
            price=snap.price,
            risk_tier=risk_tier,
            snapshot=snap,
            direction=direction,
        )
    except Exception:
        # Simple fallback: 2% SL, 3% TP
        mult = 1.0 if direction == "long" else -1.0
        math_sl = round(snap.price * (1 - 0.02 * mult), 4)
        math_tp = round(snap.price * (1 + 0.03 * mult), 4)

    try:
        confidence = compute_confidence(
            sentiment_avg=sentiment_avg if sentiment_count > 0 else None,
            sentiment_count=sentiment_count,
            snapshot=snap,
            direction=direction,
            regime=regime,
        )
    except Exception:
        confidence = 50

    try:
        rr = calc_risk_reward(snap.price, math_sl, math_tp)
    except Exception:
        rr = None

    try:
        tech_score = compute_technical_score(
            sentiment_avg=sentiment_avg if sentiment_count > 0 else None,
            sentiment_count=sentiment_count,
            snapshot=snap,
        )
    except Exception:
        tech_score = 0.0

    return {
        "asset_id":        asset_id,
        "ticker":          yf_symbol,
        "category":        category,
        "direction":       direction,
        "entry_price":     round(snap.price, 4),
        "math_sl":         round(math_sl, 4),
        "math_tp":         round(math_tp, 4),
        "confidence":      confidence,
        "risk_reward":     rr,
        "tech_score":      tech_score,
        "regime":          regime,
        "sentiment_avg":   round(sentiment_avg, 4) if sentiment_count > 0 else None,
        "sentiment_count": sentiment_count,
        # LLM will fill these in
        "llm_sl":          None,
        "llm_tp":          None,
        "trade_type":      None,
        "rationale":       None,
        "catalysts":       [],
    }


# ── Async orchestrator ────────────────────────────────────────────────────────

async def build_all_signals(min_confidence: int = 40) -> list[dict]:
    """
    Async: fetches universe, runs all signal computations in parallel threads,
    returns list of signal dicts with confidence >= min_confidence.
    """
    loop = asyncio.get_event_loop()

    # Get regime
    regime = "cautious"
    try:
        from app.services.market_context import detect_market_regime
        ctx = await loop.run_in_executor(_snap_executor, detect_market_regime)
        regime = ctx.regime if hasattr(ctx, "regime") else str(ctx)
    except Exception:
        pass

    # Get sentiment data and universe
    sentiment_data, universe = await asyncio.gather(
        loop.run_in_executor(_snap_executor, _aggregate_live_sentiment_sync),
        loop.run_in_executor(_snap_executor, get_auto_universe),
    )

    logger.info("[LIVE_ANALYZER] Universe: %d assets, regime=%s", len(universe), regime)

    # Build signals in parallel
    tasks = []
    for asset_id, yf_symbol, risk_tier, category in universe:
        tag_info = sentiment_data.get(asset_id) or sentiment_data.get(yf_symbol) or {}
        sentiment_avg   = float(tag_info.get("avg", 0.0))
        sentiment_count = int(tag_info.get("count", 0))

        tasks.append(loop.run_in_executor(
            _snap_executor,
            _compute_signal_sync,
            asset_id, yf_symbol, risk_tier, category,
            sentiment_avg, sentiment_count, regime,
        ))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    signals = []
    for r in results:
        if isinstance(r, Exception) or r is None:
            continue
        if (r.get("confidence") or 0) >= min_confidence:
            signals.append(r)

    # Sort by confidence desc within each category
    signals.sort(key=lambda x: -(x.get("confidence") or 0))
    logger.info("[LIVE_ANALYZER] %d signals above min_confidence=%d", len(signals), min_confidence)
    return signals
