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


SYSTEM_PROMPT = (
    "You are a concise trading research assistant. In 2–4 sentences only, "
    "explain why this trade idea might make sense and the main risk. "
    "No disclaimers; factual and neutral."
)


def _format_user_content(context: dict[str, Any]) -> str:
    sentiment = context.get("sentiment_avg")
    price = context.get("buy_in_price")
    risk = context.get("risk_tier")
    headlines = context.get("headlines") or []
    asset = context.get("asset_id", "this asset")
    parts = [f"Asset: {asset}."]
    if sentiment is not None:
        parts.append(f"Recent sentiment (avg): {sentiment:.3f}.")
    if price is not None:
        parts.append(f"Current/reference price: {price}.")
    if risk:
        parts.append(f"Risk tier: {risk}.")
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


async def _call_openai(model: str, system: str, user: str, api_key: str) -> str | None:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key)
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=200,
    )
    if response.choices and response.choices[0].message.content:
        return response.choices[0].message.content.strip()
    return None


async def _call_openrouter(model: str, system: str, user: str, api_key: str) -> str | None:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=200,
    )
    if response.choices and response.choices[0].message.content:
        return response.choices[0].message.content.strip()
    return None


async def _call_anthropic(model: str, system: str, user: str, api_key: str) -> str | None:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)
    msg = await client.messages.create(
        model=model,
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if msg.content and len(msg.content) > 0 and hasattr(msg.content[0], "text"):
        return msg.content[0].text.strip()
    return None


async def _call_ollama(model: str, system: str, user: str, base_url: str) -> str | None:
    import httpx
    # Ollama chat API: system + user as a single prompt or separate messages
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{base_url.rstrip('/')}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()
    msg = data.get("message")
    if msg and isinstance(msg.get("content"), str):
        return msg["content"].strip()
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
            return await _call_openai(model, system, user, api_key or "")
        if provider_id == "anthropic":
            return await _call_anthropic(model, system, user, api_key or "")
        if provider_id == "ollama":
            return await _call_ollama(model, system, user, ollama_base_url)
        if provider_id == "openrouter":
            return await _call_openrouter(model, system, user, api_key or "")
    except Exception:
        pass
    return None
