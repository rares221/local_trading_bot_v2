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
    model: str, system: str, user: str, api_key: str, *, max_tokens: int = 200
) -> str | None:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)
    msg = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if msg.content and len(msg.content) > 0 and hasattr(msg.content[0], "text"):
        return msg.content[0].text.strip()
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

    try:
        if provider_id == "openai":
            return await _call_openai(model, system, user, api_key or "", max_tokens=200)
        if provider_id == "anthropic":
            return await _call_anthropic(model, system, user, api_key or "", max_tokens=200)
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
