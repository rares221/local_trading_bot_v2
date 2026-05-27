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
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Base universe ─────────────────────────────────────────────────────────────
# (asset_id, yf_symbol, risk_tier, live_category)

_BASE_UNIVERSE: list[tuple[str, str, str, str]] = [
    # Large cap stocks
    ("AAPL",  "AAPL",  "moderate",     "stocks_large"),
    ("MSFT",  "MSFT",  "moderate",     "stocks_large"),
    ("GOOGL", "GOOGL", "moderate",     "stocks_large"),
    ("NVDA",  "NVDA",  "moderate",     "stocks_large"),
    ("META",  "META",  "moderate",     "stocks_large"),
    ("AMZN",  "AMZN",  "moderate",     "stocks_large"),
    ("TSLA",  "TSLA",  "aggressive",   "stocks_large"),
    ("AVGO",  "AVGO",  "moderate",     "stocks_large"),
    ("JPM",   "JPM",   "moderate",     "stocks_large"),
    ("BAC",   "BAC",   "moderate",     "stocks_large"),
    ("GS",    "GS",    "moderate",     "stocks_large"),
    ("JNJ",   "JNJ",   "conservative", "stocks_large"),
    ("UNH",   "UNH",   "moderate",     "stocks_large"),
    ("XOM",   "XOM",   "conservative", "stocks_large"),
    ("CVX",   "CVX",   "conservative", "stocks_large"),
    ("BP",    "BP",    "conservative", "stocks_large"),
    ("SHEL",  "SHEL",  "moderate",     "stocks_large"),
    ("NEM",   "NEM",   "moderate",     "stocks_large"),
    ("PG",    "PG",    "conservative", "stocks_large"),
    ("WMT",   "WMT",   "conservative", "stocks_large"),
    ("V",     "V",     "conservative", "stocks_large"),
    # Mid cap stocks
    ("PLTR",  "PLTR",  "moderate",     "stocks_mid"),
    ("SMCI",  "SMCI",  "aggressive",   "stocks_mid"),
    ("IONQ",  "IONQ",  "aggressive",   "stocks_mid"),
    ("RIVN",  "RIVN",  "aggressive",   "stocks_mid"),
    ("ENPH",  "ENPH",  "moderate",     "stocks_mid"),
    ("FSLR",  "FSLR",  "moderate",     "stocks_mid"),
    ("CCJ",   "CCJ",   "moderate",     "stocks_mid"),
    ("MP",    "MP",    "aggressive",   "stocks_mid"),
    ("SOFI",  "SOFI",  "aggressive",   "stocks_mid"),
    ("HOOD",  "HOOD",  "aggressive",   "stocks_mid"),
    ("RIG",   "RIG",   "aggressive",   "stocks_mid"),
    ("HP",    "HP",    "moderate",     "stocks_mid"),
    ("HIMS",  "HIMS",  "aggressive",   "stocks_mid"),
    ("RKLB",  "RKLB",  "aggressive",   "stocks_mid"),
    # Small cap stocks
    ("BBAI",  "BBAI",  "aggressive",   "stocks_small"),
    ("SOUN",  "SOUN",  "aggressive",   "stocks_small"),
    ("LUNR",  "LUNR",  "aggressive",   "stocks_small"),
    ("ACHR",  "ACHR",  "aggressive",   "stocks_small"),
    ("ASTS",  "ASTS",  "aggressive",   "stocks_small"),
    ("JOBY",  "JOBY",  "aggressive",   "stocks_small"),
    ("ACMR",  "ACMR",  "aggressive",   "stocks_small"),
    ("ARRY",  "ARRY",  "aggressive",   "stocks_small"),
    ("RXRX",  "RXRX",  "aggressive",   "stocks_small"),
    ("OPEN",  "OPEN",  "aggressive",   "stocks_small"),
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
    "commodities":  "macro_linked",
    "fx":           "macro_linked",
    "stocks_large": None,
    "stocks_mid":   None,
    "stocks_small": None,
    "stocks":       None,  # backward compat for discovered tickers
    "crypto":       None,
    "etfs":         None,
    "indices":      None,
}

_snap_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="live_snap")


# ── Sentiment aggregation from live news ──────────────────────────────────────

