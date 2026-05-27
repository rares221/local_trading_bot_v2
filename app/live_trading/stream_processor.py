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
                        → if news tags specific tickers in current recs: run_targeted_analysis
                        → otherwise: run_full_analysis
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

# Cost controls
_DAILY_COST_CAP_USD: float = 1.0   # auto-triggered runs stop beyond this; manual always runs
_MAX_LLM_TICKERS: int = 15         # max tickers sent to LLM per full-analysis run

# Direction-aware confidence gates
# Shorts require higher conviction: bear-market counter-trend rallies cause 71.7% SL rate
# vs 60.9% for longs. Raising the short bar filters out weak signals before they run.
_MIN_CONFIDENCE_LONG: int = 40
_MIN_CONFIDENCE_SHORT: int = 55


# ── LLM config helpers ────────────────────────────────────────────────────────

def _get_llm_config() -> tuple[str, str, str | None]:
    from app.core.preferences import get_research_llm_config
    from app.core.config import settings
    cfg = get_research_llm_config()
    provider = cfg.get("research_llm_provider") or settings.research_llm_provider or ""
    model = cfg.get("research_llm_model") or ""
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

        elif provider == "lm_studio":
            import re as _re
            import requests
            from app.core.config import settings as _s
            from app.core.preferences import get_research_llm_config as _gcfg
            _cfg = _gcfg()
            base = (_cfg.get("lm_studio_base_url") or "").strip() or (getattr(_s, "lm_studio_base_url", None) or "http://localhost:1234/v1")
            resp = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": "Bearer lm-studio"},
                json={"model": model, "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": "/no_think\n" + user},
                ], "max_tokens": 4096},
                timeout=600,
            )
            resp.raise_for_status()
            data = resp.json()
            u = data.get("usage", {})
            content = _re.sub(r"<think>.*?</think>", "", data["choices"][0]["message"]["content"], flags=_re.DOTALL).strip()
            return content, u.get("prompt_tokens", 0), u.get("completion_tokens", 0)

        return "", 0, 0

    return await asyncio.to_thread(_sync)


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rate = _COST_PER_1M.get(model, _COST_PER_1M["default"])
    return round((prompt_tokens + completion_tokens) / 1_000_000 * rate, 6)


# ── News helpers ──────────────────────────────────────────────────────────────

def _parse_asset_tags(item: dict) -> list[str]:
    """Extract asset_tags from a news item as a flat list of strings."""
    raw = item.get("asset_tags") or "[]"
    try:
        tags = json.loads(raw) if isinstance(raw, str) else (raw or [])
        return [str(t).strip() for t in tags if t]
    except Exception:
        return []


def _compress_news(news_items: list[dict]) -> str:
    """Compress news to top 10 with directional context.
    Separates bullish/bearish and leads with net direction so the LLM
    understands consensus vs noise, not just magnitude."""
    if not news_items:
        return "No recent news available."

    bullish = sorted(
        [n for n in news_items if (n.get("sentiment_score") or 0) > 0],
        key=lambda x: -(x.get("sentiment_score") or 0),
    )
    bearish = sorted(
        [n for n in news_items if (n.get("sentiment_score") or 0) < 0],
        key=lambda x: (x.get("sentiment_score") or 0),
    )

    all_scores = [n.get("sentiment_score") or 0 for n in news_items]
    net_avg = sum(all_scores) / len(all_scores) if all_scores else 0
    if net_avg > 0.05:
        net_dir = "BULLISH"
    elif net_avg < -0.05:
        net_dir = "BEARISH"
    else:
        net_dir = "MIXED"

    # Interleave: up to 6 from dominant side, up to 4 from the other
    selected: list[dict] = []
    bi, bri = 0, 0
    while len(selected) < 10:
        bull_slots = sum(1 for s in selected if (s.get("sentiment_score") or 0) > 0)
        bear_slots = len(selected) - bull_slots
        if bi < len(bullish) and bull_slots < 6:
            selected.append(bullish[bi]); bi += 1
        elif bri < len(bearish) and bear_slots < 4:
            selected.append(bearish[bri]); bri += 1
        elif bi < len(bullish):
            selected.append(bullish[bi]); bi += 1
        elif bri < len(bearish):
            selected.append(bearish[bri]); bri += 1
        else:
            break

    lines = [
        f"Net: {net_dir} ({net_avg:+.2f}) | {len(bullish)} bullish / {len(bearish)} bearish of {len(news_items)}"
    ]
    for item in selected:
        score = item.get("sentiment_score") or 0
        sign = "+" if score > 0 else ""
        source = (item.get("source") or "")[:30]
        title = (item.get("title") or "")[:100]
        lines.append(f"[{source}] {title} ({sign}{score:.2f})")
    return "\n".join(lines)


