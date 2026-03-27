"""
Fetches high-impact macro event dates from public sources (no API key required).

Sources:
- FOMC: Federal Reserve calendar HTML + press release JSON
- CPI: Pattern-based (BLS blocks automated access); ~2nd week of month
- NFP: First Friday of each month (reliable pattern)

Results are cached locally; refresh on startup or weekly.
"""
from __future__ import annotations

import json
import logging
import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Cache file location
CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "macro_calendar_cache.json"
CACHE_TTL_DAYS = 7  # Refresh weekly

# Fed endpoints (public, no API key)
FED_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FED_PRESS_JSON_URL = "https://www.federalreserve.gov/json/ne-press.json"

REQUEST_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _first_friday(year: int, month: int) -> date:
    """Return the first Friday of the given month."""
    first_day = date(year, month, 1)
    days_until_friday = (4 - first_day.weekday()) % 7
    return first_day + timedelta(days=days_until_friday)


def _approx_cpi_date(year: int, month: int) -> date:
    """
    Approximate CPI release date: typically 2nd week of month (around 10th-14th).
    BLS usually releases on a Tuesday or Wednesday.
    """
    # Target: second Tuesday-Wednesday of the month (around day 10-14)
    first_day = date(year, month, 1)
    # Find first Tuesday (weekday=1)
    days_to_tuesday = (1 - first_day.weekday()) % 7
    first_tuesday = first_day + timedelta(days=days_to_tuesday)
    # Second Tuesday is +7 days
    second_tuesday = first_tuesday + timedelta(days=7)
    # If that's before day 10, shift by a day or two
    if second_tuesday.day < 10:
        return second_tuesday + timedelta(days=1)  # Wednesday
    return second_tuesday


