"""
APScheduler jobs: ingestion + sentiment, analysis run, daily recommendation batch,
active-trade updates (ATR-based trailing), daily briefing, feed cleanup.
"""
import asyncio
from functools import partial
from datetime import datetime, timezone, date, timedelta
from typing import Optional
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.feed_item import FeedItem
from app.models.recommendation import Recommendation, ActiveTrade, ActiveTradeLevel
from app.models.market_analysis import MarketAnalysis


def _infer_instrument_type(asset_category: str) -> str:
    """Infer instrument type from asset category. cfd | invest."""
    cat = (asset_category or "").lower()
    if cat in ("indices", "index", "forex", "commodities", "oil", "commodity"):
        return "cfd"
    if cat in ("stocks_large", "stocks_mid", "stocks_small", "stocks"):
        return "invest"
    return "cfd"
from app.services.ingestion import fetch_all_feeds, save_feed_items
from app.services.sentiment import score_sentiment, tag_assets, detect_tickers
from app.services.analysis import run_analysis, get_latest_analysis
from app.services.recommendations import build_daily_candidates, get_asset_price_and_risk
from app.services.recommendations.engine import get_yf_symbol, compute_sl_tp_for_price, get_sentiment_score_and_count
from app.services.market_context import build_market_context
from app.services.technicals import build_snapshot, compute_atr_levels, clear_cache, determine_direction
from app.services.briefing import run_briefing
from app.services.regime_monitor import (
    RegimeTransition,
    detect_regime_transition,
    get_trades_needing_review,
    should_skip_recommendations_on_transition,
)


# Gap risk cache: populated by job_update_active_trades(), consumed by /api/active-trades
# Key: yfinance symbol (e.g. "AAPL"), value: {"level": "low"|"medium"|"high", "note": str}
_gap_risk_by_symbol: dict[str, dict] = {}

# Volume Profile cache: populated by job_update_active_trades() + job_daily_batch()
# Key: yfinance symbol, value: vpoc/vah/val/hvn_levels/lvn_levels/vp_bias/vp_overextension/in_value_area
_vp_by_symbol: dict[str, dict] = {}

# Relative Strength cache: populated by job_daily_batch()
# Key: yfinance symbol, value: {rs_20d, rs_60d, sector_rs}
_rs_by_symbol: dict[str, dict] = {}


