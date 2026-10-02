"""
IO / adapter glue between the pure shorts_signals engine and the batch orchestrator.

Keeps `shorts_signals.py` pure (no DB/network/clock); everything here does the live fetching,
the ShortSignal -> Candidate conversion, the per-category candidate assembly the daily batch
consumes, and (Phase 2) persisting the "armed, waiting for confirmation" state across daily
batch runs. See docs/superpowers/plans/2026-09-18-shorts-engine.md.

`build_daily_candidates` (engine.py) runs sync, off the asyncio loop via `asyncio.to_thread` —
so the armed-state persistence here uses its own small SYNC DB session (see
`_get_sync_session`), independent of the app's async engine, rather than threading async state
through the whole candidate-building call chain. The four `_load_pending_arm` / `_insert_arm` /
`_mark_arm_resolved` / `_increment_arm_pending` functions are the only things that touch the DB
directly — kept small and separate specifically so unit tests can monkeypatch them instead of
exercising real SQLAlchemy (the unit suite stubs SQLAlchemy globally; see tests/conftest.py).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.services.technicals import build_snapshot, fetch_ohlcv, compute_relative_strength
from app.services.recommendations.shorts_signals import (
    ArmedShort,
    try_arm_short,
    check_confirmation,
    finalize_short_signal,
    family_of_trigger,
    _tape_deteriorating,
)

# Benchmark for the RS-vs-market gates (Phase 1 redesign — see shorts_signals.py).
_RS_BENCHMARK = "^GSPC"

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


# ── Phase 2: armed-state persistence (sync DB, see module docstring) ────────

_sync_sessionmaker = None


def _get_sync_session():
    """Lazy-singleton sync session factory, independent of the app's async engine."""
    global _sync_sessionmaker
    if _sync_sessionmaker is None:
        import sqlalchemy as sa
        from sqlalchemy.orm import sessionmaker
        from app.core.config import settings
        sync_url = settings.database_url.replace("+aiosqlite", "").replace("+asyncpg", "")
        engine = sa.create_engine(sync_url)
        _sync_sessionmaker = sessionmaker(bind=engine)
    return _sync_sessionmaker()


def _load_pending_arm(symbol: str) -> Optional[dict]:
    """Return the pending armed_shorts row for `symbol`, or None if not currently armed."""
    from app.models.armed_short import ArmedShortRow
    with _get_sync_session() as session:
        row = (
            session.query(ArmedShortRow)
            .filter(ArmedShortRow.symbol == symbol, ArmedShortRow.status == "pending")
            .one_or_none()
        )
        if row is None:
            return None
        return {
            "id": row.id, "family": row.family, "trigger": row.trigger,
            "armed_date": row.armed_date, "days_since_armed": row.days_since_armed,
        }


def _insert_arm(symbol: str, category: str, family: str, trigger: str, armed_date: date) -> None:
    from app.models.armed_short import ArmedShortRow
    with _get_sync_session() as session:
        session.add(ArmedShortRow(
            symbol=symbol, category=category, family=family, trigger=trigger,
            armed_date=armed_date, days_since_armed=0, status="pending",
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()


def _mark_arm_resolved(row_id: int, status: str) -> None:
    from app.models.armed_short import ArmedShortRow
    with _get_sync_session() as session:
        row = session.get(ArmedShortRow, row_id)
        if row is not None:
            row.status = status
            row.resolved_at = datetime.now(timezone.utc)
            session.commit()


def _increment_arm_pending(row_id: int) -> None:
    from app.models.armed_short import ArmedShortRow
    with _get_sync_session() as session:
        row = session.get(ArmedShortRow, row_id)
        if row is not None:
            row.days_since_armed += 1
            session.commit()


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
    Scan the short universe for `category` (Phase 2 two-stage entry):
      - a symbol with a pending arm gets checked for confirmation/expiry against TODAY's fresh
        data; a confirmed arm is scored/leveled (finalize_short_signal) and — if it still
        clears the confidence/R:R gates — becomes a Candidate.
      - a symbol with no pending arm is scanned for a NEW trigger (try_arm_short); if one
        fires, it's recorded as pending and does NOT become a Candidate this cycle (Phase 2's
        whole point is not entering on the trigger bar itself).
    Returns Candidates ranked by confidence (desc). Honors exclude_taken.
    """
    excluded = exclude_taken or set()
    cands: list = []
    for (cat, asset_id, yf_symbol, risk_tier) in SHORT_UNIVERSE:
        if cat != category:
            continue
        try:
            snap = build_snapshot(yf_symbol)
            if snap is None or getattr(snap, "price", 0) <= 0:
                continue
            df = fetch_ohlcv(yf_symbol, period="1y")
            if df is None or df.empty or len(df) < 60:
                continue
            # RS-vs-market gates (Phase 1 redesign): 20d for breakdown's weakness check, 60d
            # for fade-top's prior-outperformance check. Walk-forward-safe / cached.
            rs_vs_market = compute_relative_strength(yf_symbol, _RS_BENCHMARK, period=20)
            rs_prior = compute_relative_strength(yf_symbol, _RS_BENCHMARK, period=60)

            pending = _load_pending_arm(yf_symbol)
            if pending is not None:
                armed = ArmedShort(
                    symbol=yf_symbol, category=cat, family=pending["family"],
                    trigger=pending["trigger"], armed_date=pending["armed_date"],
                )
                status = check_confirmation(
                    armed, snap, df, regime, tape_deteriorating,
                    rs_vs_market, rs_prior, pending["days_since_armed"],
                )
                if status == "confirmed":
                    _mark_arm_resolved(pending["id"], "confirmed")
                    sig = finalize_short_signal(
                        armed, snap, df, regime_id=regime, risk_tier=risk_tier,
                        preferred_risk=preferred_risk, sentiment_avg=None,
                        sentiment_count=0, today=today,
                    )
                    if sig is not None and (cat, asset_id) not in excluded:
                        cands.append(short_signal_to_candidate(sig, cat, asset_id, risk_tier, today))
                elif status == "expired":
                    _mark_arm_resolved(pending["id"], "expired")
                else:
                    _increment_arm_pending(pending["id"])
                # Already armed this cycle either way — don't also scan for a new trigger.
                continue

            if (cat, asset_id) in excluded:
                continue
            trigger = try_arm_short(snap, df, regime, tape_deteriorating, rs_vs_market, rs_prior)
            if trigger is not None:
                _insert_arm(yf_symbol, cat, family_of_trigger(trigger), trigger, today)
        except Exception:
            continue
    cands.sort(key=lambda c: -c.confidence_score)
    return cands
