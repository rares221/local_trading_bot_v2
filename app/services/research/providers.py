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
    """Maximum extended thinking budget in tokens. 0 = provider does not support thinking."""
    max_thinking_budget: int = 0


PROVIDERS: list[ProviderDef] = [
    ProviderDef(
        id="openai",
        display_name="OpenAI",
        api_key_env_var="OPENAI_API_KEY",
        default_model="gpt-4o-mini",
        max_thinking_budget=0,  # standard models; reasoning models (o1/o3/o4-mini) not yet supported
    ),
    ProviderDef(
        id="anthropic",
        display_name="Anthropic",
        api_key_env_var="ANTHROPIC_API_KEY",
        default_model="claude-opus-4-6",
        # claude-opus-4-6 / claude-sonnet-4-6 support extended thinking up to 32000 tokens.
        # Practical cap at 16000 for latency/cost balance; raise in .env to go higher.
        max_thinking_budget=32000,
    ),
    ProviderDef(
        id="ollama",
        display_name="Ollama",
        api_key_env_var=None,
        default_model="llama3.1",
        max_thinking_budget=0,  # local models do not support thinking tokens
    ),
    ProviderDef(
        id="openrouter",
        display_name="OpenRouter",
        api_key_env_var="OPENROUTER_API_KEY",
        default_model="openai/gpt-4o-mini",
        max_thinking_budget=0,  # depends on routed model; not enforced here
    ),
    ProviderDef(
        id="lm_studio",
        display_name="LM Studio (Local)",
        api_key_env_var=None,   # no API key needed
        default_model="",       # user specifies the loaded model
        max_thinking_budget=0,
    ),
]


def get_max_thinking_budget(provider_id: str) -> int:
    """Return the maximum thinking budget in tokens for this provider. 0 = not supported."""
    p = get_provider_by_id(provider_id)
    return p.max_thinking_budget if p else 0


def clamp_thinking_budget(provider_id: str, requested: int) -> int:
    """
    Clamp a requested thinking_budget to the provider's maximum.
    Returns 0 if the provider does not support extended thinking.
    """
    max_budget = get_max_thinking_budget(provider_id)
    if max_budget == 0:
        return 0
    return max(0, min(requested, max_budget))


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