async def job_ingest_and_sentiment() -> None:
    """Fetch RSS feeds, save items, run sentiment and asset tagging."""
    from app.services.ticker_resolver import reset_run_counter
    import app.services.pipeline_progress as progress
    reset_run_counter()
    progress.update(2, "ingest", "Fetching RSS feeds…")
    async with async_session_factory() as session:
        items = await asyncio.to_thread(fetch_all_feeds)
        progress.update(8, "ingest", f"Saving {len(items)} feed items…")
        saved = await save_feed_items(session, items)
        q = select(FeedItem).where(FeedItem.is_processed == False).limit(500)
        result = await session.execute(q)
        unprocessed = result.scalars().all()
        total = len(unprocessed)
        progress.update(12, "ingest", f"Scoring sentiment for {total} items…")

        def _score_all(items_):
            for i, it in enumerate(items_):
                text = f"{it.title or ''} {it.description or ''}"
                it.sentiment_score = score_sentiment(text)
                keyword_tags = tag_assets(text) or []
                dynamic_tickers = detect_tickers(text) or []
                it.asset_tags = list(dict.fromkeys(keyword_tags + dynamic_tickers)) or None
                it.is_processed = True
                if total > 0 and i % max(1, total // 5) == 0:
                    pct = 12 + int((i / total) * 13)
                    progress.update(pct, "ingest", f"Scoring sentiment… {i}/{total}")

        await asyncio.to_thread(_score_all, unprocessed)
        await session.commit()
        progress.update(25, "ingest", "Ingestion complete")
        progress.log_stage(
            "Ingest & Sentiment",
            "ok",
            f"Fetched {len(items)} feed items, scored {total} new items",
        )


async def job_run_analysis() -> None:
    """Build market analysis from feed items."""
    import app.services.pipeline_progress as progress
    progress.update(27, "analysis", "Aggregating sentiment data…")
    async with async_session_factory() as session:
        progress.update(32, "analysis", "Fetching price snapshots…")
        await run_analysis(session)
        await session.commit()
    progress.update(50, "analysis", "Analysis complete")
    progress.log_stage("Market Analysis", "ok", "Sentiment aggregated, price snapshots fetched, divergence detected")


async def job_daily_batch() -> None:
    """
    Produce today's batch: up to 5 recommendations per category (stocks, commodities, other, indices),
    i.e. up to ~20 total, with ATR SL/TP, direction, expiry, global correlation and risk-budget filters.
    """
    import app.services.pipeline_progress as progress
    batch_id = date.today().isoformat()
    print(f"[BATCH] Starting daily batch for {batch_id}")
    try:
        async with async_session_factory() as session:
            q = select(Recommendation).where(
                Recommendation.batch_id == batch_id, Recommendation.status == "pending"
            ).limit(1)
            result = await session.execute(q)
            if result.scalars().first():
                print("[BATCH] Pending recs already exist for today — skipping")
                progress.update(100, "done", "Batch already exists for today")
                return
            progress.update(52, "batch", "Loading market analysis…")
            analysis = await get_latest_analysis(session)
            if not analysis:
                print("[BATCH] No analysis found — run the full pipeline first")
                return
            # Cache id before heavy async work — the asyncpg connection may go idle
            # during asyncio.to_thread(_build) and LLM calls, causing SQLAlchemy to
            # expire the ORM instance; accessing analysis.id after that triggers a
            # pre-ping in a non-greenlet context → MissingGreenlet error.
            analysis_id = analysis.id
            print(f"[BATCH] Analysis found (id={analysis_id})")
            q_taken = select(Recommendation.asset_category, Recommendation.asset_id).where(
                Recommendation.status == "taken"
            ).distinct()
            result_taken = await session.execute(q_taken)
            exclude_taken = {(row[0], row[1]) for row in result_taken.all()}
            from app.core.preferences import get_preferred_risk, get_risk_budget_1r
            payload = analysis.payload or {}
            by_asset_sentiment = payload.get("by_asset_sentiment") or None
            active_events = payload.get("active_events") or None
            # Fetch closed trades for circuit breaker evaluation
            q_closed = select(ActiveTrade).where(ActiveTrade.outcome.isnot(None))
            result_closed = await session.execute(q_closed)
            closed_trades = result_closed.scalars().all()
            print(f"[BATCH] Sentiment tags: {len(by_asset_sentiment) if by_asset_sentiment else 0}, excluded taken: {len(exclude_taken)}, closed trades: {len(closed_trades)}")
            progress.update(55, "batch", "Building candidate recommendations…")
            # Run heavy sync work off the asyncio loop so HTTP (e.g. /api/recommendations) stays responsive.
            _build = partial(
                build_daily_candidates,
                by_asset_sentiment=by_asset_sentiment,
                preferred_risk=get_preferred_risk(),
                tie_seed=batch_id,
                exclude_taken=exclude_taken,
                active_events=active_events,
                risk_budget_1r=get_risk_budget_1r(),
                closed_trades=list(closed_trades),
            )
            candidates = await asyncio.to_thread(_build)
            print(f"[BATCH] Candidates built: {len(candidates)}")
            # Populate VP + RS caches from candidates (snap was already computed inside engine)
            for _c in candidates:
                _sym = get_yf_symbol(_c.asset_category, _c.asset_id)
                if _sym and _c.vpoc is not None:
                    _vp_by_symbol[_sym] = {
                        "vpoc": _c.vpoc, "vah": _c.vah, "val": _c.val,
                        "hvn_levels": [], "lvn_levels": [],
                        "vp_bias": _c.vp_bias, "vp_overextension": _c.vp_overextension,
                        "in_value_area": _c.in_value_area,
                    }
                if _sym and (_c.rs_20d is not None or _c.rs_60d is not None):
                    _rs_by_symbol[_sym] = {
                        "rs_20d": _c.rs_20d,
                        "rs_60d": _c.rs_60d,
                        "sector_rs": _c.sector_rs,
                    }
            for c in candidates:
                print(f"  [{c.asset_category}] {c.asset_id} dir={c.direction} conf={c.confidence_score} rr={c.risk_reward}")
            if not candidates:
                print("[BATCH] No candidates passed filters — nothing to save")
                progress.log_stage("Recommendations", "warn", "No candidates passed filters — batch skipped")
                return
            now = datetime.now(timezone.utc)
            total_candidates = len(candidates)
            llm_failures = []
            progress.update(60, "batch", f"Generating rationales for {total_candidates} candidates…")
            from app.services.research import build_research_context, generate_rationale
            from datetime import date as _date
            from app.services.technicals import classify_horizon as _classify_horizon
            for i, c in enumerate(candidates):
                llm_pct = 60 + int((i / total_candidates) * 36)
                progress.update(llm_pct, "batch", f"LLM rationale {i + 1}/{total_candidates} — {c.asset_id}")
                rationale_text = None
                try:
                    # Fresh session per candidate: the main batch session may have an idle
                    # PostgreSQL connection after the long asyncio.to_thread(_build) call.
                    # Reusing it triggers asyncpg pre-ping outside a greenlet → MissingGreenlet.
                    async with async_session_factory() as research_session:
                        ctx = await build_research_context(research_session, c.asset_id, c.asset_category)
                    ctx["buy_in_price"] = c.buy_in_price
                    ctx["direction"] = c.direction
                    if c.expiry_date and "expiry_days" not in ctx:
                        _ed = max(1, (c.expiry_date - _date.today()).days)
                        ctx["expiry_days"] = _ed
                        ctx["horizon"] = _classify_horizon(_ed)
                    rationale_text = await generate_rationale(ctx)
                except Exception as exc:
                    import traceback
                    print(f"  [BATCH] LLM rationale failed for {c.asset_id}: {exc}")
                    print(f"  [BATCH] Traceback:\n{''.join(traceback.format_exc())}")
                    llm_failures.append(f"{c.asset_id}: {exc}")
                # Persist regime / divergence / event hints with technical notes (Candidate.market_regime_note is not a DB column)
                _reg_note = getattr(c, "market_regime_note", None)
                _vol = c.volatility_notes
                if _reg_note and _vol:
                    _combined_notes = f"{_reg_note}; {_vol}"
                elif _reg_note:
                    _combined_notes = _reg_note
                else:
                    _combined_notes = _vol
                rec = Recommendation(
                    id=str(uuid4()),
                    created_at=now,
                    batch_id=batch_id,
                    asset_category=c.asset_category,
                    asset_id=c.asset_id,
                    buy_in_price=c.buy_in_price,
                    stop_loss=c.stop_loss,
                    take_profit=c.take_profit,
                    expiry_date=c.expiry_date,
                    rationale_ref=analysis_id,
                    rationale_text=rationale_text,
                    risk_tier=c.risk_tier,
                    direction=c.direction,
                    confidence_score=c.confidence_score,
                    risk_reward=c.risk_reward,
                    volatility_notes=_combined_notes,
                    status="pending",
                    suggested_risk_pct=getattr(c, "suggested_risk_pct", None),
                    min_confidence_gate=getattr(c, "min_confidence_gate", None),
                    shadow_b_threshold=getattr(c, "shadow_b_threshold", None),
                    shadow_b_pass=getattr(c, "shadow_b_pass", None),
                    instrument_type=_infer_instrument_type(c.asset_category),
                )
                session.add(rec)
            progress.update(97, "batch", f"Saving {len(candidates)} recommendations…")
            await session.commit()
            print(f"[BATCH] Saved {len(candidates)} recommendations")

            # Build category breakdown for log
            cats = {}
            for c in candidates:
                cat = c.asset_category or "other"
                cats[cat] = cats.get(cat, 0) + 1
            cat_str = ", ".join(f"{v} {k}" for k, v in sorted(cats.items()))
            batch_status = "warn" if llm_failures else "ok"
            progress.log_stage(
                "Recommendations",
                batch_status,
                f"Saved {len(candidates)} recommendations ({cat_str}); "
                f"{total_candidates - len(llm_failures)}/{total_candidates} rationales generated",
                warnings=llm_failures,
            )

            # Send notification
            try:
                from app.services.notifications import send_notification, EVENT_BATCH_READY
                await send_notification(EVENT_BATCH_READY, {
                    "count": len(candidates),
                    "categories": cats,
                    "batch_id": batch_id,
                })
            except Exception as notify_exc:
                print(f"[BATCH] Notification failed (non-critical): {notify_exc}")
    except Exception as exc:
        print(f"[BATCH] ERROR: {exc}")
        import traceback
        traceback.print_exc()
        progress.finish(error=str(exc))


async def job_update_active_trades() -> None:
    """
    Professional trade management lifecycle for active trades:
    1. OPEN phase: Track price movement, compute ATR trailing SL/TP
    2. At 1R profit (risk = |entry - SL|): suggest moving SL to breakeven -> PARTIAL_TP phase
    3. TRAILING phase: SL only moves favorably, trail behind structure
    4. If price hits TP or SL -> mark CLOSED
    Regime-aware: detects regime transitions and flags trades needing review.
    """
    def _regime_prep() -> tuple[Optional[RegimeTransition], Optional[str]]:
        clear_cache()
        market_ctx = build_market_context()
        rid = market_ctx.regime.regime if market_ctx else None
        trans = detect_regime_transition(rid) if rid else None
        return trans, rid

    transition, regime_id = await asyncio.to_thread(_regime_prep)
    if transition:
        print(f"[UPDATE_TRADES] {transition.description()}")
        # Send regime change notification
        try:
            from app.services.notifications import send_notification, EVENT_REGIME_CHANGE
            flagged = get_trades_needing_review(transition)
            await send_notification(EVENT_REGIME_CHANGE, {
                "old_regime": transition.old_regime,
                "new_regime": transition.new_regime,
                "flagged_count": len(flagged) if flagged else 0,
            })
        except Exception as notify_exc:
            print(f"[UPDATE_TRADES] Notification failed (non-critical): {notify_exc}")

    async with async_session_factory() as session:
        # Load latest analysis for sentiment lookup
        analysis = await get_latest_analysis(session)
        by_asset_sentiment = (analysis.payload or {}).get("by_asset_sentiment", {}) if analysis and analysis.payload else {}

        # Circuit breaker: evaluate from closed trades and log/notify if active
        try:
            from app.services.circuit_breaker import compute_state as _cb_compute
            from app.core.preferences import get_circuit_breaker_config
            _cb_cfg = get_circuit_breaker_config()
            if _cb_cfg["circuit_breaker_enabled"]:
                _closed_q = select(ActiveTrade).where(ActiveTrade.outcome.isnot(None))
                _closed_result = await session.execute(_closed_q)
                _closed_trades = _closed_result.scalars().all()
                _cb_state = _cb_compute(
                    list(_closed_trades),
                    max_consecutive_losses=_cb_cfg["max_consecutive_losses"],
                    max_daily_loss_r=_cb_cfg["max_daily_loss_r"],
                    drawdown_gate_r=_cb_cfg["drawdown_gate_r"],
                )
                if _cb_state.mode != "normal":
                    print(
                        f"[CB] mode={_cb_state.mode} | "
                        f"rolling_loss={_cb_state.rolling_loss_r}R | "
                        f"streak={_cb_state.consecutive_losses} | "
                        f"drawdown={_cb_state.drawdown_r}R | "
                        f"triggers={_cb_state.triggered_by}"
                    )
                    try:
                        from app.services.notifications import send_notification
                        await send_notification("circuit_breaker", {
                            "mode": _cb_state.mode,
                            "rolling_loss_r": _cb_state.rolling_loss_r,
                            "consecutive_losses": _cb_state.consecutive_losses,
                            "drawdown_r": _cb_state.drawdown_r,
                            "triggered_by": _cb_state.triggered_by,
                        })
                    except Exception:
                        pass
        except Exception as _cb_err:
            print(f"[UPDATE_TRADES] Circuit breaker check failed (non-critical): {_cb_err}")

        q = select(ActiveTrade).options(selectinload(ActiveTrade.recommendation))
        result = await session.execute(q)
        now = datetime.now(timezone.utc)
        for trade in result.scalars().all():
            rec = trade.recommendation
            if not rec:
                continue
            symbol = get_yf_symbol(rec.asset_category, rec.asset_id)
            if not symbol:
                trade.last_updated = now
                continue
            snap = build_snapshot(symbol)
            if not snap or snap.price <= 0:
                # Even without a live price, close trades that are past their expiry date.
                _phase = getattr(trade, "trade_phase", "open") or "open"
                if _phase == "open" and getattr(rec, "expiry_date", None) and rec.expiry_date < date.today():
                    trade.trade_phase = "closed_expired"
                    if getattr(trade, "outcome", None) is None:
                        trade.outcome = "expired"
                        trade.outcome_at = now
                        trade.closing_price = None
                trade.last_updated = now
                continue

            # Volume Profile cache: store VP data from snap (already computed, zero extra cost)
            if symbol not in _vp_by_symbol and snap.vpoc is not None:
                _vp_by_symbol[symbol] = {
                    "vpoc": snap.vpoc,
                    "vah": snap.vah,
                    "val": snap.val,
                    "hvn_levels": snap.hvn_levels,
                    "lvn_levels": snap.lvn_levels,
                    "vp_bias": snap.vp_bias,
                    "vp_overextension": snap.vp_overextension,
                    "in_value_area": snap.in_value_area(),
                }

            # Gap risk: compute once per symbol while snap is available (no extra API call)
            if symbol not in _gap_risk_by_symbol:
                try:
                    from app.services.recommendations.engine import _compute_gap_risk
                    from app.core.preferences import get_gap_risk_config
                    _gap_cfg = get_gap_risk_config()
                    if _gap_cfg["gap_risk_enabled"]:
                        _expiry_days = (trade.expiry - date.today()).days if trade.expiry else 7
                        _gap_lvl, _, _gap_note = _compute_gap_risk(
                            snap=snap,
                            expiry_days=max(0, _expiry_days),
                            near_earnings=False,
                            category=rec.asset_category,
                            today=date.today(),
                            max_penalty=_gap_cfg["gap_risk_max_penalty"],
                        )
                        _gap_risk_by_symbol[symbol] = {"level": _gap_lvl, "note": _gap_note}
                except Exception as _gap_err:
                    print(f"[UPDATE_TRADES] Gap risk compute failed for {symbol}: {_gap_err}")

            direction = getattr(rec, "direction", "long") or "long"
            current_price = snap.price
            entry_price = rec.buy_in_price
            original_sl = trade.current_sl
            risk = abs(entry_price - original_sl)
            phase = getattr(trade, "trade_phase", "open") or "open"

            # Expired: if still open and past expiry_date, close and log outcome
            today = date.today()
            if phase == "open" and getattr(rec, "expiry_date", None) and rec.expiry_date < today:
                trade.trade_phase = "closed_expired"
                if getattr(trade, "outcome", None) is None:
                    trade.outcome = "expired"
                    trade.outcome_at = now
                    trade.closing_price = current_price
                phase = "closed_expired"

            # Track extreme prices for trailing
            if direction == "long":
                if trade.highest_price is None or current_price > trade.highest_price:
                    trade.highest_price = current_price
            else:
                if trade.lowest_price is None or current_price < trade.lowest_price:
                    trade.lowest_price = current_price

            # Phase transitions
            if phase == "open" and risk > 0:
                # Check if we've reached 1R profit (reward = risk distance)
                if direction == "long" and current_price >= entry_price + risk:
                    trade.trade_phase = "partial_tp"
                    phase = "partial_tp"
                elif direction == "short" and current_price <= entry_price - risk:
                    trade.trade_phase = "partial_tp"
                    phase = "partial_tp"

            if phase == "partial_tp" and not trade.breakeven_hit:
                # Move SL to breakeven (entry price + small buffer)
                buffer = risk * 0.05  # 5% of risk as buffer above breakeven
                if direction == "long":
                    trade.breakeven_hit = True
                    # SL moves to entry + buffer
                    sl_breakeven = round(entry_price + buffer, 2)
                    if trade.suggested_sl is None or sl_breakeven > trade.suggested_sl:
                        trade.suggested_sl = sl_breakeven
                else:
                    trade.breakeven_hit = True
                    sl_breakeven = round(entry_price - buffer, 2)
                    if trade.suggested_sl is None or sl_breakeven < trade.suggested_sl:
                        trade.suggested_sl = sl_breakeven
                trade.trade_phase = "trailing"
                phase = "trailing"

            # ATR-based trailing (all phases). SL follows volatility from *current* price.
            # TP from compute_atr_levels is a fresh entry-style target — same for every leg on
            # this symbol, so it must NOT replace per-leg TPs from split / Fib strategies.
            # Leg labels: ``app.services.trade_strategies`` (STRATEGY_LEG_VANILLA vs STRATEGY_LEG_SPLIT_*,
            # THIRDS_*, FIB_*, RSTEP_*, etc. from ``legs_for_strategy``). Only VANILLA uses tp_atr here.
            sl, tp_atr = compute_atr_levels(current_price, rec.risk_tier, snapshot=snap, direction=direction)
            strat_leg = getattr(trade, "strategy_leg", None) or "vanilla"
            if strat_leg != "vanilla" and trade.current_tp is not None:
                tp = trade.current_tp
            else:
                tp = tp_atr

            # SL only moves in favorable direction
            if trade.suggested_sl is not None:
                if direction == "long":
                    sl = max(sl, trade.suggested_sl)
                else:
                    sl = min(sl, trade.suggested_sl)

            # Phase check: if current price has hit TP or SL, mark closed and log outcome
            if direction == "long":
                if current_price <= (trade.suggested_sl or original_sl):
                    trade.trade_phase = "closed_sl"
                    if getattr(trade, "outcome", None) is None:
                        trade.outcome = "hit_sl"
                        trade.outcome_at = now
                        trade.closing_price = current_price
                elif current_price >= trade.current_tp:
                    trade.trade_phase = "closed_tp"
                    if getattr(trade, "outcome", None) is None:
                        trade.outcome = "hit_tp"
                        trade.outcome_at = now
                        trade.closing_price = current_price
            else:
                if current_price >= (trade.suggested_sl or original_sl):
                    trade.trade_phase = "closed_sl"
                    if getattr(trade, "outcome", None) is None:
                        trade.outcome = "hit_sl"
                        trade.outcome_at = now
                        trade.closing_price = current_price
                elif current_price <= trade.current_tp:
                    trade.trade_phase = "closed_tp"
                    if getattr(trade, "outcome", None) is None:
                        trade.outcome = "hit_tp"
                        trade.outcome_at = now
                        trade.closing_price = current_price

            # Signal flip detection: re-evaluate direction from current data (regime-aware)
            pair = get_sentiment_score_and_count(rec.asset_category, rec.asset_id, by_asset_sentiment)
            current_sentiment = pair[0] if pair else None
            current_signal = determine_direction(current_sentiment, snap, regime=regime_id)
            flip_count = getattr(trade, "signal_flip_count", 0) or 0
            if current_signal != direction:
                trade.signal_flip_count = min(flip_count + 1, 10)
            else:
                trade.signal_flip_count = max(flip_count - 1, 0)

            # SL trails (trailing stop); TP stays fixed at the per-leg strategy target.
            # Overwriting suggested_tp with ATR from current price would make all legs
            # of a multi-leg strategy look identical (same ATR, same price, same risk tier).
            # The TP is a fixed exit target chosen at trade creation — don't override it.
            leg_tp = trade.current_tp  # per-leg strategy TP (set at creation, never changed)
            trade.suggested_sl = sl
            trade.suggested_tp = leg_tp  # keeps per-leg TP; no misleading arrow in UI
            trade.suggested_at = now
            trade.last_updated = now
            level = ActiveTradeLevel(
                active_trade_id=trade.id,
                sl=sl,
                tp=leg_tp,
                created_at=now,
                source=f"scheduled_update_{phase}",
            )
            session.add(level)
        await session.commit()

        # P9: copy outcomes + quality_score to llm_rationale_logs
        await _verify_outcomes_for_logs(session)
        await session.commit()


def _calculate_quality_score(
    outcome: str,
    validation_flags: list[str],
    retry_count: int,
    pnl_r: float | None,
    metrics_accuracy_score: float | None = None,
    news_verified: bool | None = None,
) -> int:
    score = 5  # baseline

    if outcome == "hit_tp":
        score += 3
    elif outcome == "hit_sl":
        score -= 3
    # expired: no change

    if metrics_accuracy_score is not None:
        if metrics_accuracy_score >= 0.85:
            score += 2
        elif metrics_accuracy_score >= 0.65:
            score += 1
        elif metrics_accuracy_score < 0.50:
            score -= 2

    if news_verified is True:
        score += 1
    elif news_verified is False:
        score -= 1

    for flag in validation_flags:
        if flag in ("V3_direction_conflict", "V4a_price_hallucination", "V4b_indicator_hallucination"):
            score -= 2
        elif flag in ("V4c_news_hallucination", "V4d_temporal_hallucination", "V5_too_generic"):
            score -= 1

    score -= retry_count

    if outcome == "hit_tp" and pnl_r is not None and pnl_r >= 2.0:
        score += 1

    return max(0, min(10, score))


async def _verify_outcomes_for_logs(session: AsyncSession) -> None:
    """
    P9: For every closed ActiveTrade that has a linked LLMRationaleLog without an outcome,
    copy outcome data and calculate quality_score.
    Runs at the end of job_update_active_trades.
    """
    from app.models.llm_rationale_log import LLMRationaleLog
    from sqlalchemy import and_

    try:
        # Find closed trades with unresolved rationale logs
        q = (
            select(ActiveTrade, Recommendation)
            .join(Recommendation, ActiveTrade.recommendation_id == Recommendation.id)
            .where(ActiveTrade.outcome.isnot(None))
        )
        rows = (await session.execute(q)).all()

        if not rows:
            return

        rec_ids = list({rec.id for _, rec in rows})

        # Load all unresolved logs for these recommendations in one query
        logs_q = (
            select(LLMRationaleLog)
            .where(
                LLMRationaleLog.recommendation_id.in_(rec_ids),
                LLMRationaleLog.outcome.is_(None),
            )
        )
        logs = (await session.execute(logs_q)).scalars().all()

        if not logs:
            return

        # Build lookup: recommendation_id → (trade, rec)
        trade_by_rec: dict[str, tuple] = {}
        for trade, rec in rows:
            if rec.id not in trade_by_rec:
                trade_by_rec[rec.id] = (trade, rec)

        updated = 0
        for log in logs:
            pair = trade_by_rec.get(log.recommendation_id)
            if not pair:
                continue
            trade, rec = pair

            entry_price = rec.buy_in_price or 0.0
            original_sl = trade.current_sl or 0.0
            closing_price = trade.closing_price or 0.0
            direction = getattr(rec, "direction", "long") or "long"
            risk = abs(entry_price - original_sl)

            pnl_r: float | None = None
            if risk > 0 and closing_price:
                if direction == "long":
                    pnl_r = round((closing_price - entry_price) / risk, 3)
                else:
                    pnl_r = round((entry_price - closing_price) / risk, 3)

            # P12: verify metrics claims if metrics_at_entry is available
            metrics_accuracy: float | None = log.metrics_accuracy_score
            metrics_verification: str | None = log.metrics_verification
            if log.metrics_at_entry and metrics_accuracy is None:
                try:
                    from app.services.research.hallucination import verify_metrics_claims
                    import json as _json
                    metrics_dict = log.get_metrics_at_entry()
                    if metrics_dict:
                        mc_result = verify_metrics_claims(log.rationale_text, metrics_dict)
                        metrics_accuracy = mc_result.get("accuracy_score")
                        metrics_verification = _json.dumps(mc_result, default=str)
                except Exception as _me:
                    print(f"[VERIFY_OUTCOMES] metrics claims check failed (non-fatal): {_me}")

            flags = log.get_validation_flags()
            quality = _calculate_quality_score(
                outcome=trade.outcome,
                validation_flags=flags,
                retry_count=log.retry_count or 0,
                pnl_r=pnl_r,
                metrics_accuracy_score=metrics_accuracy,
                news_verified=log.news_verified,
            )

            log.outcome = trade.outcome
            log.outcome_at = trade.outcome_at
            log.closing_price = closing_price or None
            log.pnl_r = pnl_r
            log.metrics_accuracy_score = metrics_accuracy
            log.metrics_verification = metrics_verification
            log.quality_score = quality
            updated += 1

        if updated:
            await session.flush()
            print(f"[VERIFY_OUTCOMES] Updated {updated} rationale log(s) with outcome data")
    except Exception as exc:
        print(f"[VERIFY_OUTCOMES] Failed (non-fatal): {exc}")


async def job_build_hallucination_profile() -> None:
    """P13: Weekly aggregation of V4 flag rates per provider/model."""
    async with async_session_factory() as session:
        try:
            from app.services.research.bias_detector import build_hallucination_profile
            profile = await build_hallucination_profile(session, days_back=30)
            entries = len(profile.get("entries") or [])
            print(f"[HALLUCINATION_PROFILE] Built profile with {entries} provider/model entries")
        except Exception as exc:
            print(f"[HALLUCINATION_PROFILE] Failed (non-fatal): {exc}")


async def job_daily_briefing() -> None:
    """Run daily briefing checklist and store result."""
    async with async_session_factory() as session:
        await run_briefing(session)
        await session.commit()


async def job_cleanup_feed_items() -> None:
    """Delete feed_items older than feed_max_age_days."""
    async with async_session_factory() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.feed_max_age_days)
        await session.execute(delete(FeedItem).where(FeedItem.fetched_at < cutoff))
        await session.commit()


