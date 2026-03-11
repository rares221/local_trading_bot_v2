"""
Dynamic ticker detection from feed text. Extracts likely stock/ETF symbols so we can
recommend any ticker that appears in the news, not just a fixed universe.
Blocklist: in-code defaults + optional data/blocklist.txt (one symbol per line; # = comment).
"""
import re
from pathlib import Path
from typing import List

# In-code defaults: all-caps 2–5 letter tokens to exclude from ticker detection.
# data/blocklist.txt can add more (one symbol per line; lines starting with # ignored).
_DEFAULT_BLOCKLIST = frozenset({
    "USA", "US", "UK", "EU", "ETF", "SEC", "CEO", "CFO", "IPO", "FDA", "OPEC", "EIA",
    "API", "AI", "IT", "HR", "PR", "TV", "PC", "ID", "PM", "AM", "ET", "LP", "LLC",
    "NA", "NY", "LA", "DC", "HQ", "FY", "Q1", "Q2", "Q3", "Q4", "YTD", "R&D",
    "CNBC", "BBC", "NPR", "CNN", "FT", "WSJ", "USD", "EUR", "GBP", "JPY", "CAD",
    "LNG", "WTI", "Brent",
    # Indices / venues / macro
    "FTSE", "NYSE", "VIX", "SPX", "NDX", "DAX", "DJI", "DXY", "CPI", "XAU", "FOMC",
    # Trading / technical terms (often appear in headlines)
    "FVG", "ICT", "VWAP", "PIPS", "IFVG", "VASP",
    # Crypto / stablecoins (yfinance symbol differs or no data)
    "USDT", "HBAR", "DOT",
    # Common words / acronyms / no-data symbols (add more as logs appear)
    "IRA", "JONES", "LIKE", "LL", "LOVES", "NGM", "PCE", "PDL", "PGIM", "PIXEL",
    "SHARE", "SL", "SOL", "IEA", "CAPO", "CIO", "CISD", "DSCW", "GATE", "VALID", "WAIT",
})


def get_blocklist() -> frozenset[str]:
    """Blocklist = in-code defaults + symbols from data/blocklist.txt (if present)."""
    from app.core.config import settings
    out = set(_DEFAULT_BLOCKLIST)
    path = Path(settings.data_dir) / "blocklist.txt"
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                s = line.split("#", 1)[0].strip().upper()
                if s and 2 <= len(s) <= 5 and s.isalpha():
                    out.add(s)
        except Exception:
            pass
    return frozenset(out)


# Optional: company name -> ticker for when headline says "Tesla" not "TSLA"
COMPANY_TO_TICKER: dict[str, str] = {
    "tesla": "TSLA",
    "apple": "AAPL",
    "microsoft": "MSFT",
    "amazon": "AMZN",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "meta": "META",
    "facebook": "META",
    "nvidia": "NVDA",
    "netflix": "NFLX",
    "jpmorgan": "JPM",
    "jpm": "JPM",
    "berkshire": "BRK.B",
    "exxon": "XOM",
    "chevron": "CVX",
    "shell": "SHEL",
    "bp": "BP",
    "newmont": "NEM",
    "barrick": "GOLD",
}

# 2–5 letter all-caps word (standalone)
TICKER_PATTERN = re.compile(r"\b([A-Z]{2,5})\b")


def is_likely_ticker(s: str) -> bool:
    """True if s looks like a ticker (2–5 all-caps letters, not in blocklist). Used by engine for dynamic ideas."""
    if not s or len(s) < 2 or len(s) > 5:
        return False
    if s != s.upper():
        return False
    if not s.isalpha():
        return False
    return s not in get_blocklist()


def resolve_company_to_ticker(query: str) -> list[dict]:
    """
    Resolve a company name (or partial) to ticker(s) using COMPANY_TO_TICKER.
    Returns list of {"name": str, "ticker": str} for matches (query substring in name or name in query).
    """
    if not query or not query.strip():
        return []
    q = query.strip().lower()
    out: list[dict] = []
    seen: set[str] = set()
    for name, ticker in COMPANY_TO_TICKER.items():
        if name in q or q in name:
            if ticker not in seen:
                seen.add(ticker)
                out.append({"name": name, "ticker": ticker})
    return out


def detect_tickers(text: str) -> List[str]:
    """
    Extract likely ticker symbols from text. Returns deduplicated list.
    - All-caps 2–5 letter tokens (excluding blocklist)
    - Company names mapped to tickers (e.g. Tesla -> TSLA)
    """
    if not text or not text.strip():
        return []
    seen: set[str] = set()
    out: list[str] = []

    # 1) Company name -> ticker (lowercase match)
    lower = text.lower()
    for name, ticker in COMPANY_TO_TICKER.items():
        if name in lower and ticker not in seen:
            seen.add(ticker)
            out.append(ticker)

    # 2) All-caps 2–5 letter tokens
    blocklist = get_blocklist()
    for m in TICKER_PATTERN.finditer(text):
        sym = m.group(1)
        if sym in blocklist or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)

    return out
