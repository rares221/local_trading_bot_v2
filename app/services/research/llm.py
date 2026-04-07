"""
Multi-provider LLM dispatch for research rationale (Dexter-style).
Supports OpenAI, Anthropic, Ollama, OpenRouter; uses research_llm_provider + research_llm_model from config.
"""
from __future__ import annotations

from typing import Any

from app.services.research.providers import (
    get_provider_by_id,
    is_available,
)


STRATEGY_EXPLAIN_SYSTEM = (
    "You are an experienced trading coach. Explain the user's chosen execution strategy "
    "(scale-out / targets) in clear, plain language. "
    "Use 1–2 short paragraphs or a few bullets. Cover: each exit level and position share, "
    "combined risk if stopped before TPs, one pro and one con vs a single full TP. "
    "Use the numbers from the user message. No long disclaimers. "
    "Target about 100–180 words."
)


SYSTEM_PROMPT = (
    "You are a concise trading research assistant. In 2–4 sentences only, "
    "explain why this SPECIFIC trade direction (the Direction field provided) might make sense and the main risk. "
    "CRITICAL: If Direction is 'short', frame the rationale around why the asset may decline — "
    "do NOT argue for a long/bullish position. If Direction is 'long', argue why it may rise. "
    "Reference key technicals, sentiment, and market conditions. "
    "No disclaimers; factual and neutral."
)


def _format_user_content(context: dict[str, Any]) -> str:
    sentiment = context.get("sentiment_avg")
    price = context.get("buy_in_price")
    risk = context.get("risk_tier")
    headlines = context.get("headlines") or []
    asset = context.get("asset_id", "this asset")
    fundamentals = context.get("fundamentals") or {}
    direction = context.get("direction", "long")
    parts = [f"Asset: {asset}.", f"Direction: {direction}."]
    if sentiment is not None:
        parts.append(f"Recent sentiment (weighted avg): {sentiment:.3f}.")
    if price is not None:
        parts.append(f"Current/reference price: {price}.")
    if risk:
        parts.append(f"Risk tier: {risk}.")
    vol_notes = context.get("volatility_notes")
    if vol_notes:
        parts.append(f"Technicals: {vol_notes}.")
    if fundamentals:
        fparts = []
        if fundamentals.get("sector"):
            fparts.append(f"sector {fundamentals['sector']}")
        if fundamentals.get("industry"):
            fparts.append(f"industry {fundamentals['industry']}")
        if fundamentals.get("market_cap") is not None:
            mc = fundamentals["market_cap"]
            if mc >= 1_000_000_000_000:
                fparts.append("market cap ~trillion")
            elif mc >= 1_000_000_000:
                fparts.append("large-cap")
            elif mc >= 300_000_000:
                fparts.append("mid-cap")
            else:
                fparts.append("small-cap")
        if fundamentals.get("short_name"):
            fparts.append(f"({fundamentals['short_name']})")
        if fparts:
            parts.append("Fundamentals: " + ", ".join(fparts) + ".")
    if headlines:
        parts.append("Headlines: " + " | ".join(headlines[:2]))
    # Relative Strength context
    rs_20d = context.get("rs_20d")
    if rs_20d is not None:
        rs_60d = context.get("rs_60d")
        sector_rs = context.get("sector_rs")
        sector_etf = context.get("sector_etf", "sector ETF")
        rs_label = "outperforming" if rs_20d > 1.0 else "underperforming"
        rs_line = f"Relative Strength vs S&P 500 (20d): {rs_20d:.2f}x ({rs_label} the market)"
        if rs_60d is not None:
            rs_line += f"; 60d: {rs_60d:.2f}x"
        if sector_rs is not None:
            sector_label = "outperforming" if sector_rs > 1.0 else "underperforming"
            rs_line += f"; vs {sector_etf}: {sector_rs:.2f}x ({sector_label} its sector)"
        parts.append(rs_line + ".")

    # Volume Profile context
    vpoc = context.get("vpoc")
    if vpoc is not None:
        vah = context.get("vah")
        val = context.get("val")
        vp_bias = context.get("vp_bias", "at_vpoc")
        vp_ext = context.get("vp_overextension")
        in_va = context.get("in_value_area", False)
        bias_label = {"above_vpoc": "above fair value", "below_vpoc": "below fair value"}.get(vp_bias, "at fair value")
        vp_line = f"Volume Profile: VPOC=${vpoc:.4g} ({bias_label})"
        if vah and val:
            vp_line += f", Value Area ${val:.4g}–${vah:.4g}"
        if in_va:
            vp_line += ", price inside value area (choppy equilibrium zone)"
        if vp_ext is not None and abs(vp_ext) > 1.3:
            vp_line += f", overextended {abs(vp_ext):.1f}× from VPOC (mean reversion risk)"
        parts.append(vp_line + ".")
    return "\n".join(parts)