# ── LLM prompts ───────────────────────────────────────────────────────────────

_ANALYSIS_SYSTEM = """\
You are a senior quantitative trader. You receive a single math-generated trading signal
(direction, entry, ATR-based SL/TP, confidence score). Your job is to analyze
it in the context of current news and market regime, and return:

  trade_type : "scalp" (<4h), "intraday" (same day), or "swing" (2-5 days)
  rationale  : 1-2 sentences — WHY this trade makes sense given news + regime
  llm_sl     : your stop loss estimate (adjust ATR level if key news/levels suggest it)
  llm_tp     : your take profit estimate (same logic)
  catalysts  : list of 1-3 short strings (e.g. ["risk_off_regime", "safe_haven_bid"])

Return ONLY valid JSON (no markdown, no extra text):
{
  "ticker": "GC=F",
  "trade_type": "swing",
  "rationale": "Gold bid as safe haven...",
  "llm_sl": 4760.0,
  "llm_tp": 4920.0,
  "catalysts": ["safe_haven_demand", "risk_off_regime"]
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
        # Cache of the last full analysis results — used to match news tags to displayed tickers
        self._current_recs: dict[str, list] = {}

    def set_engine(self, eng) -> None:
        self._engine = eng
        eng.set_news_callback(self._on_news_event)
        eng.set_warmup_callback(self._on_warmup_trigger)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    async def _on_news_event(self, item: dict) -> None:
        from app.live_trading.stream_engine import analysis_allowed, ws_manager
        from app.live_trading.trade_watcher import watcher

        watchlist = self._engine.watchlist if self._engine else []
        headline = (item.get("title") or "")[:200]
        asset_tags = _parse_asset_tags(item)

        # ── Active trade watch ────────────────────────────────────────────────
        selected = watcher.get()
        if selected:
            ticker = selected.get("ticker", "")
            ticker_match = any(
                ticker.upper() in tag.upper() or tag.upper() in ticker.upper()
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
                            headline=headline,
                        ))

        # ── News-triggered analysis (targeted only) ───────────────────────────
        last_refresh = self._last_news_refresh
        if not last_refresh or (datetime.now(timezone.utc) - last_refresh) > self._news_refresh_debounce:
            if analysis_allowed(watchlist) and not self._lock.locked():
                matched = self._match_tags_to_recs(asset_tags)
                if matched:
                    # Only act when news directly targets tickers we're already tracking
                    self._last_news_refresh = datetime.now(timezone.utc)
                    logger.info("[PROCESSOR] News tagged %s — running targeted analysis", matched)
                    asyncio.create_task(self.run_targeted_analysis(matched, headline))
                # General market news: do NOT trigger a full analysis — too expensive per article

    async def _on_warmup_trigger(self) -> None:
        # run_full_analysis checks the daily cost cap internally for non-manual triggers
        if not self._lock.locked():
            asyncio.create_task(self.run_full_analysis(trigger="pre_market_warmup"))

    # ── Full analysis (math + LLM) ────────────────────────────────────────────

    async def run_full_analysis(self, trigger: str = "manual_button") -> str | None:
        from app.live_trading.stream_engine import ws_manager, get_market_state
        from app.live_trading.live_analyzer import build_all_signals

        if self._lock.locked():
            logger.info("[PROCESSOR] Already running, skipping trigger=%s", trigger)
            return self._active_run_id

        # Daily cost cap — manual button always bypasses; auto-triggers respect it
        if trigger != "manual_button":
            daily = await self.get_daily_cost()
            if daily >= _DAILY_COST_CAP_USD:
                logger.warning("[PROCESSOR] Daily cost cap $%.2f reached (spent $%.4f) — skipping auto-trigger=%s",
                               _DAILY_COST_CAP_USD, daily, trigger)
                await ws_manager.broadcast({
                    "type": "analysis_skipped",
                    "reason": f"Daily cost cap ${_DAILY_COST_CAP_USD:.2f} reached (${daily:.4f} spent today). Use the manual button to override.",
                })
                return None

        async with self._lock:
            run_id = str(uuid.uuid4())
            self._active_run_id = run_id
            watchlist = self._engine.watchlist if self._engine else []
            state = get_market_state(watchlist)

            await ws_manager.broadcast({"type": "analysis_started", "run_id": run_id, "trigger": trigger})
            logger.info("[PROCESSOR] Analysis started run_id=%s trigger=%s", run_id, trigger)

            try:
                # ── Step 1: Math signals ──────────────────────────────────────
                signals = await build_all_signals(min_confidence=10)
                logger.info("[PROCESSOR] Math signals: %d", len(signals))

                if not signals:
                    await ws_manager.broadcast({"type": "analysis_error", "run_id": run_id, "error": "No signals above confidence threshold"})
                    return None

                # ── Step 2: Fetch news + regime context ───────────────────────
                logger.info("[PROCESSOR] Fetching news…")
                news_items = await self._fetch_recent_news(limit=50)
                news_text = _compress_news(news_items)
                logger.info("[PROCESSOR] Getting regime summary…")
                regime_summary = await asyncio.to_thread(self._get_regime_summary)
                logger.info("[PROCESSOR] Regime: %s", regime_summary)

                # ── Step 3: Per-ticker LLM analysis ──────────────────────────
                # Priority: watched tickers first, then confidence >= 40%, sorted desc
                # Hard cap at _MAX_LLM_TICKERS to bound per-run cost
                watchlist = set(self._engine.watchlist if self._engine else [])
                # Regime-aware short confidence floor: only allow shorts in regimes
                # where the engine permits them; block in all others.
                _regime_label = regime_summary.split(" | ")[0].strip().lower()
                from app.services.recommendations.engine import (
                    SHORTS_ENABLED,
                    SHORTS_BEAR_REGIME_ENABLED,
                )
                _shorts_regime_ok = SHORTS_ENABLED or (SHORTS_BEAR_REGIME_ENABLED and _regime_label == "bear")

                eligible = [
                    s for s in signals
                    if (
                        s.get("direction") != "short"
                        and (s.get("confidence") or 0) >= _MIN_CONFIDENCE_LONG
                    ) or (
                        s.get("direction") == "short"
                        and _shorts_regime_ok
                        and (s.get("confidence") or 0) >= _MIN_CONFIDENCE_SHORT
                    )
                ]
                llm_signals = sorted(
                    eligible,
                    key=lambda x: (
                        0 if x["ticker"] in watchlist else 1,  # watched first
                        -(x.get("confidence") or 0),
                    )
                )[:_MAX_LLM_TICKERS]

                provider, model, api_key = _get_llm_config()
                llm_by_ticker: dict[str, dict] = {}
                total_prompt_tokens = 0
                total_completion_tokens = 0

                session_note = (
                    "NOTE: Equity markets (stocks, ETFs, cash indices) are CLOSED. "
                    "Only crypto (-USD), FX (=X), and CME futures (=F) are actively trading. "
                    "Deprioritize or skip equity signals; focus analysis on the open markets.\n\n"
                ) if state == "partial_24h" else ""
                context_prefix = (
                    f"Market session: {state}\n"
                    f"Regime: {regime_summary}\n\n"
                    f"{session_note}"
                    f"Recent news:\n{news_text}\n\n"
                )

                logger.info("[PROCESSOR] Starting per-ticker LLM loop: %d signals, provider=%s", len(llm_signals), provider)
                for sig in llm_signals:
                    logger.info("[PROCESSOR] Calling LLM for %s…", sig["ticker"])
                    signal_text = json.dumps({
                        "ticker":      sig["ticker"],
                        "category":    sig["category"],
                        "direction":   sig["direction"],
                        "entry_price": sig["entry_price"],
                        "math_sl":     sig["math_sl"],
                        "math_tp":     sig["math_tp"],
                        "confidence":  sig["confidence"],
                        "risk_reward": sig["risk_reward"],
                        "regime":      sig["regime"],
                    })
                    user_msg = context_prefix + f"Signal:\n{signal_text}"

                    try:
                        raw, pt, ct = await _call_llm(_ANALYSIS_SYSTEM, user_msg, provider, model, api_key)
                        total_prompt_tokens += pt
                        total_completion_tokens += ct
                        parsed = self._parse_single_analysis(raw)
                        if parsed:
                            llm_by_ticker[sig["ticker"]] = parsed
                            logger.debug("[PROCESSOR] LLM ok: %s", sig["ticker"])
                        else:
                            logger.warning("[PROCESSOR] Parse failed for %s. Raw: %r", sig["ticker"], (raw or "")[:200])
                    except Exception as exc:
                        logger.warning("[PROCESSOR] LLM call failed for %s: %s", sig["ticker"], exc)

                cost = _estimate_cost(model, total_prompt_tokens, total_completion_tokens)
                raw = None  # no single raw for per-ticker mode

                # ── Step 4: Merge math + LLM ──────────────────────────────────
                merged = self._merge_signals_with_llm(eligible, llm_by_ticker)

                # Group by category
                recs_by_cat: dict[str, list] = {}
                for sig in merged:
                    cat = sig["category"]
                    recs_by_cat.setdefault(cat, []).append(sig)

                # Cache for targeted-analysis routing
                self._current_recs = recs_by_cat

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
                    prompt_tokens=total_prompt_tokens,
                    completion_tokens=total_completion_tokens,
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

    def _parse_single_analysis(self, raw: str | None) -> dict | None:
        """Parse a single-ticker LLM response (flat JSON object, not wrapped in analyses array)."""
        if not raw:
            return None
        raw = self._strip_thinking(raw)
        try:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            data = json.loads(raw[start:end])
            if isinstance(data, dict) and any(k in data for k in ("rationale", "llm_sl", "trade_type")):
                return data
        except Exception as exc:
            logger.warning("[PROCESSOR] _parse_single_analysis JSON error: %s | raw=%r", exc, (raw or "")[:300])
        return None

    # ── Targeted analysis (news → specific tickers) ───────────────────────────

    def _match_tags_to_recs(self, asset_tags: list[str]) -> list[str]:
        """Return tickers from current recommendations that match the news asset_tags."""
        if not asset_tags or not self._current_recs:
            return []
        all_tickers = {
            sig["ticker"]
            for recs in self._current_recs.values()
            for sig in recs
        }
        matched = []
        for ticker in all_tickers:
            t_up = ticker.upper()
            for tag in asset_tags:
                tag_up = tag.upper()
                if t_up in tag_up or tag_up in t_up:
                    matched.append(ticker)
                    break
        return matched

    def _find_rec_meta(self, ticker: str) -> dict:
        """Return category and any other metadata stored for a ticker in current recs."""
        for cat, recs in self._current_recs.items():
            for rec in recs:
                if rec.get("ticker") == ticker:
                    return {"category": rec.get("category", cat)}
        return {}

    async def run_targeted_analysis(self, tickers: list[str], headline: str) -> None:
        """Run math + LLM analysis only for the tickers affected by breaking news."""
        from app.live_trading.stream_engine import ws_manager, get_market_state
        from app.live_trading.live_analyzer import (
            _compute_signal_sync, _snap_executor,
            _aggregate_live_sentiment_sync, get_auto_universe,
        )

        logger.info("[PROCESSOR] Targeted analysis %s ← %s", tickers, headline[:60])
        try:
            await ws_manager.broadcast({
                "type": "targeted_analysis_started",
                "tickers": tickers,
                "trigger_headline": headline,
            })

            # Resolve universe metadata (risk_tier, category) for each ticker
            universe = await asyncio.to_thread(get_auto_universe)
            universe_map: dict[str, tuple] = {row[1]: row for row in universe}  # yf_symbol → row

            # Get current sentiment data
            sent_data = await asyncio.to_thread(_aggregate_live_sentiment_sync)

            # Get market regime
            regime = "cautious"
            try:
                from app.services.market_context import detect_market_regime
                ctx = await asyncio.to_thread(detect_market_regime)
                regime = ctx.regime if hasattr(ctx, "regime") else str(ctx)
            except Exception:
                pass

            # Compute math signals in parallel for the targeted tickers only
            loop = asyncio.get_event_loop()
            sig_tasks = []
            for ticker in tickers:
                meta = universe_map.get(ticker)
                if meta:
                    asset_id, yf_symbol, risk_tier, category = meta
                else:
                    rec_meta = self._find_rec_meta(ticker)
                    asset_id, yf_symbol, risk_tier, category = (
                        ticker, ticker, "moderate", rec_meta.get("category", "stocks")
                    )
                tag_info = sent_data.get(asset_id) or sent_data.get(ticker) or {}
                import functools as _functools
                sig_tasks.append(loop.run_in_executor(
                    _snap_executor,
                    _functools.partial(
                        _compute_signal_sync,
                        asset_id, yf_symbol, risk_tier, category,
                        float(tag_info.get("avg", 0.0)),
                        int(tag_info.get("count", 0)),
                        regime,
                        None,  # backtest_stats
                        tag_info.get("velocity"),
                        tag_info.get("conviction"),
                    ),
                ))

            results = await asyncio.gather(*sig_tasks, return_exceptions=True)
            signals = [r for r in results if isinstance(r, dict) and r and (r.get("confidence") or 0) >= 40]

            if not signals:
                logger.info("[PROCESSOR] Targeted: no displayable signals for %s", tickers)
                return

            # Fetch recent news for LLM context
            news_items = await self._fetch_recent_news(limit=20)
            news_text = _compress_news(news_items)
            regime_summary = await asyncio.to_thread(self._get_regime_summary)

            watchlist = self._engine.watchlist if self._engine else []
            state = get_market_state(watchlist)

            session_note = (
                "NOTE: Equity markets (stocks, ETFs, cash indices) are CLOSED. "
                "Only crypto (-USD), FX (=X), and CME futures (=F) are actively trading. "
                "Deprioritize or skip equity signals; focus analysis on the open markets.\n\n"
            ) if state == "partial_24h" else ""
            context_prefix = (
                f"Market session: {state}\n"
                f"Regime: {regime_summary}\n\n"
                f"{session_note}"
                f"BREAKING NEWS (triggered this analysis):\n{headline}\n\n"
                f"Recent news context:\n{news_text}\n\n"
            )

            # Per-ticker LLM calls — same pattern as run_full_analysis.
            # The old code sent all tickers in one batch with _parse_analysis_response
            # which expects {"analyses":[...]} but _ANALYSIS_SYSTEM tells the LLM to
            # return a single flat JSON object — format mismatch always lost LLM data.
            provider, model, api_key = _get_llm_config()
            llm_by_ticker: dict[str, dict] = {}
            total_prompt_tokens = 0
            total_completion_tokens = 0

            for sig in signals:
                signal_text = json.dumps({
                    "ticker":      sig["ticker"],
                    "category":    sig["category"],
                    "direction":   sig["direction"],
                    "entry_price": sig["entry_price"],
                    "math_sl":     sig["math_sl"],
                    "math_tp":     sig["math_tp"],
                    "confidence":  sig["confidence"],
                    "risk_reward": sig["risk_reward"],
                    "regime":      sig["regime"],
                })
                user_msg = context_prefix + f"Signal:\n{signal_text}"
                try:
                    raw, pt, ct = await _call_llm(_ANALYSIS_SYSTEM, user_msg, provider, model, api_key)
                    total_prompt_tokens += pt
                    total_completion_tokens += ct
                    parsed = self._parse_single_analysis(raw)
                    if parsed:
                        llm_by_ticker[sig["ticker"]] = parsed
                        logger.debug("[PROCESSOR] Targeted LLM ok: %s", sig["ticker"])
                    else:
                        logger.warning("[PROCESSOR] Targeted parse failed for %s", sig["ticker"])
                except Exception as exc:
                    logger.warning("[PROCESSOR] Targeted LLM call failed for %s: %s", sig["ticker"], exc)

            cost = _estimate_cost(model, total_prompt_tokens, total_completion_tokens)
            merged = self._merge_signals_with_llm(signals, llm_by_ticker)

            # Patch _current_recs (internal cache uses raw signal field names).
            # Build patches with DB-style field names so the frontend renders correctly —
            # signal dicts use math_sl/math_tp/llm_sl/llm_tp/trade_type/category but
            # recCardHTML expects stop_loss/take_profit/llm_stop_loss/llm_take_profit/timeframe/asset_category.
            patches_by_cat: dict[str, list] = {}
            for sig in merged:
                cat = sig["category"]
                if cat in self._current_recs:
                    for i, existing in enumerate(self._current_recs[cat]):
                        if existing["ticker"] == sig["ticker"]:
                            self._current_recs[cat][i] = sig
                            break
                normalized = {
                    **sig,
                    "asset_category":  sig.get("category"),
                    "stop_loss":       sig.get("math_sl"),
                    "take_profit":     sig.get("math_tp"),
                    "llm_stop_loss":   sig.get("llm_sl"),
                    "llm_take_profit": sig.get("llm_tp"),
                    "timeframe":       sig.get("trade_type") or "swing",
                    "market_session":  state,
                }
                patches_by_cat.setdefault(cat, []).append(normalized)

            await ws_manager.broadcast({
                "type": "ticker_patch",
                "patches": patches_by_cat,
                "tickers": tickers,
                "trigger_headline": headline,
                "estimated_cost_usd": cost,
            })
            logger.info("[PROCESSOR] Targeted done tickers=%s llm_ok=%d cost=$%.4f", tickers, len(llm_by_ticker), cost)

        except Exception as exc:
            logger.warning("[PROCESSOR] Targeted analysis error: %s", exc, exc_info=True)

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
                import functools as _functools
                sig = await asyncio.get_event_loop().run_in_executor(
                    _snap_executor,
                    _functools.partial(
                        _compute_signal_sync,
                        ticker, ticker, "moderate", "stocks",
                        float(tag_info.get("avg", 0.0)),
                        int(tag_info.get("count", 0)),
                        "cautious",
                        None,  # backtest_stats
                        tag_info.get("velocity"),
                        tag_info.get("conviction"),
                    ),
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

    @staticmethod
    def _strip_thinking(raw: str) -> str:
        """Strip <think>...</think> blocks returned by thinking-mode models (e.g. qwen3)."""
        import re
        return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    def _parse_analysis_response(self, raw: str) -> dict[str, dict]:
        """Parse LLM analysis response. Returns {ticker: analysis_dict}."""
        raw = self._strip_thinking(raw)

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
                logger.warning("[PROCESSOR] Failed to parse LLM analysis response. Raw (first 500): %r", raw[:500])
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
        raw = self._strip_thinking(raw)
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
