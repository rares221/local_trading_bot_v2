"""
Event type tagging for feed items. Used to adjust recommendation logic (e.g. reduce confidence around Fed/earnings).
"""
# Event type -> trigger keywords (lowercase)
EVENT_KEYWORDS: dict[str, list[str]] = {
    "earnings": [
        "earnings", "eps", "beat", "miss", "guidance", "quarterly results",
        "q1 ", "q2 ", "q3 ", "q4 ", "fy ", "revenue beat", "profit beat",
    ],
    "fed": [
        "fomc", "federal reserve", "fed rate", "powell", "fed funds",
        "rate cut", "rate hike", "interest rate", "fed meeting", "dot plot",
    ],
    "geopolitics": [
        "war", "sanctions", "election", "geopolitical", "tension",
        "conflict", "nato", "russia", "ukraine", "china", "tariff", "trade war",
    ],
    "macro": [
        "cpi", "inflation", "jobs report", "nfp", "gdp", "employment",
        "consumer confidence", "pmi", "ism ", "retail sales", "ppi",
    ],
}


def tag_event_types(text: str) -> list[str]:
    """Return event types (e.g. ['earnings', 'fed']) from keyword match on text."""
    if not text:
        return []
    lower = text.lower()
    found: list[str] = []
    for event_type, keywords in EVENT_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            found.append(event_type)
    return found
