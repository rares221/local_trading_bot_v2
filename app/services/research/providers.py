"""
Canonical LLM provider registry for research rationale (Dexter-style).
Single source of truth: add a provider here; llm.py dispatches by research_llm_provider.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ProviderDef:
    """Slug used in config (e.g. 'anthropic')."""
    id: str
    display_name: str
    """Env var for API key. None for local (e.g. Ollama)."""
    api_key_env_var: Optional[str] = None
    """Default model for this provider when not specified in config."""
    default_model: str = ""


PROVIDERS: list[ProviderDef] = [
    ProviderDef(
        id="openai",
        display_name="OpenAI",
        api_key_env_var="OPENAI_API_KEY",
        default_model="gpt-4o-mini",
    ),
    ProviderDef(
        id="anthropic",
        display_name="Anthropic",
        api_key_env_var="ANTHROPIC_API_KEY",
        default_model="claude-3-5-haiku-20241022",
    ),
    ProviderDef(
        id="ollama",
        display_name="Ollama",
        api_key_env_var=None,
        default_model="llama3.1",
    ),
    ProviderDef(
        id="openrouter",
        display_name="OpenRouter",
        api_key_env_var="OPENROUTER_API_KEY",
        default_model="openai/gpt-4o-mini",
    ),
]


def get_provider_by_id(provider_id: str) -> Optional[ProviderDef]:
    return next((p for p in PROVIDERS if p.id == provider_id), None)


def is_available(provider_id: str, api_key: Optional[str] = None) -> bool:
    """True if this provider can be used (has key or is local)."""
    p = get_provider_by_id(provider_id)
    if not p:
        return False
    if p.api_key_env_var is None:
        return True  # e.g. Ollama
    return bool(api_key)
