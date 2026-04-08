"""
Data Stream Processor — Live Trading.

Handles all LLM analysis calls for the Live Trading feature.

Trigger modes:
  manual_button       — user presses Analyze Market (no gate, always runs)
  pre_market_warmup   — auto at 08:30 ET transition
  news_refresh        — any new news article, debounced 3 min, gated by analysis_allowed()
  ticker_watch        — news matching selected ticker, debounced 5 min per ticker, gated

Key design:
  - asyncio.Lock prevents concurrent LLM calls
  - Context compressed before LLM: top-10 news by |sentiment|, lightweight technicals
  - All snapshot builds run in parallel via ThreadPoolExecutor + 60s cache
  - Cost tracked per run (prompt_tokens × rate estimate)
  - Full context snapshot saved to DB for debugging
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

# Token cost estimates per 1M tokens (input) as of 2026
_COST_PER_1M: dict[str, float] = {
    "claude-opus-4-6": 15.0,
    "claude-sonnet-4-6": 3.0,
    "gpt-4o": 5.0,
    "gpt-4o-mini": 0.15,
    "default": 5.0,
}

_snap_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="live_snap")
_snap_cache: dict[str, tuple[Any, datetime]] = {}   # ticker → (snapshot, fetched_at)
_SNAP_TTL = 60  # seconds


# ── Snapshot helpers ──────────────────────────────────────────────────────────

async def _get_snapshot(ticker: str) -> Any | None:
    cached = _snap_cache.get(ticker)
    if cached:
        snap, ts = cached
        if (datetime.now(timezone.utc) - ts).total_seconds() < _SNAP_TTL:
            return snap
    loop = asyncio.get_event_loop()
    try:
        from app.services.technicals import build_snapshot
        snap = await loop.run_in_executor(_snap_executor, build_snapshot, ticker)
        if snap:
            _snap_cache[ticker] = (snap, datetime.now(timezone.utc))
        return snap
    except Exception:
        return None


async def _build_snapshots(tickers: list[str]) -> dict[str, Any]:
    results = await asyncio.gather(*[_get_snapshot(t) for t in tickers], return_exceptions=True)
    return {t: s for t, s in zip(tickers, results) if s and not isinstance(s, Exception)}


def _snap_to_dict(snap) -> dict:
    try:
        return {
            "price": snap.price,
            "atr_pct": round(snap.atr_pct * 100, 2) if snap.atr_pct else None,
            "rsi": round(snap.rsi, 1) if snap.rsi else None,
            "macd_bullish": snap.macd_bullish,
            "adx": round(snap.adx, 1) if snap.adx else None,
            "above_vwap": snap.price_above_vwap,
            "vp_bias": snap.vp_bias,
        }
    except Exception:
        return {}


def _compress_news(news_items: list[dict]) -> str:
    """Top 10 news by |sentiment| → compact text lines for LLM context."""
    top = sorted(news_items, key=lambda x: abs(x.get("sentiment_score") or 0), reverse=True)[:10]
    lines = []
    for item in top:
        score = item.get("sentiment_score") or 0
        sign = "+" if score > 0 else ""
        source = (item.get("source") or "unknown").split("/")[-1][:30]
        title = (item.get("title") or "")[:100]
        lines.append(f"[{source}] {title} (sentiment: {sign}{score:.2f})")
    return "\n".join(lines) if lines else "No recent news available."


# ── LLM dispatch helpers ──────────────────────────────────────────────────────

def _get_llm_config() -> tuple[str, str, str | None]:
    """Returns (provider, model, api_key)."""
    from app.core.preferences import get_research_llm_config
    from app.core.config import settings
    cfg = get_research_llm_config()
    provider = cfg.get("provider") or settings.research_llm_provider or "anthropic"
    model = cfg.get("model") or ""
    # Resolve model default
    if not model:
        from app.services.research.providers import get_provider_by_id
        p = get_provider_by_id(provider)
        model = p.default_model if p else "claude-opus-4-6"
    # Resolve API key
    api_key = None
    if provider == "anthropic":
        api_key = settings.anthropic_api_key
    elif provider == "openai":
        api_key = settings.openai_api_key
    elif provider == "openrouter":
        api_key = settings.openrouter_api_key
    return provider, model, api_key


async def _call_llm(system: str, user: str, provider: str, model: str, api_key: str | None) -> tuple[str, int, int]:
    """
    Calls the configured LLM provider. Returns (response_text, prompt_tokens, completion_tokens).
    Runs the blocking SDK call via asyncio.to_thread.
    """
    def _sync_call():
        if provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model=model,
                max_tokens=2048,
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
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=2048,
                response_format={"type": "json_object"},
            )
            text = resp.choices[0].message.content or ""
            usage = resp.usage
            return text, usage.prompt_tokens, usage.completion_tokens

        elif provider == "ollama":
            import requests
            from app.core.config import settings as _s
            base = getattr(_s, "ollama_base_url", None) or "http://localhost:11434"
            resp = requests.post(
                f"{base}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                    "format": "json",
                },
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data.get("message", {}).get("content", "")
            return text, 0, 0

        elif provider == "openrouter":
            import requests
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "max_tokens": 2048,
                },
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            return text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)

        return "", 0, 0

    return await asyncio.to_thread(_sync_call)


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rate = _COST_PER_1M.get(model, _COST_PER_1M["default"])
    return round((prompt_tokens + completion_tokens) / 1_000_000 * rate, 6)


# ── System prompts ────────────────────────────────────────────────────────────

_FULL_BATCH_SYSTEM = """\
You are a senior quantitative trader and financial analyst.

