"""
Default RSS feeds. Translated from codeops-trading-app/codeops-trading-rss-ingester/internal/feed/feed.go.
Extended with oil/commodity category per TRADING_APP_PLAN and LEVERAGE_PLAN.
"""
from dataclasses import dataclass
from typing import Optional
from datetime import datetime


@dataclass
class FeedSpec:
    url: str
    category: str
    last_fetch: Optional[datetime] = None


def default_feeds() -> list[FeedSpec]:
    """Same feed list as codeops-trading-app DefaultFeeds(), plus oil/commodity."""
    return [
        # Finance (from codeops-trading-app)
        FeedSpec("https://www.cnbc.com/id/10000664/device/rss/rss.html", "finance"),
        FeedSpec("https://www.ft.com/rss/markets", "finance"),
        FeedSpec("https://feeds.bloomberg.com/markets/news.rss", "finance"),
        # Politics
        FeedSpec("https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml", "politics"),
        FeedSpec("https://feeds.washingtonpost.com/rss/politics", "politics"),
        FeedSpec("https://www.politico.com/rss/politicopicks.xml", "politics"),
        # Trading
        FeedSpec("https://www.investing.com/rss/market_overview.rss", "trading"),
        FeedSpec("https://seekingalpha.com/feed.xml", "trading"),
        FeedSpec("https://www.tradingview.com/feed/", "trading"),
        # Crypto
        FeedSpec("https://cointelegraph.com/rss", "crypto"),
        FeedSpec("https://coindesk.com/arc/outboundfeeds/rss/", "crypto"),
        FeedSpec("https://cryptonews.com/news/feed/", "crypto"),
        # Oil / commodities (added for oil/gold/gas focus)
        FeedSpec("https://www.eia.gov/rss/main.rss", "commodities"),
        FeedSpec("https://www.investing.com/rss/news_285.rss", "commodities"),  # energy
    ]


# Categories for storage/cleanup (aligned with codeops + commodities)
FEED_CATEGORIES = ["finance", "politics", "trading", "crypto", "commodities"]
