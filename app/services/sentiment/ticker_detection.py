"""
Dynamic ticker detection from feed text. Extracts likely stock/ETF symbols so we can
recommend any ticker that appears in the news, not just a fixed universe.
"""
import re
from typing import List

# All-caps 2–5 letter tokens are likely tickers; exclude common false positives.
# Includes: indices, trading/technical terms, media, currencies, and words that aren't tickers.
BLOCKLIST = frozenset({
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
    return s not in BLOCKLIST


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
    for m in TICKER_PATTERN.finditer(text):
        sym = m.group(1)
        if sym in BLOCKLIST or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)

    return out