def _fetch_fomc_from_calendar() -> list[date]:
    """
    Scrape FOMC meeting dates from the Fed's public calendar page.
    Returns the statement release day (second day of two-day meetings).
    """
    dates: list[date] = []
    try:
        resp = requests.get(
            FED_CALENDAR_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Find year panels
        panels = soup.find_all("div", class_="panel")
        for panel in panels:
            heading = panel.find("h4")
            if not heading:
                continue
            year_match = re.search(r"(20\d{2})", heading.get_text())
            if not year_match:
                continue
            year = int(year_match.group(1))

            # Find rows with meeting dates
            rows = panel.find_all("div", class_="row")
            current_month: int | None = None

            for row in rows:
                text = row.get_text(strip=True, separator=" ")

                # Check for month name
                for month_name, month_num in MONTH_MAP.items():
                    if month_name in text.lower():
                        current_month = month_num
                        break

                if current_month is None:
                    continue

                # Look for date range like "27-28" or single date like "22"
                date_match = re.search(r"(\d{1,2})(?:-(\d{1,2}))?", text)
                if date_match:
                    # Use the second day (statement day) if range, else single day
                    day_str = date_match.group(2) or date_match.group(1)
                    try:
                        day = int(day_str)
                        # Handle month rollover (e.g., "30-1" for Jan 30 - Feb 1)
                        if date_match.group(2) and int(date_match.group(2)) < int(date_match.group(1)):
                            # Statement day is in next month
                            if current_month == 12:
                                meeting_date = date(year + 1, 1, day)
                            else:
                                meeting_date = date(year, current_month + 1, day)
                        else:
                            meeting_date = date(year, current_month, day)
                        dates.append(meeting_date)
                    except ValueError:
                        pass

        logger.info(f"Fetched {len(dates)} FOMC dates from Fed calendar")
    except Exception as e:
        logger.warning(f"Failed to fetch FOMC calendar: {e}")

    return dates


def _fetch_fomc_from_press_releases() -> list[date]:
    """
    Fetch past FOMC statement dates from the Fed's press release JSON.
    Useful for historical data and validation.
    """
    dates: list[date] = []
    try:
        resp = requests.get(
            FED_PRESS_JSON_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        # Handle UTF-8 BOM
        text = resp.content.decode("utf-8-sig")
        data = json.loads(text)

        for item in data:
            title = item.get("t", "")
            if "FOMC statement" in title:
                date_str = item.get("d", "")
                # Format: "3/18/2026 2:00:00 PM"
                try:
                    dt = datetime.strptime(date_str.split()[0], "%m/%d/%Y")
                    dates.append(dt.date())
                except ValueError:
                    pass

        logger.info(f"Fetched {len(dates)} FOMC dates from press releases")
    except Exception as e:
        logger.warning(f"Failed to fetch FOMC press releases: {e}")

    return dates


def _generate_nfp_dates(start_year: int, end_year: int) -> list[date]:
    """Generate NFP (first Friday) dates for a range of years."""
    dates: list[date] = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            dates.append(_first_friday(year, month))
    return dates


def _generate_cpi_dates(start_year: int, end_year: int) -> list[date]:
    """Generate approximate CPI release dates for a range of years."""
    dates: list[date] = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            dates.append(_approx_cpi_date(year, month))
    return dates


def fetch_all_macro_dates(
    years_ahead: int = 2,
    years_back: int = 2,
) -> dict[str, list[str]]:
    """
    Fetch all macro event dates from public sources.
    Returns dict with 'fomc', 'cpi', 'nfp' keys, each containing ISO date strings.
    """
    current_year = date.today().year
    start_year = current_year - years_back
    end_year = current_year + years_ahead

    # FOMC: combine calendar (future) and press releases (past)
    fomc_calendar = _fetch_fomc_from_calendar()
    fomc_press = _fetch_fomc_from_press_releases()
    fomc_all = list(set(fomc_calendar + fomc_press))
    fomc_filtered = [d for d in fomc_all if start_year <= d.year <= end_year]

    # NFP and CPI: pattern-based
    nfp_dates = _generate_nfp_dates(start_year, end_year)
    cpi_dates = _generate_cpi_dates(start_year, end_year)

    return {
        "fomc": sorted([d.isoformat() for d in fomc_filtered]),
        "cpi": sorted([d.isoformat() for d in cpi_dates]),
        "nfp": sorted([d.isoformat() for d in nfp_dates]),
        "fetched_at": datetime.utcnow().isoformat(),
        "start_year": start_year,
        "end_year": end_year,
    }


def _load_cache() -> dict[str, Any] | None:
    """Load cached macro dates if fresh enough."""
    if not CACHE_PATH.exists():
        return None
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        fetched_at = datetime.fromisoformat(data.get("fetched_at", "2000-01-01"))
        if datetime.utcnow() - fetched_at > timedelta(days=CACHE_TTL_DAYS):
            logger.info("Macro calendar cache expired")
            return None
        return data
    except Exception as e:
        logger.warning(f"Failed to load macro calendar cache: {e}")
        return None


def _save_cache(data: dict[str, Any]) -> None:
    """Save macro dates to cache file."""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Saved macro calendar cache to {CACHE_PATH}")
    except Exception as e:
        logger.warning(f"Failed to save macro calendar cache: {e}")


def get_high_impact_dates(force_refresh: bool = False) -> set[tuple[int, int, int]]:
    """
    Get all high-impact macro dates as (year, month, day) tuples.
    Uses cache if available and fresh; fetches from sources otherwise.
    """
    cache = None if force_refresh else _load_cache()

    if cache is None:
        logger.info("Fetching macro calendar from public sources...")
        cache = fetch_all_macro_dates()
        _save_cache(cache)

    # Combine all dates into a set of tuples
    all_dates: set[tuple[int, int, int]] = set()
    for key in ("fomc", "cpi", "nfp"):
        for iso_date in cache.get(key, []):
            try:
                d = date.fromisoformat(iso_date)
                all_dates.add((d.year, d.month, d.day))
            except ValueError:
                pass

    logger.info(f"Loaded {len(all_dates)} high-impact macro dates")
    return all_dates


def refresh_cache() -> dict[str, Any]:
    """Force refresh the macro calendar cache. Returns the new data."""
    data = fetch_all_macro_dates()
    _save_cache(data)
    return data