def _aggregate_live_sentiment_sync() -> dict[str, dict]:
    """
    Query LiveNewsItem for last 24h, parse asset_tags, aggregate decay-weighted sentiment per tag.
    Uses the same 24h half-life exponential decay as the batch pipeline (aggregate.py)
    so that breaking news dominates over stale articles from earlier in the day.
    Returns: {tag: {"avg": float, "count": int}}
    """
    import math as _math
    try:
        from sqlalchemy import create_engine, text
        from app.core.config import settings
        db_url = str(settings.database_url)
        # Convert async URL to sync for blocking query
        db_url = db_url.replace("+asyncpg", "").replace("+aiosqlite", "")
        engine = create_engine(db_url)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        now = datetime.now(timezone.utc)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT asset_tags, sentiment_score, fetched_at "
                    "FROM live_news_items WHERE fetched_at >= :cutoff"
                ),
                {"cutoff": cutoff},
            ).fetchall()
        engine.dispose()

        _HALF_LIFE = 24.0  # hours — matches batch pipeline default
        agg: dict[str, list[tuple[float, float]]] = {}  # tag -> [(score, weight)]
        for asset_tags_json, sentiment, fetched_at in rows:
            if not asset_tags_json:
                continue
            tags = asset_tags_json if isinstance(asset_tags_json, list) else []
            score = float(sentiment or 0.0)
            if fetched_at is not None:
                fa = fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=timezone.utc)
                age_hours = max(0.0, (now - fa).total_seconds() / 3600)
            else:
                age_hours = 0.0
            weight = _math.exp(-0.693 * age_hours / _HALF_LIFE)
            for tag in tags:
                if not tag:
                    continue
                tag = str(tag).strip()
                agg.setdefault(tag, []).append((score, weight))

        # Split into recent (0-12h) and older (12-24h) for velocity
        agg_recent: dict[str, list[tuple[float, float]]] = {}
        agg_older: dict[str, list[tuple[float, float]]] = {}
        for asset_tags_json, sentiment, fetched_at in rows:
            if not asset_tags_json:
                continue
            tags = asset_tags_json if isinstance(asset_tags_json, list) else []
            score = float(sentiment or 0.0)
            if fetched_at is not None:
                fa = fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=timezone.utc)
                age_hours = max(0.0, (now - fa).total_seconds() / 3600)
            else:
                age_hours = 0.0
            weight = _math.exp(-0.693 * age_hours / _HALF_LIFE)
            for tag in tags:
                if not tag:
                    continue
                tag = str(tag).strip()
                if age_hours <= 12.0:
                    agg_recent.setdefault(tag, []).append((score, weight))
                else:
                    agg_older.setdefault(tag, []).append((score, weight))

        def _wavg(pairs: list[tuple[float, float]]) -> float | None:
            if not pairs:
                return None
            tw = sum(w for _, w in pairs)
            return sum(s * w for s, w in pairs) / tw if tw > 0 else None

        def _conv(pairs: list[tuple[float, float]]) -> float:
            if len(pairs) < 2:
                return 1.0
            scores = [s for s, _ in pairs]
            mean = sum(scores) / len(scores)
            variance = sum((s - mean) ** 2 for s in scores) / len(scores)
            return round(max(0.0, 1.0 - min(1.0, (variance ** 0.5) / 0.5)), 4)

        result: dict[str, dict] = {}
        for tag, pairs in agg.items():
            total_w = sum(w for _, w in pairs)
            if total_w > 0:
                weighted_avg = sum(s * w for s, w in pairs) / total_w
            else:
                weighted_avg = sum(s for s, _ in pairs) / len(pairs)
            recent_avg = _wavg(agg_recent.get(tag, []))
            older_avg = _wavg(agg_older.get(tag, []))
            velocity = round(recent_avg - older_avg, 4) if (recent_avg is not None and older_avg is not None) else None
            conviction = _conv(agg_recent.get(tag, pairs))  # prefer recent window; fall back to all
            result[tag] = {
                "avg": round(weighted_avg, 4),
                "count": len(pairs),
                "velocity": velocity,
                "conviction": conviction,
            }
        return result
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
        if abs(info.get("avg", 0.0)) <= 0.05:
            continue  # near-neutral: noise, not a directional signal
        candidates.append((tag, info["count"]))

    candidates.sort(key=lambda x: -x[1])
    result = []
    for tag, _ in candidates[:_MAX_DISCOVERY_STOCKS]:
        result.append((tag, tag, "moderate", "stocks_large"))
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
    backtest_stats: dict | None = None,
    sentiment_velocity: float | None = None,
    sentiment_conviction: float | None = None,
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
        from app.live_trading.live_confidence import compute_live_confidence
        indicator_score = compute_confidence(
            sentiment_avg=sentiment_avg if sentiment_count > 0 else None,
            sentiment_count=sentiment_count,
            snapshot=snap,
            direction=direction,
            regime=regime,
        )
        confidence = compute_live_confidence(
            backtest_stats=backtest_stats,
            indicator_score=indicator_score,
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
            direction=direction,
        )
    except Exception:
        tech_score = 0.0

    # Velocity contribution to ranking score (mirrors batch engine logic)
    if sentiment_velocity is not None:
        vel_contribution = max(-0.3, min(0.3, sentiment_velocity * 1.5))
        tech_score += vel_contribution if direction == "long" else -vel_contribution

    # Conviction contribution to confidence
    if sentiment_conviction is not None:
        if sentiment_conviction >= 0.7:
            confidence = min(100, confidence + 5)
        elif sentiment_conviction < 0.3:
            confidence = max(0, confidence - 8)

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

async def build_all_signals(min_confidence: int = 10) -> list[dict]:
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

    # Pre-fetch backtest win rates for all symbols (async, before threads)
    from app.live_trading.live_confidence import fetch_backtest_stats
    all_symbols = [yf_symbol for _, yf_symbol, _, _ in universe]
    backtest_stats_map = await fetch_backtest_stats(all_symbols)
    logger.info(
        "[LIVE_ANALYZER] Backtest stats available for %d/%d symbols",
        len(backtest_stats_map), len(all_symbols),
    )

    # Build signals in parallel
    import functools
    tasks = []
    for asset_id, yf_symbol, risk_tier, category in universe:
        tag_info = sentiment_data.get(asset_id) or sentiment_data.get(yf_symbol) or {}
        sentiment_avg       = float(tag_info.get("avg", 0.0))
        sentiment_count     = int(tag_info.get("count", 0))
        sentiment_velocity  = tag_info.get("velocity")
        sentiment_conviction = tag_info.get("conviction")
        stats = backtest_stats_map.get(yf_symbol)

        tasks.append(loop.run_in_executor(
            _snap_executor,
            functools.partial(
                _compute_signal_sync,
                asset_id, yf_symbol, risk_tier, category,
                sentiment_avg, sentiment_count, regime,
                stats,
                sentiment_velocity, sentiment_conviction,
            ),
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
