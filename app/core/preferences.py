"""
User preferences (e.g. risk stance, research LLM). Stored in data/preferences.json; config used as fallback.
"""
from pathlib import Path
import json
from typing import Any

from app.core.config import settings

VALID_RISK = frozenset({"conservative", "moderate", "aggressive"})
VALID_LLM_PROVIDERS = frozenset({"openai", "anthropic", "ollama", "openrouter"})


def _prefs_path() -> Path:
    p = settings.data_dir / "preferences.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_prefs() -> dict[str, Any]:
    path = _prefs_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_prefs(data: dict[str, Any]) -> None:
    path = _prefs_path()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_preferred_risk() -> str:
    """Return preferred_risk from preferences file, or from config if missing/invalid."""
    data = _load_prefs()
    risk = (data.get("preferred_risk") or "").strip().lower()
    return risk if risk in VALID_RISK else settings.preferred_risk


def set_preferred_risk(value: str) -> str:
    """Save preferred_risk; must be conservative | moderate | aggressive. Returns saved value."""
    risk = value.strip().lower()
    if risk not in VALID_RISK:
        raise ValueError(f"preferred_risk must be one of: {sorted(VALID_RISK)}")
    data = _load_prefs()
    data["preferred_risk"] = risk
    _save_prefs(data)
    return risk


def get_research_llm_config() -> dict[str, Any]:
    """Return research LLM provider, model, ollama_base_url from preferences or config."""
    data = _load_prefs()
    provider = (data.get("research_llm_provider") or "").strip().lower()
    if provider not in VALID_LLM_PROVIDERS:
        provider = (settings.research_llm_provider or "openai").strip().lower()
        if provider not in VALID_LLM_PROVIDERS:
            provider = "openai"
    model = (data.get("research_llm_model") or "").strip() or (settings.research_llm_model or "").strip()
    ollama_base_url = (data.get("ollama_base_url") or "").strip() or (settings.ollama_base_url or "http://localhost:11434").strip()
    return {"research_llm_provider": provider, "research_llm_model": model, "ollama_base_url": ollama_base_url}


def set_research_llm_config(provider: str | None = None, model: str | None = None, ollama_base_url: str | None = None) -> dict[str, Any]:
    """Save research LLM preferences. Omit a key to leave unchanged. Returns current full config."""
    data = _load_prefs()
    if provider is not None:
        p = provider.strip().lower()
        if p and p not in VALID_LLM_PROVIDERS:
            raise ValueError(f"research_llm_provider must be one of: {sorted(VALID_LLM_PROVIDERS)}")
        data["research_llm_provider"] = p or data.get("research_llm_provider", settings.research_llm_provider or "openai")
    if model is not None:
        data["research_llm_model"] = model.strip()
    if ollama_base_url is not None:
        data["ollama_base_url"] = ollama_base_url.strip() or "http://localhost:11434"
    _save_prefs(data)
    return get_research_llm_config()


# Sum of per-trade suggested_risk_pct values (each is % of capital, e.g. 0.5 = 0.5%).
# Default 10.0 ≈ ten trades at ~1% suggested risk — not "one percent total" (see README).
# Sum of Size % across all recs; default fits ~20 small positions (e.g. 20 × ~1%).
_DEFAULT_RISK_BUDGET = 25.0
_MAX_RISK_BUDGET = 50.0


def get_risk_budget_1r() -> float:
    """Return max sum of suggested_risk_pct (%) allowed per batch. Default allows a normal-sized batch."""
    data = _load_prefs()
    val = data.get("risk_budget_1r")
    if val is None:
        return _DEFAULT_RISK_BUDGET
    try:
        return max(0.0, float(val))
    except (ValueError, TypeError):
        return _DEFAULT_RISK_BUDGET


def set_risk_budget_1r(value: float) -> float:
    """Save risk_budget_1r (0.0–50.0). Returns saved value."""
    val = max(0.0, min(_MAX_RISK_BUDGET, float(value)))
    data = _load_prefs()
    data["risk_budget_1r"] = val
    _save_prefs(data)
    return val


# Sentiment decay half-life (hours). Default 24h = items from 24h ago have half the weight.
_DEFAULT_SENTIMENT_HALF_LIFE = 24.0
_MIN_SENTIMENT_HALF_LIFE = 1.0
_MAX_SENTIMENT_HALF_LIFE = 168.0  # 1 week


def get_sentiment_half_life() -> float:
    """Return sentiment decay half-life in hours. Default 24h."""
    data = _load_prefs()
    val = data.get("sentiment_half_life_hours")
    if val is None:
        return _DEFAULT_SENTIMENT_HALF_LIFE
    try:
        return max(_MIN_SENTIMENT_HALF_LIFE, min(_MAX_SENTIMENT_HALF_LIFE, float(val)))
    except (ValueError, TypeError):
        return _DEFAULT_SENTIMENT_HALF_LIFE


def set_sentiment_half_life(value: float) -> float:
    """Save sentiment decay half-life (1.0–168.0 hours). Returns saved value."""
    val = max(_MIN_SENTIMENT_HALF_LIFE, min(_MAX_SENTIMENT_HALF_LIFE, float(value)))
    data = _load_prefs()
    data["sentiment_half_life_hours"] = val
    _save_prefs(data)
    return val
