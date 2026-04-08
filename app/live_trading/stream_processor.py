"""
Data Stream Processor — Live Trading.

Flow:
  1. Math engine (live_analyzer) computes signals for 35+ assets using the same
     quantitative logic as the main recommendations engine.
  2. LLM receives the math signals and returns for each:
       - trade_type (scalp/intraday/swing)
       - rationale (2-sentence explanation)
       - llm_sl / llm_tp (LLM-adjusted levels, may differ from ATR math)
  3. Both math SL/TP and LLM SL/TP are stored and shown in the UI.

Trigger modes:
  manual_button       — user presses Analyze Market (no gate, always runs)
  pre_market_warmup   — auto at 08:30 ET transition
  news_refresh        — new news article, debounced 3 min, gated by analysis_allowed()
  ticker_watch        — news matching selected ticker, debounced 5 min per ticker, gated
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_COST_PER_1M: dict[str, float] = {
    "claude-opus-4-6":   15.0,
    "claude-sonnet-4-6":  3.0,
    "gpt-4o":             5.0,
    "gpt-4o-mini":        0.15,
    "default":            5.0,
}

_snap_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="live_llm")


# ── LLM config helpers ────────────────────────────────────────────────────────

def _get_llm_config() -> tuple[str, str, str | None]:
    from app.core.preferences import get_research_llm_config
    from app.core.config import settings
    cfg = get_research_llm_config()
    provider = cfg.get("provider") or settings.research_llm_provider or "anthropic"
    model = cfg.get("model") or ""
    if not model:
        from app.services.research.providers import get_provider_by_id
        p = get_provider_by_id(provider)
        model = p.default_model if p else "claude-opus-4-6"
    api_key = None
    if provider == "anthropic":
        api_key = settings.anthropic_api_key
    elif provider == "openai":
        api_key = settings.openai_api_key
    elif provider == "openrouter":
        api_key = settings.openrouter_api_key
    return provider, model, api_key


async def _call_llm(system: str, user: str, provider: str, model: str, api_key: str | None) -> tuple[str, int, int]:
    def _sync():
        if provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model=model,
                max_tokens=8192,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = msg.content[0].text if msg.content else ""
            return text, msg.usage.input_tokens, msg.usage.output_tokens

        elif provider == "openai":
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=8192,
                response_format={"type": "json_object"},
            )
            text = resp.choices[0].message.content or ""
            u = resp.usage
            return text, u.prompt_tokens, u.completion_tokens

        elif provider == "ollama":
            import requests
            from app.core.config import settings as _s
            base = getattr(_s, "ollama_base_url", None) or "http://localhost:11434"
            resp = requests.post(
                f"{base}/api/chat",
                json={"model": model, "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ], "stream": False, "format": "json"},
                timeout=180,
            )
            resp.raise_for_status()
            return resp.json().get("message", {}).get("content", ""), 0, 0

        elif provider == "openrouter":
            import requests
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ], "max_tokens": 8192},
                timeout=180,
            )
            resp.raise_for_status()
            data = resp.json()
            u = data.get("usage", {})
            return data["choices"][0]["message"]["content"], u.get("prompt_tokens", 0), u.get("completion_tokens", 0)

        return "", 0, 0

    return await asyncio.to_thread(_sync)


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rate = _COST_PER_1M.get(model, _COST_PER_1M["default"])
    return round((prompt_tokens + completion_tokens) / 1_000_000 * rate, 6)


# ── News compression ──────────────────────────────────────────────────────────

def _compress_news(news_items: list[dict]) -> str:
    top = sorted(news_items, key=lambda x: abs(x.get("sentiment_score") or 0), reverse=True)[:10]
    lines = []
    for item in top:
        score = item.get("sentiment_score") or 0
        sign = "+" if score > 0 else ""
        source = (item.get("source") or "")[:30]
        title = (item.get("title") or "")[:100]
        lines.append(f"[{source}] {title} ({sign}{score:.2f})")
    return "\n".join(lines) if lines else "No recent news available."


# ── LLM prompts ───────────────────────────────────────────────────────────────

_ANALYSIS_SYSTEM = """\
You are a senior quantitative trader. You receive math-generated trading signals
(direction, entry, ATR-based SL/TP, confidence score). Your job is to analyze
each signal in the context of current news and market regime, and return:

  trade_type : "scalp" (<4h), "intraday" (same day), or "swing" (2-5 days)
  rationale  : 1-2 sentences — WHY this trade makes sense given news + regime
  llm_sl     : your stop loss estimate (adjust ATR level if key news/levels suggest it)
  llm_tp     : your take profit estimate (same logic)
  catalysts  : list of 1-3 short strings (e.g. ["risk_off_regime", "safe_haven_bid"])