async def job_refresh_backtest_stats() -> None:
    """
    Run a full backtest for all base universe symbols (last 3 years).
    Runs on startup and weekly (Sunday 05:00) to keep backtest_trades
    up-to-date so live confidence scores are statistically grounded.
    """
    import json as _json
    from sqlalchemy import update
    from app.live_trading.live_analyzer import _BASE_UNIVERSE
    from app.api.backtest_routes import _run_backtest_background, BacktestRequest
    from app.models.backtest import BacktestRun

    base_symbols = [yf for _, yf, _, _ in _BASE_UNIVERSE]
    end = date.today()
    start = end - timedelta(days=3 * 365)

    try:
        async with async_session_factory() as session:
            now = datetime.now(timezone.utc)
            stale_cutoff = now - timedelta(hours=6)

            # Check 1: mark orphaned "running" rows as failed (container crashed mid-run)
            await session.execute(
                update(BacktestRun)
                .where(BacktestRun.status == "running", BacktestRun.created_at < stale_cutoff)
                .values(status="failed", error_message="Marked failed on restart — container was restarted mid-run")
            )
            await session.commit()

            # Check 2: skip if a backtest is actively running (started within last 6h)
            active = await session.execute(
                select(BacktestRun.id).where(
                    BacktestRun.status == "running",
                    BacktestRun.created_at >= stale_cutoff,
                ).limit(1)
            )
            if active.scalar_one_or_none():
                print("[BACKTEST_STATS] Backtest already in progress — skipping")
                return

            run_id = str(uuid4())
            session.add(BacktestRun(
                id=run_id,
                created_at=now,
                start_date=start,
                end_date=end,
                symbols_json=_json.dumps(base_symbols),
                risk_tier="moderate",
                min_confidence=30,
                status="running",
            ))
            await session.commit()

        body = BacktestRequest(
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            symbols=base_symbols,
            risk_tier="moderate",
            min_confidence=30,
            period="3y",
        )
        asyncio.create_task(_run_backtest_background(run_id, body))
        print(f"[BACKTEST_STATS] Auto-backtest started run_id={run_id} for {len(base_symbols)} symbols")

    except Exception as exc:
        print(f"[BACKTEST_STATS] Failed (non-fatal): {exc}")


