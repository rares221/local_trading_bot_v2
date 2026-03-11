"""
User preferences (e.g. risk stance). Stored in data/preferences.json; config used as fallback.
"""
from pathlib import Path
import json

from app.core.config import settings

VALID_RISK = frozenset({"conservative", "moderate", "aggressive"})


def _prefs_path() -> Path:
    p = settings.data_dir / "preferences.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_preferred_risk() -> str:
    """Return preferred_risk from preferences file, or from config if missing/invalid."""
    path = _prefs_path()
    if not path.exists():
        return settings.preferred_risk
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        risk = (data.get("preferred_risk") or "").strip().lower()
        return risk if risk in VALID_RISK else settings.preferred_risk
    except Exception:
        return settings.preferred_risk


def set_preferred_risk(value: str) -> str:
    """Save preferred_risk; must be conservative | moderate | aggressive. Returns saved value."""
    risk = value.strip().lower()
    if risk not in VALID_RISK:
        raise ValueError(f"preferred_risk must be one of: {sorted(VALID_RISK)}")
    path = _prefs_path()
    data = {"preferred_risk": risk}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return risk