Your skills:
- Technical analysis: ATR-based stop placement, multi-timeframe trend alignment, RSI divergence, MACD momentum, Bollinger Band breakouts, volume profile analysis
- Quantitative methods: Kelly Criterion position sizing, R-multiples, volatility regime classification, Sharpe ratio estimation
- Trading strategies: trend-following, mean-reversion, news-momentum, breakout confirmation
- Risk management: position sizing per conviction, correlated risk, portfolio heat

Given the market context provided, identify the BEST 6 trading opportunities for EACH of these categories:
stocks, commodities, etfs, fx, crypto, indices

Rules:
- Only include if estimated win probability > 50% (confidence field)
- Calculate ATR-based stop loss ensuring R:R >= 1.5
- State primary catalyst (technical or fundamental)
- Classify timeframe: scalp (<4h), intraday (1d), swing (2-5d)
- Use current prices from the context provided; do not invent prices
- If fewer than 6 strong setups exist for a category, return only the ones that qualify

Return ONLY valid JSON with this exact structure (no markdown, no extra text):
{
  "recommendations": {
    "stocks": [{"ticker": "AAPL", "direction": "long", "entry_price": 185.20, "stop_loss": 181.40, "take_profit": 194.60, "confidence": 68, "risk_reward": 2.5, "timeframe": "swing", "rationale": "2-3 sentence explanation", "catalysts": ["volume_breakout"]}],
    "commodities": [...],
    "etfs": [...],
    "fx": [...],
    "crypto": [...],
    "indices": [...]
  }
}"""

_TICKER_WATCH_SYSTEM = """\
You are a senior quantitative trader re-assessing an open position after new market developments.
Be concise and data-driven. Return ONLY valid JSON, no markdown."""


# ── Main processor class ──────────────────────────────────────────────────────

class StreamProcessor:
    """
    Handles all LLM analysis for Live Trading.
    Single asyncio.Lock prevents concurrent calls.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active_run_id: str | None = None
        # Debounce: last auto-trigger times
        self._last_news_refresh: datetime | None = None
        self._last_ticker_watch: dict[str, datetime] = {}
        self._news_refresh_debounce = timedelta(minutes=3)
        self._ticker_watch_debounce = timedelta(minutes=5)
        # Reference to engine (injected after init)
        self._engine = None

    def set_engine(self, eng) -> None:
        self._engine = eng
        eng.set_news_callback(self._on_news_event)
        eng.set_warmup_callback(self._on_warmup_trigger)

    # ── Callbacks from engine ─────────────────────────────────────────────────

    async def _on_news_event(self, item: dict) -> None:
        """Called by engine for every new article."""
        from app.live_trading.stream_engine import analysis_allowed, ws_manager
        from app.live_trading.trade_watcher import watcher

        watchlist = self._engine.watchlist if self._engine else []

        # Check if this news matches the selected trade → ticker_watch
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
                        asyncio.create_task(
                            self.run_ticker_watch(
                                ticker=ticker,
                                selected=selected,
                                headline=(item.get("title") or "")[:200],
                            )
                        )

        # news_refresh full batch — debounced 3 min
        last_refresh = self._last_news_refresh
        if not last_refresh or (datetime.now(timezone.utc) - last_refresh) > self._news_refresh_debounce:
            if analysis_allowed(watchlist) and not self._lock.locked():
                self._last_news_refresh = datetime.now(timezone.utc)
                asyncio.create_task(self.run_full_analysis(trigger="news_refresh"))

    async def _on_warmup_trigger(self) -> None:
        """Called by engine at 08:30 ET transition."""
        if not self._lock.locked():
            asyncio.create_task(self.run_full_analysis(trigger="pre_market_warmup"))

    # ── Full batch analysis ───────────────────────────────────────────────────

    async def run_full_analysis(self, trigger: str = "manual_button") -> str | None:
        """
        Runs full 6-category LLM analysis.
        Returns run_id if started, or existing run_id if already in progress.
        """
        from app.live_trading.stream_engine import ws_manager, get_market_state

        if self._lock.locked():
            logger.info("[PROCESSOR] Analysis already running, skipping trigger=%s", trigger)
            return self._active_run_id

        async with self._lock:
            run_id = str(uuid.uuid4())
            self._active_run_id = run_id
            await ws_manager.broadcast({"type": "analysis_started", "run_id": run_id, "trigger": trigger})
            logger.info("[PROCESSOR] Starting full analysis run_id=%s trigger=%s", run_id, trigger)

            try:
                watchlist = self._engine.watchlist if self._engine else []
                state = get_market_state(watchlist)

                # Build context
                news_items = await self._fetch_recent_news(limit=50)
                news_text = _compress_news(news_items)
                snapshots = await _build_snapshots(watchlist[:20])  # cap at 20 tickers
                prices = self._engine.latest_prices if self._engine else {}

                regime_summary = await asyncio.to_thread(self._get_regime_summary)

                user_msg = (
                    f"Market session: {state}\n"
                    f"Current regime: {regime_summary}\n\n"
                    f"Recent news (top by sentiment magnitude):\n{news_text}\n\n"
                    f"Current prices:\n"
                    + "\n".join(
                        f"  {t}: ${d['price']} ({d['change_pct']:+.2f}%)"
                        for t, d in list(prices.items())[:20]
                    )
                    + "\n\nTechnical snapshots:\n"
                    + "\n".join(
                        f"  {t}: {json.dumps(v)}"
                        for t, v in list({k: _snap_to_dict(v) for k, v in snapshots.items()}.items())[:15]
                    )
                )

                context_snapshot = {
                    "trigger": trigger,
                    "market_session": state,
                    "regime": regime_summary,
                    "news_count": len(news_items),
                    "news_summary": news_text,
                    "prices": {t: d for t, d in list(prices.items())[:20]},
                    "technicals": {t: _snap_to_dict(s) for t, s in list(snapshots.items())[:15]},
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }

                provider, model, api_key = _get_llm_config()
                raw, prompt_tokens, completion_tokens = await _call_llm(
                    _FULL_BATCH_SYSTEM, user_msg, provider, model, api_key
                )

                parsed_ok, recs = self._parse_full_response(raw)
                cost = _estimate_cost(model, prompt_tokens, completion_tokens)

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
                    parsed_ok=parsed_ok,
                    recommendations=recs,
                )

                await ws_manager.broadcast({
                    "type": "batch_ready",
                    "run_id": run_id,
                    "trigger": trigger,
                    "categories": list(recs.keys()),
                    "total": sum(len(v) for v in recs.values()),
                    "estimated_cost_usd": cost,
                })
                logger.info(
                    "[PROCESSOR] Analysis done run_id=%s recs=%d cost=$%.4f",
                    run_id, sum(len(v) for v in recs.values()), cost,
                )
                return run_id

            except Exception as exc:
                logger.error("[PROCESSOR] Analysis failed run_id=%s: %s", run_id, exc)
                await ws_manager.broadcast({"type": "analysis_error", "run_id": run_id, "error": str(exc)})
                return None
            finally:
                self._active_run_id = None

    # ── Ticker watch analysis ─────────────────────────────────────────────────

    async def run_ticker_watch(self, ticker: str, selected: dict, headline: str) -> None:
        """Narrow LLM re-analysis for selected trade after news event."""
        from app.live_trading.stream_engine import ws_manager
        from app.live_trading.trade_watcher import watcher

        logger.info("[PROCESSOR] Ticker watch for %s triggered by: %s", ticker, headline[:60])
        try:
            snap = await _get_snapshot(ticker)
            snap_info = _snap_to_dict(snap) if snap else {}
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
                f"  Technicals: {json.dumps(snap_info)}\n\n"
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

            # Update selected trade state
            watcher.update_from_analysis(result)

            # Save to DB
            await self._save_trade_update(
                ticker=ticker,
                headline=headline,
                confidence_before=selected.get("confidence"),
                result=result,
            )

            # Broadcast update
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

    def _parse_full_response(self, raw: str) -> tuple[bool, dict]:
        try:
            data = json.loads(raw)
            recs = data.get("recommendations", {})
            # Validate structure
            for cat in ("stocks", "commodities", "etfs", "fx", "crypto", "indices"):
                if cat not in recs:
                    recs[cat] = []
                # Filter to confidence >= 50
                recs[cat] = [r for r in recs[cat] if isinstance(r, dict) and (r.get("confidence") or 0) >= 50]
            return True, recs
        except Exception:
            # Retry: try to extract JSON from markdown-wrapped response
            try:
                start = raw.index("{")
                end = raw.rindex("}") + 1
                data = json.loads(raw[start:end])
                recs = data.get("recommendations", {})
                for cat in ("stocks", "commodities", "etfs", "fx", "crypto", "indices"):
                    if cat not in recs:
                        recs[cat] = []
                    recs[cat] = [r for r in recs[cat] if isinstance(r, dict) and (r.get("confidence") or 0) >= 50]
                return True, recs
            except Exception:
                logger.warning("[PROCESSOR] Failed to parse LLM response")
                return False, {cat: [] for cat in ("stocks", "commodities", "etfs", "fx", "crypto", "indices")}

    def _parse_ticker_watch_response(self, raw: str) -> dict | None:
        try:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            return json.loads(raw[start:end])
        except Exception:
            logger.warning("[PROCESSOR] Failed to parse ticker watch response")
            return None

    # ── DB persistence ────────────────────────────────────────────────────────

    async def _fetch_recent_news(self, limit: int = 50) -> list[dict]:
        try:
            from app.core.database import AsyncSessionLocal
            from app.live_trading.models import LiveNewsItem
            from sqlalchemy import select, desc
            cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(LiveNewsItem)
                    .where(LiveNewsItem.fetched_at >= cutoff)
                    .order_by(desc(LiveNewsItem.fetched_at))
                    .limit(limit)
                )
                rows = result.scalars().all()
                return [
                    {
                        "title": r.title,
                        "source": r.source,
                        "sentiment_score": r.sentiment_score or 0.0,
                        "asset_tags": json.loads(r.asset_tags or "[]"),
                    }
                    for r in rows
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
        recommendations: dict,
    ) -> None:
        try:
            from app.core.database import AsyncSessionLocal
            from app.live_trading.models import LiveAnalysisRun, LiveRecommendation
            now = datetime.now(timezone.utc)
            async with AsyncSessionLocal() as session:
                run = LiveAnalysisRun(
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
                )
                session.add(run)
                for cat, items in recommendations.items():
                    for item in items:
                        session.add(LiveRecommendation(
                            id=str(uuid.uuid4()),
                            run_id=run_id,
                            created_at=now,
                            asset_category=cat,
                            ticker=str(item.get("ticker", ""))[:32],
                            direction=str(item.get("direction", "long"))[:8],
                            entry_price=item.get("entry_price"),
                            stop_loss=item.get("stop_loss"),
                            take_profit=item.get("take_profit"),
                            confidence=item.get("confidence"),
                            risk_reward=item.get("risk_reward"),
                            rationale=str(item.get("rationale", ""))[:2000],
                            timeframe=str(item.get("timeframe", ""))[:16],
                            catalysts=json.dumps(item.get("catalysts") or []),
                            market_session=market_session,
                        ))
                await session.commit()
        except Exception as exc:
            logger.warning("[PROCESSOR] Failed to save run: %s", exc)

    async def _save_trade_update(
        self, ticker: str, headline: str, confidence_before: int | None, result: dict
    ) -> None:
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

    # ── Daily cost summary ────────────────────────────────────────────────────

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
                total = result.scalar()
                return round(float(total or 0), 4)
        except Exception:
            return 0.0


# ── Module-level singleton ────────────────────────────────────────────────────

processor = StreamProcessor()