def start_scheduler() -> AsyncIOScheduler:
    """Schedule jobs."""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        job_ingest_and_sentiment,
        "interval",
        hours=settings.analysis_run_interval_hours,
        id="ingest_sentiment",
    )
    scheduler.add_job(
        job_run_analysis,
        "interval",
        hours=settings.analysis_run_interval_hours,
        id="run_analysis",
    )
    scheduler.add_job(
        job_daily_batch,
        "cron",
        hour=settings.daily_batch_hour,
        minute=0,
        id="daily_batch",
    )
    briefing_hour = min(23, (settings.daily_batch_hour + 1) % 24)
    scheduler.add_job(
        job_daily_briefing,
        "cron",
        hour=briefing_hour,
        minute=5,
        id="daily_briefing",
    )
    scheduler.add_job(
        job_update_active_trades,
        "interval",
        hours=settings.update_active_trades_interval_hours,
        id="update_active_trades",
    )
    scheduler.add_job(
        job_cleanup_feed_items,
        "cron",
        hour=4,
        minute=0,
        id="cleanup_feed_items",
    )
    scheduler.add_job(
        job_build_hallucination_profile,
        "cron",
        day_of_week="sun",
        hour=6,
        minute=0,
        id="build_hallucination_profile",
    )
    # Backtest stats: startup auto-run DISABLED (manual backtest work in progress)
    # scheduler.add_job(
    #     job_refresh_backtest_stats,
    #     "date",
    #     run_date=datetime.now(timezone.utc) + timedelta(seconds=60),
    #     id="backtest_stats_startup",
    # )
    scheduler.add_job(
        job_refresh_backtest_stats,
        "cron",
        day_of_week="sun",
        hour=5,
        minute=0,
        id="backtest_stats_weekly",
    )
    scheduler.start()
    return scheduler
