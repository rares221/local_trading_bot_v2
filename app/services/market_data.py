"""
Market data routing layer.

When FINNHUB_KEY is set in .env, eligible symbols (plain stock tickers — no special chars
like =, ^, -, :) are fetched via Finnhub instead of Yahoo Finance.

Non-eligible symbols (futures CL=F, indices ^GSPC, crypto BTC-USD, FX EURUSD=X, DXY)
always fall back to yfinance regardless of whether Finnhub is configured.
"""
from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from datetime import date, datetime, timedelta
from app.core.market_time import market_date
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ── yfinance rate-limit throttle ──────────────────────────────────────────────
# Yahoo Finance throttles by container IP when burst requests exceed ~1/s.
# The live_analyzer runs up to 10 threads in parallel — without a Lock, all
# threads read _yfinance_last_call simultaneously, see it's expired, and burst
# into Yahoo at the same time, causing timeouts.  The Lock serialises access so
# only one yfinance call proceeds at a time and the spacing is actually enforced.
_YFINANCE_MIN_INTERVAL = 0.8  # seconds between yfinance calls (was 0.6)
_yfinance_last_call: float = 0.0
_yfinance_lock = threading.Lock()          # prevents burst from multi-threaded callers
_YFINANCE_TIMEOUT = 15                     # seconds per call (was 10)
_YFINANCE_RETRIES = 1                      # retry once on timeout before giving up


# ── Eligibility ───────────────────────────────────────────────────────────────

def is_finnhub_eligible(symbol: str) -> bool:
    """
    Return True only for plain stock/ETF ticker symbols that Finnhub can serve.
    Rejects: futures (=F), indices (^), crypto (-USD), FX (=X), exchange-prefixed (BINANCE:).
    """
    return bool(symbol) and not any(c in symbol for c in ('=', '^', '-', ':'))


# ── Finnhub client (lazy singleton) ──────────────────────────────────────────

_finnhub_client = None
_finnhub_checked = False


def _get_client():
    """Return a finnhub.Client if FINNHUB_KEY is configured, else None."""
    global _finnhub_client, _finnhub_checked
    if _finnhub_checked:
        return _finnhub_client
    _finnhub_checked = True
    try:
        from app.core.config import settings
        key = (settings.finnhub_key or "").strip()
        if not key:
            return None
        import finnhub
        _finnhub_client = finnhub.Client(api_key=key)
        logger.info("[MARKET_DATA] Finnhub client initialized (user=%s)", settings.finnhub_user or "n/a")
    except Exception as exc:
        logger.warning("[MARKET_DATA] Could not init Finnhub client: %s", exc)
    return _finnhub_client


# ── Period / interval helpers ─────────────────────────────────────────────────

_PERIOD_DAYS: dict[str, int] = {
    "1d": 1, "5d": 5,
    "1mo": 30, "3mo": 90, "6mo": 180,
    "1y": 365, "2y": 730, "3y": 1095, "5y": 1825,
    "30d": 30, "60d": 60, "90d": 90,
    "130d": 130, "180d": 180, "250d": 250,
}

_INTERVAL_MAP: dict[str, str] = {
    "1m": "1", "5m": "5", "15m": "15", "30m": "30",
    "60m": "60", "1h": "60",
    "1d": "D", "1wk": "W", "1mo": "M",
}


def _period_to_timestamps(period: str) -> tuple[int, int]:
    """Convert yfinance-style period string to (from_ts, to_ts) Unix timestamps."""
    to_ts = int(time.time())
    days = _PERIOD_DAYS.get(period)
    if days is None:
        if period.endswith("d"):
            try:
                days = int(period[:-1])
            except ValueError:
                days = 365
        elif period.endswith("y"):
            try:
                days = int(period[:-1]) * 365
            except ValueError:
                days = 365
        else:
            days = 365
    return to_ts - days * 86400, to_ts


def _to_finnhub_resolution(interval: str) -> str:
    return _INTERVAL_MAP.get(interval, "D")


# ── OHLCV ─────────────────────────────────────────────────────────────────────

