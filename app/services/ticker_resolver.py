"""
Three-layer ticker resolution for global markets:
  Layer 1: Hardcoded COMPANY_TO_TICKER (instant, ~100 entries)
  Layer 2: Persistent JSON cache (data/ticker_cache.json, grows over time)
  Layer 3: Live yfinance.Search fallback (any exchange Yahoo covers)

Negative cache prevents repeated failed lookups within a session.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exchange preference & constants
# ---------------------------------------------------------------------------

_EXCHANGE_PRIORITY_DEFAULT = ["NMS", "NYQ", "NGM", "PCX", "BTS", "ASE",  # US
                              "LSE", "GER", "PAR", "AMS", "MIL",          # Europe
                              "TYO", "HKG", "SHH", "SHZ", "KSC",         # Asia
                              "TSE", "SAO", "JSE", "NSI", "BSE"]          # Others

MAX_LIVE_LOOKUPS_PER_RUN = 30
NEGATIVE_CACHE_TTL_SECONDS = 86_400  # 24 hours
DEFAULT_CACHE_MAX_AGE_DAYS = 90

_lock = threading.Lock()

# In-memory negative cache: {lowercase_name: timestamp_of_failure}
_negative_cache: dict[str, float] = {}

# Per-run counter to limit Yahoo hits
_live_lookup_count = 0


def reset_run_counter() -> None:
    """Call at the start of each pipeline run."""
    global _live_lookup_count
    _live_lookup_count = 0


# ---------------------------------------------------------------------------
# Layer 2: Persistent JSON cache
# ---------------------------------------------------------------------------

def _cache_path() -> Path:
    from app.core.config import settings
    p = Path(settings.data_dir) / "ticker_cache.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_cache() -> dict[str, dict]:
    """Load {lowercase_name: {"symbol": ..., "exchange": ..., "short_name": ..., "resolved_at": ISO}} """
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(cache: dict[str, dict]) -> None:
    path = _cache_path()
    try:
        path.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to save ticker cache: %s", exc)


def _is_cache_entry_expired(entry: dict, max_age_days: int) -> bool:
    resolved_at = entry.get("resolved_at")
    if not resolved_at:
        return True
    try:
        dt = datetime.fromisoformat(resolved_at)
        age_days = (datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)).days
        return age_days > max_age_days
    except Exception:
        return True


def _get_max_age_days() -> int:
    try:
        from app.core.config import settings
        return getattr(settings, "ticker_cache_max_age_days", DEFAULT_CACHE_MAX_AGE_DAYS)
    except Exception:
        return DEFAULT_CACHE_MAX_AGE_DAYS


def _get_exchange_priority() -> list[str]:
    try:
        from app.core.config import settings
        raw = getattr(settings, "preferred_exchanges", "")
        if raw and raw.strip():
            return [e.strip() for e in raw.split(",") if e.strip()]
    except Exception:
        pass
    return _EXCHANGE_PRIORITY_DEFAULT


# ---------------------------------------------------------------------------
# Layer 3: Live yfinance.Search
# ---------------------------------------------------------------------------

def _search_live(query: str) -> Optional[dict]:
    """
    Layer 3 live symbol search — routed via market_data (Finnhub or yfinance).
    Returns {"symbol": str, "exchange": str, "short_name": str} or None.
    """
    global _live_lookup_count
    if _live_lookup_count >= MAX_LIVE_LOOKUPS_PER_RUN:
        return None
    _live_lookup_count += 1
    try:
        from app.services.market_data import search_symbol
        return search_symbol(query)
    except Exception as exc:
        logger.debug("search_symbol('%s') failed: %s", query, exc)
        return None


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------

def resolve_name(name: str) -> Optional[str]:
    """
    Resolve a company/asset name to a Yahoo Finance ticker symbol.
    Layer 1: COMPANY_TO_TICKER (hardcoded)
    Layer 2: data/ticker_cache.json (persistent, auto-growing)
    Layer 3: yfinance.Search (live, result cached)
    Returns symbol string or None.
    """
    if not name or not name.strip():
        return None

    key = name.strip().lower()
    if len(key) < 2:
        return None

    # Layer 1: hardcoded
    from app.services.sentiment.ticker_detection import COMPANY_TO_TICKER
    if key in COMPANY_TO_TICKER:
        return COMPANY_TO_TICKER[key]

    # Layer 2: persistent JSON cache
    with _lock:
        cache = _load_cache()

    max_age = _get_max_age_days()
    entry = cache.get(key)
    if entry and not _is_cache_entry_expired(entry, max_age):
        return entry.get("symbol")

    # Negative cache check (avoid re-hitting Yahoo for known misses)
    neg_ts = _negative_cache.get(key)
    if neg_ts and (time.time() - neg_ts) < NEGATIVE_CACHE_TTL_SECONDS:
        return None

    # Layer 3: live search (Finnhub or yfinance via market_data)
    result = _search_live(key)
    if result and result.get("symbol"):
        result["resolved_at"] = datetime.now(timezone.utc).isoformat()
        with _lock:
            cache = _load_cache()  # re-read to minimize race
            cache[key] = result
            _save_cache(cache)
        logger.info("Resolved '%s' -> %s (%s)", key, result["symbol"], result.get("exchange"))
        return result["symbol"]

    # Miss — add to negative cache
    _negative_cache[key] = time.time()
    return None


def resolve_name_batch(names: list[str]) -> dict[str, Optional[str]]:
    """Resolve multiple names. Returns {name: symbol_or_None}."""
    return {n: resolve_name(n) for n in names}


def get_cache_stats() -> dict:
    """Return cache stats for diagnostics."""
    cache = _load_cache()
    max_age = _get_max_age_days()
    expired = sum(1 for e in cache.values() if _is_cache_entry_expired(e, max_age))
    return {
        "total_entries": len(cache),
        "expired_entries": expired,
        "active_entries": len(cache) - expired,
        "negative_cache_size": len(_negative_cache),
        "live_lookups_this_run": _live_lookup_count,
        "max_live_lookups_per_run": MAX_LIVE_LOOKUPS_PER_RUN,
    }
