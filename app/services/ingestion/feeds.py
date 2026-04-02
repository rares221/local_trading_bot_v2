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
    """Comprehensive feed list for stocks, commodities, indices, crypto, macro."""
    return [
        # ── Finance / markets (broad) ────────────────────────────
        FeedSpec("https://www.cnbc.com/id/10000664/device/rss/rss.html", "finance"),
        FeedSpec("https://www.ft.com/rss/markets", "finance"),
        FeedSpec("https://feeds.bloomberg.com/markets/news.rss", "finance"),
        FeedSpec("https://finance.yahoo.com/news/rssindex", "finance"),
        FeedSpec("https://www.marketwatch.com/rss/topstories", "finance"),
        FeedSpec("https://www.marketwatch.com/rss/marketpulse", "finance"),

        # ── Stocks / equities ────────────────────────────────────
        FeedSpec("https://www.cnbc.com/id/15839069/device/rss/rss.html", "stocks"),  # CNBC top news
        FeedSpec("https://www.cnbc.com/id/10001147/device/rss/rss.html", "stocks"),  # CNBC earnings
        FeedSpec("https://seekingalpha.com/feed.xml", "stocks"),
        FeedSpec("https://feeds.reuters.com/reuters/businessNews", "stocks"),
        FeedSpec("https://www.investing.com/rss/news_14.rss", "stocks"),  # most popular
        FeedSpec("https://www.investing.com/rss/news_25.rss", "stocks"),  # stock market
        FeedSpec("https://www.zacks.com/feeds/zacks_market_rss.xml", "stocks"),

        # ── Indices / macro ──────────────────────────────────────
        FeedSpec("https://www.cnbc.com/id/20910258/device/rss/rss.html", "indices"),  # CNBC economy
        FeedSpec("https://www.investing.com/rss/news_1.rss", "indices"),  # economy
        FeedSpec("https://feeds.reuters.com/reuters/globalMarketsNews", "indices"),
        FeedSpec("https://www.federalreserve.gov/feeds/press_all.xml", "indices"),  # Fed releases

        # ── Commodities (oil, gold, gas, metals) ─────────────────
        FeedSpec("https://www.eia.gov/rss/main.rss", "commodities"),
        FeedSpec("https://www.investing.com/rss/news_285.rss", "commodities"),  # energy
        FeedSpec("https://www.investing.com/rss/news_4.rss", "commodities"),   # commodities
        FeedSpec("https://www.kitco.com/rss/gold.xml", "commodities"),  # gold
        FeedSpec("https://www.kitco.com/rss/kitconews.xml", "commodities"),  # metals
        FeedSpec("https://oilprice.com/rss/main", "commodities"),  # oil/energy

        # ── Forex ────────────────────────────────────────────────
        FeedSpec("https://www.investing.com/rss/news_5.rss", "forex"),  # forex
        FeedSpec("https://www.dailyfx.com/feeds/market-news", "forex"),
        FeedSpec("https://www.forexlive.com/feed", "forex"),

        # ── Europe ────────────────────────────────────────────────
        FeedSpec("https://www.ft.com/rss/companies/europe", "europe"),
        FeedSpec("https://feeds.reuters.com/reuters/UKBankingFinancial", "europe"),
        FeedSpec("https://www.investing.com/rss/news_11.rss", "europe"),  # European markets
        FeedSpec("https://www.ecb.europa.eu/rss/press.html", "europe"),   # ECB press
        FeedSpec("https://www.ft.com/rss/companies/uk", "europe"),

        # ── Asia ─────────────────────────────────────────────────
        FeedSpec("https://asia.nikkei.com/rss", "asia"),                  # Nikkei Asia
        FeedSpec("https://www.scmp.com/rss/2/feed", "asia"),              # SCMP business
        FeedSpec("https://feeds.reuters.com/reuters/asiaPacificMarketsNews", "asia"),
        FeedSpec("https://www.investing.com/rss/news_12.rss", "asia"),    # Asian markets
        FeedSpec("https://www.ft.com/rss/companies/asia-pacific", "asia"),

        # ── Politics / geopolitical risk ─────────────────────────
        FeedSpec("https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml", "politics"),
        FeedSpec("https://feeds.washingtonpost.com/rss/politics", "politics"),
        FeedSpec("https://www.politico.com/rss/politicopicks.xml", "politics"),
        FeedSpec("https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "politics"),

        # ── Trading / technical analysis ─────────────────────────
        FeedSpec("https://www.investing.com/rss/market_overview.rss", "trading"),
        FeedSpec("https://www.tradingview.com/feed/", "trading"),
        FeedSpec("https://www.investing.com/rss/news_301.rss", "trading"),  # technical analysis

        # ── Crypto ───────────────────────────────────────────────
        FeedSpec("https://cointelegraph.com/rss", "crypto"),
        FeedSpec("https://coindesk.com/arc/outboundfeeds/rss/", "crypto"),
        FeedSpec("https://cryptonews.com/news/feed/", "crypto"),
    ]


FEED_CATEGORIES = [
    "finance", "stocks", "indices", "commodities", "forex",
    "europe", "asia", "politics", "trading", "crypto",
]