def fetch_ohlcv(
    symbol: str,
    period: str = "90d",
    interval: str = "1d",
) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV DataFrame for symbol.
    Uses Finnhub if configured and symbol is eligible, otherwise yfinance.
    Returns None on failure.
    """
    client = _get_client()
    if client and is_finnhub_eligible(symbol):
        df = _ohlcv_finnhub(client, symbol, period, interval)
        if df is not None and not df.empty:
            return df
    return _ohlcv_yfinance(symbol, period, interval)


def _ohlcv_finnhub(
    client,
    symbol: str,
    period: str,
    interval: str,
) -> Optional[pd.DataFrame]:
    try:
        from_ts, to_ts = _period_to_timestamps(period)
        resolution = _to_finnhub_resolution(interval)
        data = client.stock_candles(symbol, resolution, from_ts, to_ts)
        if not data or data.get("s") != "ok":
            return None
        closes = data.get("c") or []
        if not closes:
            return None
        df = pd.DataFrame({
            "Open":   data["o"],
            "High":   data["h"],
            "Low":    data["l"],
            "Close":  data["c"],
            "Volume": data["v"],
        }, index=pd.to_datetime(data["t"], unit="s", utc=True).tz_localize(None))
        df.index.name = "Datetime"
        return df
    except Exception as exc:
        logger.debug("[MARKET_DATA] Finnhub OHLCV %s/%s failed: %s", symbol, period, exc)
        return None


def _ohlcv_yfinance(symbol: str, period: str, interval: str) -> Optional[pd.DataFrame]:
    global _yfinance_last_call
    for attempt in range(_YFINANCE_RETRIES + 1):
        with _yfinance_lock:
            elapsed = time.monotonic() - _yfinance_last_call
            if elapsed < _YFINANCE_MIN_INTERVAL:
                time.sleep(_YFINANCE_MIN_INTERVAL - elapsed)
            _yfinance_last_call = time.monotonic()
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(ticker.history, period=period, interval=interval)
                try:
                    df = future.result(timeout=_YFINANCE_TIMEOUT)
                except concurrent.futures.TimeoutError:
                    if attempt < _YFINANCE_RETRIES:
                        logger.warning(
                            "[MARKET_DATA] yfinance timeout for %s (attempt %d/%d), retrying",
                            symbol, attempt + 1, _YFINANCE_RETRIES + 1,
                        )
                        time.sleep(1.5)
                        continue
                    logger.warning("[MARKET_DATA] yfinance timeout for %s (all attempts exhausted)", symbol)
                    return None
            if df is None or df.empty or "Close" not in df.columns:
                return None
            return df
        except Exception as exc:
            logger.debug("[MARKET_DATA] yfinance OHLCV %s failed: %s", symbol, exc)
            return None
    return None


# ── Live quote ────────────────────────────────────────────────────────────────

def fetch_quote(symbol: str) -> Optional[dict]:
    """
    Fetch live price. Returns {"price": float, "volume": int|None, "change_pct": float} or None.
    Uses Finnhub if configured and eligible, else yfinance fast_info.
    """
    client = _get_client()
    if client and is_finnhub_eligible(symbol):
        result = _quote_finnhub(client, symbol)
        if result:
            return result
    return _quote_yfinance(symbol)


def _quote_finnhub(client, symbol: str) -> Optional[dict]:
    try:
        q = client.quote(symbol)
        last = q.get("c")   # current price
        prev = q.get("pc")  # previous close
        if not last:
            return None
        return {
            "price": round(float(last), 4),
            "volume": None,  # /quote does not include volume
            "change_pct": round((float(last) - float(prev)) / float(prev) * 100, 4) if prev else 0.0,
        }
    except Exception as exc:
        logger.debug("[MARKET_DATA] Finnhub quote %s failed: %s", symbol, exc)
        return None


def _quote_yfinance(symbol: str) -> Optional[dict]:
    global _yfinance_last_call
    with _yfinance_lock:
        elapsed = time.monotonic() - _yfinance_last_call
        if elapsed < _YFINANCE_MIN_INTERVAL:
            time.sleep(_YFINANCE_MIN_INTERVAL - elapsed)
        _yfinance_last_call = time.monotonic()
    try:
        import yfinance as yf
        fi = yf.Ticker(symbol).fast_info
        last = float(getattr(fi, "last_price", None) or 0)
        prev = float(getattr(fi, "previous_close", None) or last or 1)
        volume = getattr(fi, "last_volume", None)
        if not last:
            return None
        return {
            "price": round(last, 4),
            "volume": int(volume) if volume else None,
            "change_pct": round((last - prev) / prev * 100, 4) if prev else 0.0,
        }
    except Exception:
        return None


def fetch_last_price(symbol: str) -> Optional[float]:
    """Convenience: return just the current price or None."""
    q = fetch_quote(symbol)
    return q["price"] if q else None


# ── Company profile ───────────────────────────────────────────────────────────

def fetch_company_profile(symbol: str) -> Optional[dict]:
    """
    Returns dict with: name, industry, sector (Yahoo only), market_cap, currency.
    Uses Finnhub if configured and eligible, else yfinance .info.
    """
    client = _get_client()
    if client and is_finnhub_eligible(symbol):
        result = _profile_finnhub(client, symbol)
        if result:
            return result
    return _profile_yfinance(symbol)


def _profile_finnhub(client, symbol: str) -> Optional[dict]:
    try:
        p = client.company_profile2(symbol=symbol)
        if not p or not p.get("name"):
            return None
        market_cap = p.get("marketCapitalization")
        return {
            "name": p["name"],
            "industry": p.get("finnhubIndustry"),
            "sector": None,  # Finnhub doesn't split sector/industry
            "market_cap": int(market_cap * 1_000_000) if market_cap else None,
            "currency": p.get("currency"),
        }
    except Exception as exc:
        logger.debug("[MARKET_DATA] Finnhub profile %s failed: %s", symbol, exc)
        return None


def _profile_yfinance(symbol: str) -> Optional[dict]:
    if not is_finnhub_eligible(symbol):
        return None  # futures/indices/crypto don't have yfinance fundamentals
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        if not isinstance(info, dict):
            return None
        name = info.get("shortName") or info.get("longName")
        if not name:
            return None
        return {
            "name": name,
            "industry": info.get("industry"),
            "sector": info.get("sector"),
            "market_cap": info.get("marketCap"),
            "currency": info.get("currency"),
        }
    except Exception:
        return None


# ── Stock metrics (P/E, 52-week range) ───────────────────────────────────────

def fetch_metrics(symbol: str) -> Optional[dict]:
    """
    Returns: {"pe_ratio": float, "high_52w": float, "low_52w": float} — any subset may be missing.
    Uses Finnhub /stock/metric if configured and eligible, else yfinance .info.
    """
    client = _get_client()
    if client and is_finnhub_eligible(symbol):
        result = _metrics_finnhub(client, symbol)
        if result:
            return result
    return _metrics_yfinance(symbol)


def _metrics_finnhub(client, symbol: str) -> Optional[dict]:
    try:
        m = client.company_basic_financials(symbol, "all")
        metric = (m or {}).get("metric") or {}
        result: dict = {}
        if metric.get("peBasicExclExtraTTM") is not None:
            result["pe_ratio"] = round(float(metric["peBasicExclExtraTTM"]), 1)
        if metric.get("52WeekHigh") is not None:
            result["high_52w"] = float(metric["52WeekHigh"])
        if metric.get("52WeekLow") is not None:
            result["low_52w"] = float(metric["52WeekLow"])
        return result if result else None
    except Exception as exc:
        logger.debug("[MARKET_DATA] Finnhub metrics %s failed: %s", symbol, exc)
        return None


def _metrics_yfinance(symbol: str) -> Optional[dict]:
    if not is_finnhub_eligible(symbol):
        return None  # futures/indices/crypto don't have yfinance fundamentals
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        if not isinstance(info, dict):
            return None
        result: dict = {}
        if info.get("trailingPE") is not None:
            result["pe_ratio"] = round(float(info["trailingPE"]), 1)
        if info.get("fiftyTwoWeekHigh") is not None:
            result["high_52w"] = float(info["fiftyTwoWeekHigh"])
        if info.get("fiftyTwoWeekLow") is not None:
            result["low_52w"] = float(info["fiftyTwoWeekLow"])
        return result if result else None
    except Exception:
        return None


# ── Earnings calendar ─────────────────────────────────────────────────────────

def fetch_next_earnings(symbol: str) -> Optional[date]:
    """
    Return the next upcoming earnings date for symbol, or None.
    Uses Finnhub if configured and eligible, else yfinance .calendar.
    """
    client = _get_client()
    if client and is_finnhub_eligible(symbol):
        result = _earnings_finnhub(client, symbol)
        if result is not None:
            return result
    return _earnings_yfinance(symbol)


def _earnings_finnhub(client, symbol: str) -> Optional[date]:
    try:
        today = market_date()
        to_date = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")
        from_date = today.strftime("%Y-%m-%d")
        data = client.earnings_calendar(
            _from=from_date, to=to_date, symbol=symbol, international=False
        )
        earnings_list = (data or {}).get("earningsCalendar") or []
        for item in earnings_list:
            try:
                d = date.fromisoformat(item["date"])
                if d >= today:
                    return d
            except Exception:
                continue
        return None
    except Exception as exc:
        logger.debug("[MARKET_DATA] Finnhub earnings %s failed: %s", symbol, exc)
        return None


def _earnings_yfinance(symbol: str) -> Optional[date]:
    try:
        import yfinance as yf
        today = market_date()
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar
        if cal is None:
            return None
        if isinstance(cal, dict):
            earnings_dates = cal.get("Earnings Date") or []
            if not isinstance(earnings_dates, (list, tuple)):
                earnings_dates = [earnings_dates]
            for e in earnings_dates:
                try:
                    d = e.date() if hasattr(e, "date") else date.fromisoformat(str(e)[:10])
                    if d >= today:
                        return d
                except Exception:
                    continue
        elif hasattr(cal, "columns"):
            for c in cal.columns:
                if "earnings" in str(c).lower():
                    for val in cal[c]:
                        try:
                            d = val.date() if hasattr(val, "date") else date.fromisoformat(str(val)[:10])
                            if d >= today:
                                return d
                        except Exception:
                            continue
        return None
    except Exception as exc:
        logger.debug("[MARKET_DATA] yfinance earnings %s failed: %s", symbol, exc)
        return None


# ── Symbol search ─────────────────────────────────────────────────────────────

def search_symbol(query: str) -> Optional[dict]:
    """
    Search for ticker by company name.
    Returns {"symbol": str, "exchange": str, "short_name": str} or None.
    Uses Finnhub /search if configured, else yfinance.Search.
    """
    client = _get_client()
    if client:
        result = _search_finnhub(client, query)
        if result:
            return result
    return _search_yfinance(query)


def _search_finnhub(client, query: str) -> Optional[dict]:
    try:
        data = client.symbol_search(query)
        results = (data or {}).get("result") or []
        for item in results:
            if item.get("type") in ("Common Stock", "ETP") and item.get("displaySymbol"):
                symbol = item.get("symbol") or item.get("displaySymbol")
                return {
                    "symbol": symbol,
                    "exchange": item.get("primaryExch", ""),
                    "short_name": item.get("description", ""),
                }
        return None
    except Exception as exc:
        logger.debug("[MARKET_DATA] Finnhub search '%s' failed: %s", query, exc)
        return None


def _search_yfinance(query: str) -> Optional[dict]:
    try:
        import yfinance as yf
        results = yf.Search(query, max_results=8)
        quotes = results.quotes if hasattr(results, "quotes") else []
        if not quotes:
            return None
        valid_types = {"EQUITY", "ETF", "FUTURE", "INDEX", "MUTUALFUND", "CRYPTOCURRENCY"}
        filtered = [q for q in quotes if q.get("quoteType", "EQUITY") in valid_types]
        best = (filtered or quotes)[0]
        symbol = best.get("symbol")
        if not symbol:
            return None
        return {
            "symbol": symbol,
            "exchange": best.get("exchange", ""),
            "short_name": best.get("shortname", best.get("longname", "")),
        }
    except Exception:
        return None