def _get_api_key_for_provider(provider_id: str) -> str | None:
    from app.core.config import settings
    if provider_id == "openai":
        return settings.openai_api_key
    if provider_id == "anthropic":
        return settings.anthropic_api_key
    if provider_id == "openrouter":
        return settings.openrouter_api_key
    return None


async def _call_openai(
    model: str, system: str, user: str, api_key: str, *, max_tokens: int = 200
) -> str | None:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key)
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
    )
    if response.choices and response.choices[0].message.content:
        return response.choices[0].message.content.strip()
    return None


async def _call_openrouter(
    model: str, system: str, user: str, api_key: str, *, max_tokens: int = 200
) -> str | None:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
    )
    if response.choices and response.choices[0].message.content:
        return response.choices[0].message.content.strip()
    return None


async def _call_anthropic(
    model: str, system: str, user: str, api_key: str, *, max_tokens: int = 200,
    thinking_budget: int = 0,
) -> str | None:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)

    kwargs: dict = dict(
        model=model,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if thinking_budget > 0:
        # Extended thinking: max_tokens must exceed budget; extra 1024 for the actual response
        kwargs["max_tokens"] = thinking_budget + 1024
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
    else:
        kwargs["max_tokens"] = max_tokens

    msg = await client.messages.create(**kwargs)

    # Content may include thinking blocks — return the first text block
    for block in (msg.content or []):
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            return block.text.strip()
    return None


def _ollama_extract_assistant_text(data: dict[str, Any]) -> str | None:
    """Normalize Ollama /api/chat `message.content` (str or multimodal list)."""
    msg = data.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
        joined = "".join(parts).strip()
        return joined or None
    return None


async def _call_ollama(
    model: str,
    system: str,
    user: str,
    base_url: str,
    *,
    num_predict: int | None = None,
    timeout: float = 60.0,
) -> str | None:
    import httpx

    messages: list[dict[str, str]] = []
    if system and str(system).strip():
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    payload: dict[str, Any] = {
        "model": model,
        "stream": False,
        "messages": messages,
    }
    if num_predict is not None:
        payload["options"] = {"num_predict": num_predict}
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{base_url.rstrip('/')}/api/chat", json=payload)
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = (e.response.text or "")[:800]
            except Exception:
                pass
            raise RuntimeError(
                f"Ollama HTTP {e.response.status_code}: {detail or e.response.reason_phrase}"
            ) from e
        try:
            data = r.json()
        except Exception as exc:
            raise RuntimeError(f"Ollama response not JSON: {(r.text or '')[:400]}") from exc

    err = data.get("error")
    if err:
        raise RuntimeError(str(err) if not isinstance(err, dict) else str(err.get("message", err)))

    text = _ollama_extract_assistant_text(data)
    if text:
        return text
    return None


_CORRELATION_SYSTEM_PROMPT = (
    "You are a concise market analyst. You will be given a list of unusual cross-asset "
    "correlations detected in the market today. In 2-3 sentences, explain what these "
    "divergences collectively suggest about current market conditions and what a trader "
    "should watch for. Be specific and direct. No disclaimers. "
    "Do NOT use markdown — no headers, no bullet points, no bold. Plain text only."
)


async def generate_correlation_analysis(diverging_signals: list[dict[str, Any]]) -> str | None:
    """
    Generate a short LLM interpretation of diverging cross-asset correlation signals.
    Returns 2-3 sentences or None if LLM is unavailable.
    """
    from app.core.config import settings
    from app.core.preferences import get_research_llm_config

    if not diverging_signals:
        return None

    cfg = get_research_llm_config()
    provider_id = (cfg.get("research_llm_provider") or "openai").strip().lower()
    provider = get_provider_by_id(provider_id)
    if not provider:
        provider_id = "openai"
        provider = get_provider_by_id("openai")

    api_key = _get_api_key_for_provider(provider_id)
    if not is_available(provider_id, api_key):
        return None

    model = (cfg.get("research_llm_model") or "").strip() or provider.default_model
    if not model:
        return None

    ticker_name = {
        "^GSPC": "S&P 500", "GC=F": "Gold", "^VIX": "VIX",
        "DX-Y.NYB": "USD Index", "CL=F": "Oil", "^TNX": "10Y Yield",
    }
    lines = []
    for s in diverging_signals:
        a = ticker_name.get(s["asset_a"], s["asset_a"])
        b = ticker_name.get(s["asset_b"], s["asset_b"])
        lines.append(f"- {a} is {s['a_direction']}, {b} is {s['b_direction']} (normally {s['expected_relationship']})")
    user = "Unusual cross-asset correlations today:\n" + "\n".join(lines)

    ollama_base_url = (cfg.get("ollama_base_url") or "").strip() or (settings.ollama_base_url or "http://localhost:11434")

    try:
        if provider_id == "openai":
            return await _call_openai(model, _CORRELATION_SYSTEM_PROMPT, user, api_key or "")
        if provider_id == "anthropic":
            return await _call_anthropic(model, _CORRELATION_SYSTEM_PROMPT, user, api_key or "")
        if provider_id == "ollama":
            return await _call_ollama(model, _CORRELATION_SYSTEM_PROMPT, user, ollama_base_url)
        if provider_id == "openrouter":
            return await _call_openrouter(model, _CORRELATION_SYSTEM_PROMPT, user, api_key or "")
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Correlation LLM call failed (%s/%s): %s", provider_id, model, exc)
    return None


async def generate_rationale(context: dict[str, Any]) -> str | None:
    """
    One-shot LLM call using the configured provider (from preferences or config).
    Returns 2–4 sentences on why this idea might make sense and main risk, or None if unavailable/fails.
    """
    from app.core.config import settings
    from app.core.preferences import get_research_llm_config

    cfg = get_research_llm_config()
    provider_id = (cfg.get("research_llm_provider") or "openai").strip().lower()
    provider = get_provider_by_id(provider_id)
    if not provider:
        provider_id = "openai"
        provider = get_provider_by_id("openai")

    api_key = _get_api_key_for_provider(provider_id)
    if not is_available(provider_id, api_key):
        return None

    model = (cfg.get("research_llm_model") or "").strip() or provider.default_model
    if not model:
        return None

    system = SYSTEM_PROMPT
    user = _format_user_content(context)
    ollama_base_url = (cfg.get("ollama_base_url") or "").strip() or (settings.ollama_base_url or "http://localhost:11434")
    thinking_budget = int(cfg.get("thinking_budget") or 0)

    try:
        if provider_id == "openai":
            return await _call_openai(model, system, user, api_key or "", max_tokens=200)
        if provider_id == "anthropic":
            return await _call_anthropic(model, system, user, api_key or "", max_tokens=200, thinking_budget=thinking_budget)
        if provider_id == "ollama":
            return await _call_ollama(model, system, user, ollama_base_url)
        if provider_id == "openrouter":
            return await _call_openrouter(model, system, user, api_key or "", max_tokens=200)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("LLM call failed (%s/%s): %s", provider_id, model, exc)
    return None


async def generate_strategy_explanation(user_message: str) -> str | None:
    """
    Longer LLM explanation for trade-builder strategy selection.
    Same provider/model as research; returns None if unavailable or on failure.
    """
    from app.core.config import settings
    from app.core.preferences import get_research_llm_config

    cfg = get_research_llm_config()
    provider_id = (cfg.get("research_llm_provider") or "openai").strip().lower()
    provider = get_provider_by_id(provider_id)
    if not provider:
        provider_id = "openai"
        provider = get_provider_by_id("openai")

    api_key = _get_api_key_for_provider(provider_id)
    if not is_available(provider_id, api_key):
        return None

    model = (cfg.get("research_llm_model") or "").strip() or provider.default_model
    if not model:
        return None

    ollama_base_url = (cfg.get("ollama_base_url") or "").strip() or (
        settings.ollama_base_url or "http://localhost:11434"
    )
    max_tok = 480

    try:
        if provider_id == "openai":
            return await _call_openai(
                model, STRATEGY_EXPLAIN_SYSTEM, user_message, api_key or "", max_tokens=max_tok
            )
        if provider_id == "anthropic":
            return await _call_anthropic(
                model, STRATEGY_EXPLAIN_SYSTEM, user_message, api_key or "", max_tokens=max_tok
            )
        if provider_id == "ollama":
            # Small local models (e.g. gemma3:1b) often fail or return empty with a separate
            # system message; one combined user turn is more reliable.
            combined = (
                f"{STRATEGY_EXPLAIN_SYSTEM.strip()}\n\n---\n\n{user_message.strip()}"
            )
            ollama_predict = min(max_tok, 384)
            return await _call_ollama(
                model,
                "",
                combined,
                ollama_base_url,
                num_predict=ollama_predict,
                timeout=120.0,
            )
        if provider_id == "openrouter":
            return await _call_openrouter(
                model, STRATEGY_EXPLAIN_SYSTEM, user_message, api_key or "", max_tokens=max_tok
            )
    except Exception as exc:
        import logging

        log = logging.getLogger(__name__)
        log.warning(
            "Strategy LLM call failed (%s/%s): %s: %r",
            provider_id,
            model,
            exc.__class__.__name__,
            exc,
            exc_info=log.isEnabledFor(logging.DEBUG),
        )
    return None


_BACKTEST_INSIGHT_SYSTEM = (
    "You are a quantitative trading analyst reviewing short-term backtesting results and making a forward-looking prediction. "
    "Return ONLY valid JSON with this exact structure — no markdown, no extra text:\n"
    "{\n"
    '  "grade": "A"|"B"|"C"|"D"|"F",\n'
    '  "grade_reason": "one short sentence explaining the grade",\n'
    '  "key_insights": ["insight 1", "insight 2", "insight 3"],\n'
    '  "recommendation": "one concrete actionable recommendation for the next 14 days"\n'
    "}\n\n"
    "Grade rubric: A = win rate ≥55% AND positive avg P&L AND sharpe >1; "
    "B = win rate 45-54% OR solid avg P&L; C = win rate 35-44%; D = win rate 25-34%; F = below 25% or losing avg P&L.\n"
    "key_insights: exactly 3 bullet strings — one about recent signal quality (last 90 days), "
    "one about risk metrics (drawdown/sharpe), one about what TODAY's signal and current conditions imply for the next 14 days.\n"
    "The third insight MUST reference the current signal confidence and sentiment if provided. "
    "Write for a mixed audience: plain enough for a beginner, precise enough for a pro trader. No disclaimers."
)


def _parse_backtest_insight_json(raw: str | None) -> dict | None:
    """Extract and parse JSON from LLM response, tolerating markdown fences."""
    import json, re
    if not raw:
        return None
    # Strip markdown code fences if present
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()
    # Find first { ... } block
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        # Validate required fields
        if all(k in data for k in ("grade", "grade_reason", "key_insights", "recommendation")):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return None


async def generate_backtest_insight(
    asset_id: str,
    direction: str,
    risk_tier: str,
    metrics: dict,
    direction_stats: dict,
    by_confidence_bucket: dict,
    current_signal: dict | None = None,
    horizon_days: int = 14,
) -> dict | None:
    """
    Post-backtest LLM interpretation with 14-day forward prediction.
    Returns a structured dict: {grade, grade_reason, key_insights[], recommendation}
    or None if LLM unavailable or response cannot be parsed.
    """
    from app.core.config import settings
    from app.core.preferences import get_research_llm_config

    cfg = get_research_llm_config()
    provider_id = (cfg.get("research_llm_provider") or "openai").strip().lower()
    provider = get_provider_by_id(provider_id)
    if not provider:
        provider_id = "openai"
        provider = get_provider_by_id("openai")

    api_key = _get_api_key_for_provider(provider_id)
    if not is_available(provider_id, api_key):
        return None

    model = (cfg.get("research_llm_model") or "").strip() or provider.default_model
    if not model:
        return None

    ollama_base_url = (cfg.get("ollama_base_url") or "").strip() or (
        settings.ollama_base_url or "http://localhost:11434"
    )

    dir_s = direction_stats.get(direction, {})
    other_dir = "short" if direction == "long" else "long"
    other_s = direction_stats.get(other_dir, {})

    def _pct(v):
        return f"{round((v or 0) * 100, 1)}%" if v is not None else "N/A"

    bucket_lines = []
    for bucket, bd in (by_confidence_bucket or {}).items():
        wr = bd.get("win_rate")
        n = bd.get("total", 0)
        bucket_lines.append(f"  {bucket}: {_pct(wr)} win rate ({n} trades)")

    # Current signal section
    cs_lines = ""
    if current_signal:
        cs = current_signal
        cs_lines = (
            f"\n--- TODAY's engine signal (current conditions) ---\n"
            f"Direction: {cs.get('direction','?').upper()} | Confidence: {cs.get('confidence','?')} | "
            f"Risk/Reward: {cs.get('risk_reward','N/A')}x\n"
            f"Price: {cs.get('price','?')} | SL: {cs.get('stop_loss','?')} | TP: {cs.get('take_profit','?')}\n"
            f"Trend bias: {cs.get('trend_bias','?')} | Volatility: {cs.get('volatility_regime','?')}\n"
            f"Sentiment avg (recent): {cs.get('sentiment_avg','N/A')} ({cs.get('sentiment_count',0)} articles)\n"
        )

    user_content = (
        f"Asset: {asset_id} | Direction analysed: {direction.upper()} | Risk tier: {risk_tier}\n"
        f"Backtest window: last 90 days | Outcome horizon: {horizon_days} trading days\n"
        f"\n--- {direction.upper()} direction (last 90 days) ---\n"
        f"Signals: {dir_s.get('total', 0)} | Wins: {dir_s.get('wins', 0)} | Win rate: {_pct(dir_s.get('win_rate'))}\n"
        f"\n--- {other_dir.upper()} direction (last 90 days) ---\n"
        f"Signals: {other_s.get('total', 0)} | Wins: {other_s.get('wins', 0)} | Win rate: {_pct(other_s.get('win_rate'))}\n"
        f"\n--- Overall (last 90 days) ---\n"
        f"Win rate: {_pct(metrics.get('win_rate'))} | Avg P&L: {metrics.get('avg_pnl_r', 'N/A')}R\n"
        f"Sharpe: {metrics.get('sharpe_ratio', 'N/A')} | Max drawdown: {metrics.get('max_drawdown_r', 'N/A')}R\n"
        f"Wins: {metrics.get('wins', 0)} | Losses: {metrics.get('losses', 0)} | Expired: {metrics.get('expired', 0)}\n"
        f"\n--- Win rate by confidence bucket ---\n"
        + ("\n".join(bucket_lines) if bucket_lines else "  No bucket data")
        + cs_lines
    )

    raw: str | None = None
    import logging as _log
    _logger = _log.getLogger(__name__)

    # Anthropic with extended thinking needs higher max_tokens
    thinking_budget = int(cfg.get("thinking_budget") or 0)
    anthropic_max_tokens = (thinking_budget + 1024) if (provider_id == "anthropic" and thinking_budget > 0) else 1200

    try:
        if provider_id == "openai":
            raw = await _call_openai(model, _BACKTEST_INSIGHT_SYSTEM, user_content, api_key or "", max_tokens=1200)
        elif provider_id == "anthropic":
            raw = await _call_anthropic(
                model, _BACKTEST_INSIGHT_SYSTEM, user_content, api_key or "",
                max_tokens=anthropic_max_tokens, thinking_budget=thinking_budget,
            )
        elif provider_id == "ollama":
            combined = f"{_BACKTEST_INSIGHT_SYSTEM.strip()}\n\n---\n\n{user_content.strip()}"
            raw = await _call_ollama(model, "", combined, ollama_base_url, num_predict=600, timeout=120.0)
        elif provider_id == "openrouter":
            raw = await _call_openrouter(model, _BACKTEST_INSIGHT_SYSTEM, user_content, api_key or "", max_tokens=1200)
        else:
            return None
    except Exception as exc:
        _logger.warning("Backtest insight LLM call failed (%s/%s): %s", provider_id, model, exc)
        return None

    _logger.debug("Backtest insight raw response (%s): %r", provider_id, (raw or "")[:300])
    parsed = _parse_backtest_insight_json(raw)
    if parsed is None:
        _logger.warning("Backtest insight JSON parse failed for %s. Raw: %r", asset_id, (raw or "")[:200])
    return parsed


# ── Gap Risk Advisor ──────────────────────────────────────────────────────────

_GAP_ADVICE_SYSTEM = (
    "You are a risk manager advising a retail trader on an active position that has elevated overnight gap risk. "
    "Your job: analyze the provided trade data and give ONE specific, actionable recommendation in plain English. "
    "Be direct and concrete. Maximum 25 words. No markdown, no bullet points, no disclaimers. "
    "Examples of good output: "
    "'Reduce to half size before Friday close; this short spans 2 weekends with bearish momentum.' "
    "'Consider closing 30% now — high ATR plus earnings in 3 days makes overnight gap destructive.' "
    "'Hold but place a tighter mental stop; gap avg is small relative to your SL buffer.'"
)


async def generate_gap_advice(context: dict[str, Any]) -> str | None:
    """
    Generate a short LLM gap risk recommendation for an active trade.
    Uses extended thinking (Anthropic) or standard call (other providers).
    Returns a single plain-text sentence or None if LLM unavailable.
    """
    import logging as _log
    _logger = _log.getLogger(__name__)

    from app.core.config import settings
    from app.core.preferences import get_research_llm_config

    cfg = get_research_llm_config()
    provider_id = (cfg.get("research_llm_provider") or "openai").strip().lower()
    provider = get_provider_by_id(provider_id)
    if not provider:
        provider_id = "openai"
        provider = get_provider_by_id("openai")

    api_key = _get_api_key_for_provider(provider_id)
    if not is_available(provider_id, api_key):
        return None

    model = (cfg.get("research_llm_model") or "").strip() or provider.default_model
    if not model:
        return None

    # Build user message from context
    asset      = context.get("asset_id", "Unknown")
    direction  = context.get("direction", "long").upper()
    category   = context.get("category", "stocks")
    gap_level  = context.get("gap_level", "medium").upper()
    gap_factors = context.get("gap_factors", "")
    hold_days  = context.get("hold_days")
    atr_pct    = context.get("atr_pct")
    gap_avg    = context.get("overnight_gap_avg")
    gap_p95    = context.get("overnight_gap_p95")
    sentiment  = context.get("sentiment_score")
    regime     = context.get("market_regime", "unknown")
    near_earn  = context.get("near_earnings", False)
    days_earn  = context.get("days_to_earnings")
    confidence = context.get("confidence_score")
    weekday    = context.get("weekday_name", "")

    parts = [
        f"Asset: {asset} ({category}, {direction}).",
        f"Gap risk level: {gap_level}. Active risk factors: {gap_factors or 'hold period, category'}.",
    ]
    if hold_days is not None:
        parts.append(f"Days until expiry: {hold_days}.")
    if weekday:
        parts.append(f"Today is {weekday}.")
    if atr_pct is not None:
        parts.append(f"ATR (daily %): {atr_pct*100:.2f}%.")
    if gap_avg is not None:
        parts.append(f"Avg overnight gap: {gap_avg*100:.2f}%.")
    if gap_p95 is not None:
        parts.append(f"95th-pct overnight gap: {gap_p95*100:.2f}%.")
    if sentiment is not None:
        sentiment_label = "bullish" if sentiment > 0.05 else "bearish" if sentiment < -0.05 else "neutral"
        parts.append(f"Current sentiment: {sentiment:.2f} ({sentiment_label}).")
    if regime and regime != "unknown":
        parts.append(f"Market regime: {regime}.")
    if confidence is not None:
        parts.append(f"Signal confidence: {confidence}%.")
    if near_earn and days_earn is not None:
        parts.append(f"Earnings report in {days_earn} day(s) — volatility spike risk.")

    user_content = " ".join(parts) + "\n\nWhat should the trader do about gap risk on this position?"

    ollama_base_url = (cfg.get("ollama_base_url") or "").strip() or (settings.ollama_base_url or "http://localhost:11434")

    # Extended thinking for Anthropic (small budget — focused question)
    THINKING_BUDGET = 2000

    try:
        if provider_id == "openai":
            return await _call_openai(model, _GAP_ADVICE_SYSTEM, user_content, api_key or "", max_tokens=80)
        elif provider_id == "anthropic":
            return await _call_anthropic(
                model, _GAP_ADVICE_SYSTEM, user_content, api_key or "",
                max_tokens=THINKING_BUDGET + 80, thinking_budget=THINKING_BUDGET,
            )
        elif provider_id == "ollama":
            combined = f"{_GAP_ADVICE_SYSTEM.strip()}\n\n---\n\n{user_content.strip()}"
            return await _call_ollama(model, "", combined, ollama_base_url, num_predict=60, timeout=60.0)
        elif provider_id == "openrouter":
            return await _call_openrouter(model, _GAP_ADVICE_SYSTEM, user_content, api_key or "", max_tokens=80)
    except Exception as exc:
        _logger.warning("Gap advice LLM call failed (%s/%s): %s", provider_id, model, exc)

    return None
