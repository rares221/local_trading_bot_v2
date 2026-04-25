"""
Trade Watcher — persists the user's selected trade across restarts and page refreshes.

State file: data/live_selected_trade.json
Watchlist file: data/live_watchlist.json

The selected trade is cleared only when the user explicitly deselects it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path("data")
_TRADE_FILE = _DATA_DIR / "live_selected_trade.json"
_WATCHLIST_FILE = _DATA_DIR / "live_watchlist.json"

DEFAULT_WATCHLIST = [
    "AAPL", "TSLA", "NVDA", "MSFT",
    "GC=F", "CL=F", "BZ=F", "GC=F", "NG=F", "SI=F",
    "BTC-USD", "ETH-USD", "BNB-USD", "SOL-USD",
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X",
    "SPY", "QQQ", "GLD", "TLT", "XLE", "XLF",
    "^GSPC", "^DJI", "^IXIC", "^RUT", "^FTSE", "^GDAXI", "^N225", "^VIX",
    "XOM", "CVX", "BP", "SHEL", "NEM",
]


class TradeWatcher:
    """
    Manages a single pinned trade.
    Thread-safe for asyncio (no threading locks needed since single-event-loop).
    """

    def __init__(self) -> None:
        self._trade: dict | None = None
        self._load()

    def _load(self) -> None:
        try:
            if _TRADE_FILE.exists():
                self._trade = json.loads(_TRADE_FILE.read_text(encoding="utf-8"))
                logger.info("[WATCHER] Loaded selected trade: %s", self._trade.get("ticker"))
        except Exception as exc:
            logger.warning("[WATCHER] Could not load trade file: %s", exc)
            self._trade = None

    def get(self) -> dict | None:
        return self._trade

    def set(self, ticker: str, category: str, entry_price: float | None,
            direction: str, stop_loss: float | None, take_profit: float | None,
            confidence: int | None) -> dict:
        self._trade = {
            "ticker": ticker,
            "category": category,
            "selected_at": datetime.now(timezone.utc).isoformat(),
            "entry_price": entry_price,
            "direction": direction,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "confidence": confidence,
            "last_analysis_at": None,
        }
        self._persist()
        return self._trade

    def clear(self) -> None:
        self._trade = None
        try:
            _TRADE_FILE.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("[WATCHER] Could not clear trade file: %s", exc)

    def update_from_analysis(self, result: dict) -> None:
        """Apply ticker_watch LLM result to the selected trade state."""
        if not self._trade:
            return
        if result.get("confidence") is not None:
            self._trade["confidence"] = result["confidence"]
        if result.get("suggested_sl") is not None:
            self._trade["stop_loss"] = result["suggested_sl"]
        if result.get("suggested_tp") is not None:
            self._trade["take_profit"] = result["suggested_tp"]
        self._trade["last_analysis_at"] = datetime.now(timezone.utc).isoformat()
        self._trade["last_action"] = result.get("action", "hold")
        self._trade["last_assessment"] = result.get("assessment", "")
        self._trade["direction_flip"] = result.get("direction_flip", False)
        self._persist()

    def _persist(self) -> None:
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            _TRADE_FILE.write_text(json.dumps(self._trade, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("[WATCHER] Could not persist trade file: %s", exc)


# ── Watchlist helpers ─────────────────────────────────────────────────────────

def get_watchlist() -> list[str]:
    """
    Returns the auto-generated watchlist from the live_analyzer universe
    (base assets + sentiment-discovered tickers).
    Falls back to DEFAULT_WATCHLIST if analyzer not available.
    """
    try:
        from app.live_trading.live_analyzer import get_auto_watchlist_symbols
        symbols = get_auto_watchlist_symbols()
        if symbols:
            return symbols
    except Exception as exc:
        logger.warning("[WATCHER] Could not get auto watchlist: %s", exc)
    return list(DEFAULT_WATCHLIST)


def set_watchlist(tickers: list[str]) -> None:
    """Kept for backward compatibility — no longer writes to file (auto-generated)."""
    pass


# ── Module-level singleton ────────────────────────────────────────────────────

watcher = TradeWatcher()
