"""
Dynamic ticker detection from feed text. Extracts likely stock/ETF symbols so we can
recommend any ticker that appears in the news, not just a fixed universe.

Resolution order:
  1. Hardcoded COMPANY_TO_TICKER (fast-path for ~100 top names)
  2. All-caps regex for explicit ticker mentions (AAPL, MSFT, etc.)
  3. Multi-word proper-noun extraction → ticker_resolver.resolve_name()
     (persistent JSON cache → yfinance.Search for global markets)

Blocklist: in-code defaults + optional data/blocklist.txt (one symbol per line; # = comment).
"""
import re
from pathlib import Path
from typing import List

# In-code defaults: all-caps 2–5 letter tokens to exclude from ticker detection.
_DEFAULT_BLOCKLIST = frozenset({
    "USA", "US", "UK", "EU", "ETF", "SEC", "CEO", "CFO", "IPO", "FDA", "OPEC", "EIA",
    "API", "AI", "IT", "HR", "PR", "TV", "PC", "ID", "PM", "AM", "ET", "LP", "LLC",
    "NA", "NY", "LA", "DC", "HQ", "FY", "Q1", "Q2", "Q3", "Q4", "YTD", "R&D",
    "CNBC", "BBC", "NPR", "CNN", "FT", "WSJ", "USD", "EUR", "GBP", "JPY", "CAD",
    "LNG", "WTI", "Brent",
    # Commodity terms — often news tags but clash with real tickers (e.g. GOLD→Barrick, OIL→various)
    "GOLD", "SILVER", "CRUDE", "BRENT", "NATGAS", "COPPER", "WHEAT", "CORN", "IRON", "COAL",
    "OPEX", "NGAS",
    "FTSE", "NYSE", "VIX", "SPX", "NDX", "DAX", "DJI", "DXY", "CPI", "XAU", "FOMC",
    # Geopolitical / org acronyms / news noise
    "IRGC", "ISI", "COVID", "STAT", "GMT", "TFA", "FXCM",
    # Company abbreviations / false positives
    "TSMC", "BOS", "CEIX", "OIL", "AACR", "USOIL", "MAHA", "BTIG",
    "ABC", "CATL", "IQVIA", "KRAS", "OCBC", "SCMP", "WATCH",
    "FVG", "ICT", "VWAP", "PIPS", "IFVG", "VASP",
    "USDT", "HBAR", "DOT",
    "IRA", "JONES", "LIKE", "LL", "LOVES", "NGM", "PCE", "PDL", "PGIM", "PIXEL",
    "SHARE", "SL", "SOL", "IEA", "CAPO", "CIO", "CISD", "DSCW", "GATE", "VALID", "WAIT",
    # Common headline / NLP false positives (avoid Yahoo "delisted" spam)
    "THE", "AND", "FOR", "NOT", "BUT", "OUR", "ARE", "WAS", "HAS", "HIS", "HER", "ITS",
    "YOU", "ALL", "CAN", "MAY", "WAY", "WHO", "HOW", "OUT", "USE", "ANY", "GOT", "GET",
    "SEE", "NEW", "NOW", "DAY", "ONE", "TWO", "BIG", "END", "FAR", "TOO", "YES", "BUY",
    "SELL", "LONG", "SHORT", "HIGH", "LOW", "TOP", "HIT", "CUT", "SET", "LOT", "RUN",
    "PUT", "BAD", "GOOD", "LAST", "NEXT", "FIRST", "JUST", "OVER", "INTO", "FROM", "THAT",
    "THIS", "WITH", "HAVE", "BEEN", "SOME", "THAN", "THEM", "ALSO", "ONLY", "VERY", "WHAT",
    "WHEN", "YOUR", "MORE", "MOST", "MANY", "SUCH", "MAKE", "LIKE", "TIME", "YEAR", "WEEK",
    "BACK", "MUCH", "THEN", "THEY", "EVEN", "WELL", "DOWN", "SAYS", "SAID", "CASE", "READ",
    "OPEN", "CLOSE", "FULL", "HALF", "LESS", "BEST", "WORST", "TRUE", "FALSE", "REAL",
    "FACT", "NEWS", "TOLD", "KNOW", "TAKE", "GAVE", "GIVE", "CAME", "COME", "WENT", "GOES",
    "DOES", "DID", "LET", "OWN", "OLD", "AGE", "AGO", "OFF", "ADD", "PER", "VIA",
    "THERE", "THESE", "THOSE", "WHERE", "WHILE", "AFTER", "SINCE", "UNDER", "ABOVE",
    "BELOW", "STOCK", "TRADE", "TRUMP", "BANK", "RATE", "BOND", "CASH", "DEAL", "PLAN",
    # Institutions / acronyms often tagged but not Yahoo tickers
    "ECB", "BOE", "PBOC", "IMF", "OECD", "NATO", "WTO", "WHO", "UN", "IRS", "FBI", "DOJ",
    "FED", "BOJ", "SNB", "RBA", "RBNZ",
    # Sports / leagues (headlines, not tickers)
    "NFL", "NHL", "NBA", "MLS", "FIFA", "UEFA", "NCAA", "NIL",
    # Macro / policy / gov tokens mistaken for symbols
    "NFP", "USMCA", "USDA", "USGS", "USTR", "NSE", "TASI", "SIFMA",
    # Macro economic indicators / reports (not Yahoo-resolvable tickers)
    "GDP", "GNP", "PPI", "PMI", "ISM", "NFIB", "LEI",
    # Elliott Wave labels (common in TA commentary, not tickers)
    "WXY",
    # Financial slang (not exchange-listed tickers)
    "HYPE", "FOMO",
    # Common headline words / jargon (all-caps RSS)
    "WORLD", "SPACE", "SPAC", "SPDR", "RISK", "SWING", "TRACE", "WILL", "WONT", "WOULD",
    "PLACE", "ZONES", "WPSR", "TBPN", "VAT", "RFK", "RTHK", "RWA", "SUV", "TMGH",
    "POC", "POI", "PRZ", "SEN", "SFO", "XIV",
    # Technical analysis indicator names (very common in financial news text)
    "OBV", "RSI", "MACD", "ATR", "EMA", "SMA", "ADX", "CCI", "ROC", "MFI", "ADL",
    "DMI", "PPO", "DPO", "CMO", "AROON", "STOCH", "TRIX", "KAMA", "DEMA", "TEMA",
    "PSAR", "VWMA", "CHOP", "ULTI", "BBB", "WMA",
    # Trade management abbreviations (TP/SL already common in news, SL was blocked but TP wasn't)
    "TP", "RR", "RRR", "PNL", "PRL", "ENTRY", "EXIT", "STOP", "LIMIT", "TRAIL",
    # Trading timeframe / jargon abbreviations (not tickers, appear in TA content)
    "HTF", "LTF", "MTF", "ATH", "ATL", "BIAS", "SETUP", "CHART", "DAILY", "WEEKLY",
    "SCALP", "SWING", "INTRA", "FRGNT",
    # Broker / exchange / regulatory acronyms (not Yahoo-resolvable tickers)
    "OANDA", "AMEX", "CFTC", "AFSL", "FINRA", "SIPC", "ESMA", "MIFID", "FSCA",
    # Financial metric acronyms (APY, APR etc. — not stock tickers)
    "APY", "APR", "NAV", "AUM", "IRR", "EBIT", "EBITDA", "EBITA", "CAPEX", "OPEX",
    # Currency codes missing from original list (caused $AUD/$FX spam)
    "AUD", "NZD", "CHF", "CNY", "CNH", "HKD", "SGD", "INR", "BRL", "RUB", "MXN",
    "ZAR", "KRW", "TRY", "SEK", "NOK", "DKK", "PLN", "HUF", "CZK", "ILS", "THB",
    "PHP", "IDR", "TWD", "SAR", "AED", "QAR", "KWD",
    # Region / exchange abbreviations that yfinance can't resolve
    "HK", "SS", "AT", "FX", "EM", "DM", "AP", "APAC", "EMEA", "LATAM",
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


# ---------------------------------------------------------------------------
# Layer 1: Hardcoded company name -> ticker (fast-path)
# ---------------------------------------------------------------------------

COMPANY_TO_TICKER: dict[str, str] = {
    # Tech mega-caps
    "tesla": "TSLA", "apple": "AAPL", "microsoft": "MSFT", "amazon": "AMZN",
    "google": "GOOGL", "alphabet": "GOOGL", "meta": "META", "facebook": "META",
    "nvidia": "NVDA", "netflix": "NFLX", "adobe": "ADBE", "salesforce": "CRM",
    "intel": "INTC", "amd": "AMD", "broadcom": "AVGO", "qualcomm": "QCOM",
    "oracle": "ORCL", "ibm": "IBM", "cisco": "CSCO", "palantir": "PLTR",
    "snowflake": "SNOW", "uber": "UBER", "airbnb": "ABNB", "spotify": "SPOT",
    "paypal": "PYPL", "shopify": "SHOP", "coinbase": "COIN",
    # Finance / banks (US)
    "jpmorgan": "JPM", "jp morgan": "JPM", "goldman sachs": "GS", "goldman": "GS",
    "morgan stanley": "MS", "bank of america": "BAC", "citigroup": "C", "citi": "C",
    "wells fargo": "WFC", "blackrock": "BLK", "charles schwab": "SCHW",
    "visa": "V", "mastercard": "MA", "american express": "AXP", "amex": "AXP",
    "berkshire": "BRK.B", "berkshire hathaway": "BRK.B",
    "royal bank": "RY", "td bank": "TD", "toronto-dominion": "TD",
    # Healthcare / pharma
    "johnson & johnson": "JNJ", "pfizer": "PFE", "moderna": "MRNA",
    "unitedhealth": "UNH", "eli lilly": "LLY", "novo nordisk": "NVO",
    "abbott": "ABT", "merck": "MRK", "abbvie": "ABBV",
    # Energy / oil
    "exxon": "XOM", "exxonmobil": "XOM", "chevron": "CVX",
    "shell": "SHEL", "royal dutch shell": "SHEL",
    "bp": "BP", "conocophillips": "COP", "conoco": "COP",
    "schlumberger": "SLB", "halliburton": "HAL",
    "marathon": "MPC", "valero": "VLO", "pioneer": "PXD",
    # Mining / metals
    "newmont": "NEM", "barrick": "GOLD", "barrick gold": "GOLD",
    "freeport": "FCX", "freeport-mcmoran": "FCX",
    "rio tinto": "RIO", "bhp": "BHP", "vale": "VALE",
    # Consumer / retail
    "walmart": "WMT", "costco": "COST", "target": "TGT", "home depot": "HD",
    "nike": "NKE", "starbucks": "SBUX", "mcdonald": "MCD", "mcdonalds": "MCD",
    "coca-cola": "KO", "coca cola": "KO", "pepsi": "PEP", "pepsico": "PEP",
    "procter & gamble": "PG", "procter": "PG", "disney": "DIS", "walt disney": "DIS",
    # Industrials / defense
    "boeing": "BA", "lockheed": "LMT", "lockheed martin": "LMT",
    "raytheon": "RTX", "caterpillar": "CAT", "deere": "DE", "john deere": "DE",
    "general electric": "GE", "honeywell": "HON", "3m": "MMM",
    # Autos
    "ford": "F", "general motors": "GM", "rivian": "RIVN", "lucid": "LCID",
    # Telecom / media
    "at&t": "T", "verizon": "VZ", "t-mobile": "TMUS", "comcast": "CMCSA",
    # ── European ─────────────────────────────────────────────────
    "nestle": "NESN.SW", "nestlé": "NESN.SW",
    "lvmh": "MC.PA", "louis vuitton": "MC.PA",
    "sap": "SAP.DE", "siemens": "SIE.DE", "basf": "BAS.DE", "bayer": "BAYN.DE",
    "asml": "ASML.AS", "unilever": "ULVR.L", "astrazeneca": "AZN.L",
    "hsbc": "HSBA.L", "barclays": "BARC.L", "deutsche bank": "DBK.DE",
    "volkswagen": "VOW3.DE", "bmw": "BMW.DE", "mercedes": "MBG.DE", "daimler": "MBG.DE",
    "total": "TTE.PA", "totalenergies": "TTE.PA",
    "roche": "ROG.SW", "novartis": "NOVN.SW",
    "banco santander": "SAN.MC", "santander": "SAN.MC",
    "bnp paribas": "BNP.PA", "bnp": "BNP.PA",
    "ing": "INGA.AS", "philips": "PHIA.AS",
    # ── Asian ────────────────────────────────────────────────────
    "toyota": "TM", "sony": "SONY", "honda": "HMC",
    "samsung": "005930.KS", "hyundai": "005380.KS",
    "alibaba": "BABA", "tencent": "0700.HK", "baidu": "BIDU",
    "jd.com": "JD", "xiaomi": "1810.HK", "byd": "1211.HK",
    "softbank": "9984.T", "mitsubishi": "8058.T", "hitachi": "6501.T",
    "taiwan semiconductor": "TSM", "tsmc": "TSM",
    "reliance": "RELIANCE.NS", "tata": "TCS.NS", "infosys": "INFY",
    # Commodities (name mentions in headlines)
    "crude oil": "CL=F", "brent crude": "BZ=F", "wti": "CL=F",
    "gold": "GC=F", "silver": "SI=F", "copper": "HG=F",
    "natural gas": "NG=F", "nat gas": "NG=F",
    "platinum": "PL=F", "palladium": "PA=F",
    "iron ore": "TIO=F", "wheat": "ZW=F", "corn": "ZC=F", "soybeans": "ZS=F",
    # Indices (name mentions)
    "s&p 500": "^GSPC", "s&p": "^GSPC", "dow jones": "^DJI", "dow": "^DJI",
    "nasdaq": "^IXIC", "russell": "^RUT", "russell 2000": "^RUT",
    "ftse": "^FTSE", "ftse 100": "^FTSE", "dax": "^GDAXI",
    "nikkei": "^N225", "hang seng": "^HSI", "shanghai": "000001.SS",
    "kospi": "^KS11", "sensex": "^BSESN", "nifty": "^NSEI",
    "cac 40": "^FCHI", "stoxx": "^STOXX50E", "euro stoxx": "^STOXX50E",
    "asx": "^AXJO", "ibovespa": "^BVSP",
}


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# 3–5 letter all-caps word (standalone). Minimum 3 avoids 2-letter noise (HK, FX, AT, SS…).
# Real 2-letter tickers (GE, BA, GM…) are caught by the company-name map in layer 1.
TICKER_PATTERN = re.compile(r"\b([A-Z]{3,5})\b")

# Multi-word proper nouns: sequences of 2–5 capitalized words.
# Matches "Royal Bank of Canada", "Goldman Sachs", "Novo Nordisk" etc.
# Allows lowercase connectors: of, and, the, &, de, du, del, van, von
_CONNECTORS = {"of", "and", "the", "&", "de", "du", "del", "van", "von", "di", "la", "le"}
_PROPER_WORD = re.compile(
    r"\b([A-Z][a-z]+(?:\s+(?:[A-Z][a-z]+|" + "|".join(re.escape(c) for c in _CONNECTORS) + r")){1,4})\b"
)


def _extract_proper_nouns(text: str) -> list[str]:
    """
    Extract multi-word proper noun phrases from text.
    Returns lowercase phrases suitable for resolver lookup.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _PROPER_WORD.finditer(text):
        phrase = m.group(1).strip().lower()
        if phrase not in seen and len(phrase) >= 4:
            seen.add(phrase)
            out.append(phrase)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_likely_ticker(s: str) -> bool:
    """True if s looks like a ticker (2–5 all-caps letters, not in blocklist)."""
    if not s or len(s) < 2 or len(s) > 5:
        return False
    if s != s.upper():
        return False
    if not s.isalpha():
        return False
    return s not in get_blocklist()


def resolve_company_to_ticker(query: str) -> list[dict]:
    """
    Resolve a company name (or partial) to ticker(s).
    Uses hardcoded map first, then dynamic resolver.
    Returns list of {"name": str, "ticker": str}.
    """
    if not query or not query.strip():
        return []
    q = query.strip().lower()
    out: list[dict] = []
    seen: set[str] = set()

    # Hardcoded matches
    for name, ticker in COMPANY_TO_TICKER.items():
        if name in q or q in name:
            if ticker not in seen:
                seen.add(ticker)
                out.append({"name": name, "ticker": ticker})

    # Dynamic resolver fallback
    if not out:
        from app.services.ticker_resolver import resolve_name
        sym = resolve_name(q)
        if sym:
            out.append({"name": q, "ticker": sym})

    return out


def detect_tickers(text: str) -> List[str]:
    """
    Extract likely ticker symbols from text. Returns deduplicated list.
    1. Hardcoded company name -> ticker (lowercase match)
    2. All-caps 2–5 letter tokens (excluding blocklist)
    3. Multi-word proper nouns -> dynamic resolver (yfinance.Search with cache)
    """
    if not text or not text.strip():
        return []
    seen: set[str] = set()
    out: list[str] = []

    # 1) Hardcoded company name -> ticker (fast-path)
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

    # 3) Multi-word proper nouns -> dynamic resolver
    proper_nouns = _extract_proper_nouns(text)
    if proper_nouns:
        from app.services.ticker_resolver import resolve_name
        for phrase in proper_nouns:
            # Skip if already matched via hardcoded
            if phrase in COMPANY_TO_TICKER:
                continue
            sym = resolve_name(phrase)
            if sym and sym not in seen:
                seen.add(sym)
                out.append(sym)

    return out