Return ONLY valid JSON (no markdown, no extra text):
{
  "analyses": [
    {
      "ticker": "GC=F",
      "trade_type": "swing",
      "rationale": "Gold bid as safe haven...",
      "llm_sl": 4760.0,
      "llm_tp": 4920.0,
      "catalysts": ["safe_haven_demand", "risk_off_regime"]
    }
  ]
}"""

_TICKER_WATCH_SYSTEM = """\
You are a senior quantitative trader re-assessing an open position after new market developments.
Be concise and data-driven. Return ONLY valid JSON, no markdown."""


# ── Main processor class ──────────────────────────────────────────────────────

class StreamProcessor:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active_run_id: str | None = None
        self._last_news_refresh: datetime | None = None
        self._last_ticker_watch: dict[str, datetime] = {}
        self._news_refresh_debounce = timedelta(minutes=3)
        self._ticker_watch_debounce = timedelta(minutes=5)
        self._engine = None

    def set_engine(self, eng) -> None:
        self._engine = eng
        eng.set_news_callback(self._on_news_event)
        eng.set_warmup_callback(self._on_warmup_trigger)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    async def _on_news_event(self, item: dict) -> None:
        from app.live_trading.stream_engine import analysis_allowed, ws_manager
        from app.live_trading.trade_watcher import watcher

        watchlist = self._engine.watchlist if self._engine else []

        selected = watcher.get()
        if selected:
            ticker = selected.get("ticker", "")
            asset_tags = []
            raw = item.get("asset_tags") or "[]"
            try:
                asset_tags = json.loads(raw) if isinstance(raw, str) else (raw or [])
            except Exception:
                pass
            ticker_match = any(
                ticker.upper() in str(tag).upper() or str(tag).upper() in ticker.upper()
                for tag in asset_tags
            )
            if ticker_match:
                last = self._last_ticker_watch.get(ticker)
                if not last or (datetime.now(timezone.utc) - last) > self._ticker_watch_debounce:
                    if analysis_allowed(watchlist):
                        self._last_ticker_watch[ticker] = datetime.now(timezone.utc)
                        asyncio.create_task(self.run_ticker_watch(
                            ticker=ticker,
                            selected=selected,
                            headline=(item.get("title") or "")[:200],
                        ))

        last_refresh = self._last_news_refresh
        if not last_refresh or (datetime.now(timezone.utc) - last_refresh) > self._news_refresh_debounce:
            if analysis_allowed(watchlist) and not self._lock.locked():
                self._last_news_refresh = datetime.now(timezone.utc)
                asyncio.create_task(self.run_full_analysis(trigger="news_refresh"))

    async def _on_warmup_trigger(self) -> None:
        if not self._lock.locked():
            asyncio.create_task(self.run_full_analysis(trigger="pre_market_warmup"))

    # ── Full analysis (math + LLM) ────────────────────────────────────────────

    async def run_full_analysis(self, trigger: str = "manual_button") -> str | None:
        from app.live_trading.stream_engine import ws_manager, get_market_state
        from app.live_trading.live_analyzer import build_all_signals

        if self._lock.locked():
            logger.info("[PROCESSOR] Already running, skipping trigger=%s", trigger)
            return self._active_run_id

        async with self._lock:
            run_id = str(uuid.uuid4())
            self._active_run_id = run_id
            watchlist = self._engine.watchlist if self._engine else []
            state = get_market_state(watchlist)

            await ws_manager.broadcast({"type": "analysis_started", "run_id": run_id, "trigger": trigger})
            logger.info("[PROCESSOR] Analysis started run_id=%s trigger=%s", run_id, trigger)

            try:
                # ── Step 1: Math signals ──────────────────────────────────────
                signals = await build_all_signals(min_confidence=45)
                logger.info("[PROCESSOR] Math signals: %d", len(signals))

                if not signals:
                    await ws_manager.broadcast({"type": "analysis_error", "run_id": run_id, "error": "No signals above confidence threshold"})
                    return None

                # ── Step 2: Fetch news context ────────────────────────────────
                news_items = await self._fetch_recent_news(limit=50)
                news_text = _compress_news(news_items)

                regime_summary = await asyncio.to_thread(self._get_regime_summary)
                prices = self._engine.latest_prices if self._engine else {}

                # ── Step 3: LLM analysis prompt ───────────────────────────────
                signals_text = json.dumps([
                    {
                        "ticker":      s["ticker"],
                        "category":    s["category"],
                        "direction":   s["direction"],
                        "entry_price": s["entry_price"],
                        "math_sl":     s["math_sl"],
                        "math_tp":     s["math_tp"],
                        "confidence":  s["confidence"],
                        "risk_reward": s["risk_reward"],
                        "regime":      s["regime"],
                        "sentiment_avg":   s["sentiment_avg"],
                        "sentiment_count": s["sentiment_count"],
                    }
                    for s in signals
                ], indent=2)

                user_msg = (
                    f"Market session: {state}\n"
                    f"Regime: {regime_summary}\n\n"
                    f"Recent news:\n{news_text}\n\n"
                    f"Math-generated signals ({len(signals)} total):\n{signals_text}\n\n"
                    "Analyze each signal above. For each ticker return: trade_type, rationale, llm_sl, llm_tp, catalysts."
                )

                provider, model, api_key = _get_llm_config()
                raw, prompt_tokens, completion_tokens = await _call_llm(
                    _ANALYSIS_SYSTEM, user_msg, provider, model, api_key
                )

                # ── Step 4: Parse LLM analyses ────────────────────────────────
                llm_by_ticker = self._parse_analysis_response(raw)
                cost = _estimate_cost(model, prompt_tokens, completion_tokens)

                # ── Step 5: Merge math + LLM ──────────────────────────────────
                merged = self._merge_signals_with_llm(signals, llm_by_ticker)

                # Group by category
                recs_by_cat: dict[str, list] = {}
                for sig in merged:
                    cat = sig["category"]
                    recs_by_cat.setdefault(cat, []).append(sig)

                context_snapshot = {
                    "trigger": trigger,
                    "market_session": state,
                    "regime": regime_summary,
                    "news_count": len(news_items),
                    "signal_count": len(signals),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }

                await self._save_run(
                    run_id=run_id,
                    trigger=trigger,
                    market_session=state,
                    provider=provider,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost=cost,
                    raw=raw,
                    context=context_snapshot,
                    parsed_ok=bool(llm_by_ticker),
                    recommendations=merged,
                )

                await ws_manager.broadcast({
                    "type": "batch_ready",
                    "run_id": run_id,
                    "trigger": trigger,
                    "categories": list(recs_by_cat.keys()),
                    "total": len(merged),
                    "estimated_cost_usd": cost,
                })
                logger.info("[PROCESSOR] Done run_id=%s total=%d cost=$%.4f", run_id, len(merged), cost)
                return run_id

            except Exception as exc:
                logger.error("[PROCESSOR] Analysis failed run_id=%s: %s", run_id, exc, exc_info=True)
                await ws_manager.broadcast({"type": "analysis_error", "run_id": run_id, "error": str(exc)})
                return None
            finally:
                self._active_run_id = None

    # ── Ticker watch analysis ─────────────────────────────────────────────────

    async def run_ticker_watch(self, ticker: str, selected: dict, headline: str) -> None:
        from app.live_trading.stream_engine import ws_manager
        from app.live_trading.trade_watcher import watcher
        from app.live_trading.live_analyzer import _compute_signal_sync, _aggregate_live_sentiment_sync

        logger.info("[PROCESSOR] Ticker watch %s triggered by: %s", ticker, headline[:60])
        try:
            from app.live_trading.live_analyzer import (
                _compute_signal_sync, _snap_executor, _aggregate_live_sentiment_sync
            )
            # Recompute math signal
            sent_data = await asyncio.to_thread(_aggregate_live_sentiment_sync)
            tag_info = sent_data.get(ticker) or {}
            snap_dict: dict = {}
            try:
                sig = await asyncio.get_event_loop().run_in_executor(
                    _snap_executor,
                    _compute_signal_sync,
                    ticker, ticker, "moderate", "stocks",
                    float(tag_info.get("avg", 0.0)),
                    int(tag_info.get("count", 0)),
                    "cautious",
                )
                if sig:
                    snap_dict = sig
            except Exception:
                pass

            price_info = (self._engine.latest_prices if self._engine else {}).get(ticker, {})

            user_msg = (
                f"Ticker: {ticker}\n"
                f"News: {headline}\n\n"
                f"Current position:\n"
                f"  Direction: {selected.get('direction', 'long')}\n"
                f"  Entry: {selected.get('entry_price')}\n"
                f"  Stop Loss: {selected.get('stop_loss')}\n"
                f"  Take Profit: {selected.get('take_profit')}\n"
                f"  Previous confidence: {selected.get('confidence')}%\n\n"
                f"Current market data:\n"
                f"  Price: {price_info.get('price')}\n"
                f"  Change: {price_info.get('change_pct')}%\n"
                f"  Math signal: {json.dumps(snap_dict)}\n\n"
                "Re-assess the trade given this new development.\n"
                "Return ONLY valid JSON:\n"
                '{"confidence": <0-100>, "direction_flip": <true/false>, '
                '"action": "<hold|tighten_sl|exit|add>", '
                '"suggested_sl": <float or null>, "suggested_tp": <float or null>, '
                '"assessment": "<one sentence>"}'
            )

            provider, model, api_key = _get_llm_config()
            raw, _, _ = await _call_llm(_TICKER_WATCH_SYSTEM, user_msg, provider, model, api_key)

            result = self._parse_ticker_watch_response(raw)
            if not result:
                return

            watcher.update_from_analysis(result)
            await self._save_trade_update(
                ticker=ticker,
                headline=headline,
                confidence_before=selected.get("confidence"),
                result=result,
            )
            await ws_manager.broadcast({
                "type": "analysis_update",
                "ticker": ticker,
                "confidence": result.get("confidence"),
                "direction_flip": result.get("direction_flip", False),
                "action": result.get("action", "hold"),
                "suggested_sl": result.get("suggested_sl"),
                "suggested_tp": result.get("suggested_tp"),
                "assessment": result.get("assessment", ""),
                "trigger_headline": headline,
            })
        except Exception as exc:
            logger.warning("[PROCESSOR] Ticker watch error for %s: %s", ticker, exc)

    # ── Parse helpers ─────────────────────────────────────────────────────────

    def _parse_analysis_response(self, raw: str) -> dict[str, dict]:
        """Parse LLM analysis response. Returns {ticker: analysis_dict}."""
        def _try_parse(text: str) -> dict[str, dict]:
            data = json.loads(text)
            analyses = data.get("analyses") or []
            return {a["ticker"]: a for a in analyses if isinstance(a, dict) and "ticker" in a}

        try:
            return _try_parse(raw)
        except Exception:
            try:
                start = raw.index("{")
                end = raw.rindex("}") + 1
                return _try_parse(raw[start:end])
            except Exception:
                logger.warning("[PROCESSOR] Failed to parse LLM analysis response")
                return {}

    def _merge_signals_with_llm(self, signals: list[dict], llm_by_ticker: dict[str, dict]) -> list[dict]:
        """Merge math signals with LLM analyses."""
        merged = []
        for sig in signals:
            ticker = sig["ticker"]
            llm = llm_by_ticker.get(ticker) or {}
            merged.append({
                **sig,
                "llm_sl":       llm.get("llm_sl"),
                "llm_tp":       llm.get("llm_tp"),
                "trade_type":   llm.get("trade_type") or "swing",
                "rationale":    llm.get("rationale") or "",
                "catalysts":    llm.get("catalysts") or [],
            })
        return merged

    def _parse_ticker_watch_response(self, raw: str) -> dict | None:
        try:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            return json.loads(raw[start:end])
        except Exception:
            logger.warning("[PROCESSOR] Failed to parse ticker watch response")
            return None

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _fetch_recent_news(self, limit: int = 50) -> list[dict]:
        try:
            from app.core.database import AsyncSessionLocal
            from app.live_trading.models import LiveNewsItem
            from sqlalchemy import select, desc
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(LiveNewsItem)
                    .where(LiveNewsItem.fetched_at >= cutoff)
                    .order_by(desc(LiveNewsItem.fetched_at))
                    .limit(limit)
                )
                return [
                    {"title": r.title, "source": r.source, "sentiment_score": r.sentiment_score or 0.0}
                    for r in result.scalars().all()
                ]
        except Exception:
            return []

    async def _save_run(
        self,
        run_id: str,
        trigger: str,
        market_session: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: float,
        raw: str,
        context: dict,
        parsed_ok: bool,
        recommendations: list[dict],
    ) -> None:
        try:
            from app.core.database import AsyncSessionLocal
            from app.live_trading.models import LiveAnalysisRun, LiveRecommendation
            now = datetime.now(timezone.utc)
            async with AsyncSessionLocal() as session:
                session.add(LiveAnalysisRun(
                    id=run_id,
                    created_at=now,
                    trigger=trigger,
                    market_session=market_session,
                    provider=provider,
                    model=model,
                    llm_prompt_tokens=prompt_tokens,
                    llm_response_tokens=completion_tokens,
                    estimated_cost_usd=cost,
                    raw_response=raw[:50000] if raw else None,
                    context_snapshot_json=json.dumps(context)[:50000],
                    parsed_ok=parsed_ok,
                ))
                for sig in recommendations:
                    session.add(LiveRecommendation(
                        id=str(uuid.uuid4()),
                        run_id=run_id,
                        created_at=now,
                        asset_category=sig.get("category", "stocks"),
                        ticker=str(sig.get("ticker", ""))[:32],
                        direction=str(sig.get("direction", "long"))[:8],
                        entry_price=sig.get("entry_price"),
                        stop_loss=sig.get("math_sl"),
                        take_profit=sig.get("math_tp"),
                        llm_stop_loss=sig.get("llm_sl"),
                        llm_take_profit=sig.get("llm_tp"),
                        confidence=sig.get("confidence"),
                        risk_reward=sig.get("risk_reward"),
                        rationale=str(sig.get("rationale") or "")[:2000],
                        timeframe=str(sig.get("trade_type") or "swing")[:16],
                        catalysts=json.dumps(sig.get("catalysts") or []),
                        market_session=market_session,
                    ))
                await session.commit()
        except Exception as exc:
            logger.warning("[PROCESSOR] Failed to save run: %s", exc)

    async def _save_trade_update(self, ticker: str, headline: str, confidence_before: int | None, result: dict) -> None:
        try:
            from app.core.database import AsyncSessionLocal
            from app.live_trading.models import LiveTradeUpdate
            async with AsyncSessionLocal() as session:
                session.add(LiveTradeUpdate(
                    id=str(uuid.uuid4()),
                    created_at=datetime.now(timezone.utc),
                    ticker=ticker,
                    trigger_headline=headline[:500] if headline else None,
                    confidence_before=confidence_before,
                    confidence_after=result.get("confidence"),
                    direction_flip=bool(result.get("direction_flip", False)),
                    suggested_sl=result.get("suggested_sl"),
                    suggested_tp=result.get("suggested_tp"),
                    assessment=str(result.get("assessment", ""))[:1000],
                    action_advised=str(result.get("action", "hold"))[:32],
                ))
                await session.commit()
        except Exception as exc:
            logger.warning("[PROCESSOR] Failed to save trade update: %s", exc)

    @staticmethod
    def _get_regime_summary() -> str:
        try:
            from app.services.market_context import detect_market_regime
            regime = detect_market_regime()
            return f"{regime.regime} | SPX: {regime.spx_trend} | VIX: {regime.vix_level}"
        except Exception:
            return "unknown"

    async def get_daily_cost(self) -> float:
        try:
            from app.core.database import AsyncSessionLocal
            from app.live_trading.models import LiveAnalysisRun
            from sqlalchemy import select, func
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(func.sum(LiveAnalysisRun.estimated_cost_usd))
                    .where(LiveAnalysisRun.created_at >= today_start)
                )
                return round(float(result.scalar() or 0), 4)
        except Exception:
            return 0.0


# ── Module-level singleton ────────────────────────────────────────────────────

processor = StreamProcessor()
