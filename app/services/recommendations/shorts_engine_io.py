"""
IO / adapter glue between the pure shorts_signals engine and the batch orchestrator.

Keeps `shorts_signals.py` pure (no DB/network/clock); everything here does the live fetching,
the ShortSignal -> Candidate conversion, and the per-category candidate assembly the daily
batch consumes. See docs/superpowers/plans/2026-09-18-shorts-orchestrator-integration.md.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from app.services.technicals import build_snapshot, fetch_ohlcv
from app.services.recommendations.shorts_signals import (
    generate_short_signal,
    _tape_deteriorating,
)

# (category, asset_id, yf_symbol, risk_tier). Individual names -> stocks_large; broad/sector ETFs
# -> indices. Single validated universe for now; per-family universes (breakdown tighter /
# fade-top broader) are a follow-up tuning, not required for integration.
SHORT_UNIVERSE: list[tuple[str, str, str, str]] = [
    ("stocks_large", s, s, "moderate") for s in
    ["AAPL", "MSFT", "NVDA", "META", "AMZN", "TSLA", "GOOGL", "NFLX", "AMD", "AVGO",
     "CRM", "COST", "HD", "JPM", "BAC"]
] + [
    ("indices", s, s, "moderate") for s in
    ["XLF", "XLY", "XLK", "XLI", "XLE", "SMH", "HYG", "JNK", "IWM", "SPY", "QQQ", "DIA"]
]

_HORIZON_DAYS = 14  # 14-day prediction app


def compute_tape_deteriorating() -> bool:
    """Fade-top activation for the live batch: fetch ^VIX/^GSPC and apply _tape_deteriorating."""
    vix = fetch_ohlcv("^VIX", period="90d")
    spx = fetch_ohlcv("^GSPC", period="90d")
    if vix is None or spx is None or "Close" not in vix.columns or "Close" not in spx.columns:
        return False
    return _tape_deteriorating(vix["Close"], spx["Close"])


def short_signal_to_candidate(sig, category: str, asset_id: str, risk_tier: str, today: date):
    """Convert a pure ShortSignal into a batch Candidate (direction='short')."""
    from app.services.recommendations.engine import Candidate, _suggested_risk_pct
    suggested = _suggested_risk_pct(
        sig.confidence, sig.risk_reward,
        trend_aligned=False, sector_aligned=False, gap_sizing_mult=1.0, gap_max_penalty=0.0,
    )
    return Candidate(
        asset_category=category,
        asset_id=asset_id,
        risk_tier=risk_tier,
        buy_in_price=sig.entry,
        stop_loss=sig.stop_loss,
        take_profit=sig.take_profit,
        direction="short",
        expiry_date=today + timedelta(days=_HORIZON_DAYS),
        confidence_score=sig.confidence,
        risk_reward=sig.risk_reward,
        market_regime_note=f"short:{sig.trigger}",
        suggested_risk_pct=suggested,
        _score=float(sig.confidence),
    )


def build_short_candidates_for_category(
    category: str,
    *,
    regime: Optional[str],
    tape_deteriorating: bool,
    exclude_taken: Optional[set[tuple[str, str]]],
    today: date,
    preferred_risk: str = "moderate",
) -> list:
    """
    Scan the short universe for `category`, generate short signals, convert to Candidates,
    and return them ranked by confidence (desc). Honors exclude_taken.
    """
    excluded = exclude_taken or set()
    cands: list = []
    for (cat, asset_id, yf_symbol, risk_tier) in SHORT_UNIVERSE:
        if cat != category:
            continue
        if (cat, asset_id) in excluded:
            continue
        try:
            snap = build_snapshot(yf_symbol)
            if snap is None or getattr(snap, "price", 0) <= 0:
                continue
            df = fetch_ohlcv(yf_symbol, period="1y")
            if df is None or df.empty or len(df) < 60:
                continue
            sig = generate_short_signal(
                yf_symbol, snap, df,
                regime_id=regime, category=cat, risk_tier=risk_tier,
                preferred_risk=preferred_risk, sentiment_avg=None, sentiment_count=0,
                today=today, tape_deteriorating=tape_deteriorating,
            )
        except Exception:
            continue
        if sig is None:
            continue
        cands.append(short_signal_to_candidate(sig, cat, asset_id, risk_tier, today))
    cands.sort(key=lambda c: -c.confidence_score)
    return cands
