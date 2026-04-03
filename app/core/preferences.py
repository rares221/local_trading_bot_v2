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


# Confidence thresholds per regime (minimum confidence to include a recommendation)
_DEFAULT_CONFIDENCE_THRESHOLDS = {"risk_on": 30, "cautious": 35, "risk_off": 40}
_MIN_CONFIDENCE = 0
_MAX_CONFIDENCE = 80


def get_confidence_thresholds() -> dict[str, int]:
    """Return min confidence thresholds per regime. Defaults: risk_on=30, cautious=35, risk_off=40."""
    data = _load_prefs()
    thresholds = data.get("confidence_thresholds")
    if not isinstance(thresholds, dict):
        return _DEFAULT_CONFIDENCE_THRESHOLDS.copy()
    result = _DEFAULT_CONFIDENCE_THRESHOLDS.copy()
    for regime in ("risk_on", "cautious", "risk_off"):
        if regime in thresholds:
            try:
                val = int(thresholds[regime])
                result[regime] = max(_MIN_CONFIDENCE, min(_MAX_CONFIDENCE, val))
            except (ValueError, TypeError):
                pass
    return result


def set_confidence_thresholds(
    risk_on: int | None = None,
    cautious: int | None = None,
    risk_off: int | None = None,
) -> dict[str, int]:
    """Save confidence thresholds per regime (0-80). Returns current thresholds."""
    data = _load_prefs()
    current = data.get("confidence_thresholds") or {}
    if not isinstance(current, dict):
        current = {}
    
    if risk_on is not None:
        current["risk_on"] = max(_MIN_CONFIDENCE, min(_MAX_CONFIDENCE, int(risk_on)))
    if cautious is not None:
        current["cautious"] = max(_MIN_CONFIDENCE, min(_MAX_CONFIDENCE, int(cautious)))
    if risk_off is not None:
        current["risk_off"] = max(_MIN_CONFIDENCE, min(_MAX_CONFIDENCE, int(risk_off)))
    
    data["confidence_thresholds"] = current
    _save_prefs(data)
    return get_confidence_thresholds()


# A/B shadow mode: alternate min-confidence gate (logged per recommendation; live batch unchanged)
_DEFAULT_SHADOW_DELTA = 5
_MIN_SHADOW_DELTA = -20
_MAX_SHADOW_DELTA = 30


def get_shadow_mode_config() -> dict[str, Any]:
    """Return shadow_mode_enabled and confidence_delta (applied to live min confidence for profile B)."""
    data = _load_prefs()
    enabled = bool(data.get("shadow_mode_enabled"))
    raw = data.get("shadow_confidence_delta", _DEFAULT_SHADOW_DELTA)
    try:
        delta = int(raw)
    except (TypeError, ValueError):
        delta = _DEFAULT_SHADOW_DELTA
    delta = max(_MIN_SHADOW_DELTA, min(_MAX_SHADOW_DELTA, delta))
    return {
        "enabled": enabled,
        "confidence_delta": delta if enabled else 0,
        "confidence_delta_stored": delta,
    }


def set_shadow_mode(
    enabled: bool | None = None,
    confidence_delta: int | None = None,
) -> dict[str, Any]:
    """Persist shadow toggles. Delta is clamped to [-20, 30]. Positive = stricter profile B."""
    data = _load_prefs()
    if enabled is not None:
        data["shadow_mode_enabled"] = bool(enabled)
    if confidence_delta is not None:
        data["shadow_confidence_delta"] = max(
            _MIN_SHADOW_DELTA, min(_MAX_SHADOW_DELTA, int(confidence_delta))
        )
    _save_prefs(data)
    return get_shadow_mode_config()


# Commodity-specific gates: futures/spot names often sit below equity-style confidence + R:R bars.
_DEFAULT_COMMODITY_CONF_OFFSET = -5
_MIN_COMMODITY_CONF_OFFSET = -30
_MAX_COMMODITY_CONF_OFFSET = 20

_DEFAULT_COMMODITY_MIN_RR = 1.25
_MIN_COMMODITY_RR = 1.05
_MAX_COMMODITY_RR = 2.5


def get_commodity_engine_gates() -> dict[str, int | float]:
    """
    Return gates used only for category ``commodities``:
    - commodity_confidence_offset: added to the regime min confidence (negative = looser).
    - commodity_min_risk_reward: minimum R:R before a commodity idea is skipped.
    """
    data = _load_prefs()
    off = data.get("commodity_confidence_offset", _DEFAULT_COMMODITY_CONF_OFFSET)
    try:
        off_i = int(off)
    except (TypeError, ValueError):
        off_i = _DEFAULT_COMMODITY_CONF_OFFSET
    off_i = max(_MIN_COMMODITY_CONF_OFFSET, min(_MAX_COMMODITY_CONF_OFFSET, off_i))

    raw_rr = data.get("commodity_min_risk_reward", _DEFAULT_COMMODITY_MIN_RR)
    try:
        rr = float(raw_rr)
    except (TypeError, ValueError):
        rr = _DEFAULT_COMMODITY_MIN_RR
    rr = max(_MIN_COMMODITY_RR, min(_MAX_COMMODITY_RR, rr))

    return {
        "commodity_confidence_offset": off_i,
        "commodity_min_risk_reward": rr,
    }


def set_commodity_engine_gates(
    confidence_offset: int | None = None,
    min_risk_reward: float | None = None,
) -> dict[str, int | float]:
    """Persist commodity-only engine gates. Offset clamped [-30, 20]; R:R clamped [1.05, 2.5]."""
    data = _load_prefs()
    if confidence_offset is not None:
        data["commodity_confidence_offset"] = max(
            _MIN_COMMODITY_CONF_OFFSET,
            min(_MAX_COMMODITY_CONF_OFFSET, int(confidence_offset)),
        )
    if min_risk_reward is not None:
        data["commodity_min_risk_reward"] = max(
            _MIN_COMMODITY_RR, min(_MAX_COMMODITY_RR, float(min_risk_reward)),
        )
    _save_prefs(data)
    return get_commodity_engine_gates()
