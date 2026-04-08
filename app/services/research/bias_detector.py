"""
P13: Hallucination bias detector.

build_hallucination_profile() — weekly job aggregating V4 flag rates per
provider/model from llm_rationale_logs for the past `days_back` days.
Writes result to data/hallucination_profile.json.

build_hallucination_guard() — P14: reads the profile and returns a
personalised anti-hallucination instruction block to inject into the
system prompt.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_logger = logging.getLogger(__name__)

_PROFILE_PATH = Path(os.getenv("HALLUCINATION_PROFILE_PATH", "data/hallucination_profile.json"))

# Minimum logs needed before we emit a guard for that provider/model
_MIN_SAMPLE = 10

# Flag rate threshold above which we emit a specific warning
_WARN_THRESHOLD = 0.25


# ── P13: Profile builder ──────────────────────────────────────────────────────

async def build_hallucination_profile(
    session: AsyncSession,
    days_back: int = 30,
) -> dict[str, Any]:
    """
    Aggregate V4a/V4b/V4c/V4d flag rates per (provider_id, model) from
    llm_rationale_logs for the past `days_back` days.

    Returns the profile dict and writes it atomically to
    data/hallucination_profile.json.
    """
    from datetime import datetime, timezone, timedelta
    from app.models.llm_rationale_log import LLMRationaleLog

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    q = (
        select(
            LLMRationaleLog.provider_id,
            LLMRationaleLog.model,
            LLMRationaleLog.validation_flags,
            LLMRationaleLog.retry_count,
        )
        .where(LLMRationaleLog.created_at >= cutoff)
    )
    rows = (await session.execute(q)).all()

    # Accumulate per (provider_id, model)
    buckets: dict[tuple[str, str], dict] = {}

    for provider_id, model, flags_raw, retry_count in rows:
        key = (str(provider_id or "unknown"), str(model or "unknown"))
        if key not in buckets:
            buckets[key] = {
                "total": 0,
                "v4a": 0,
                "v4b": 0,
                "v4c": 0,
                "v4d": 0,
                "any_v4": 0,
                "retried": 0,
            }
        b = buckets[key]
        b["total"] += 1

        flags: list[str] = []
        if flags_raw:
            try:
                flags = json.loads(flags_raw) if isinstance(flags_raw, str) else list(flags_raw)
            except Exception:
                pass

        has_v4 = False
        for flag in flags:
            # F2-1: flags are stored as full codes e.g. "V4a_price_hallucination"
            # → upper → "V4A_PRICE_HALLUCINATION". Must use startswith, not exact match.
            f = str(flag).upper()
            if f.startswith("V4A_"):
                b["v4a"] += 1
                has_v4 = True
            elif f.startswith("V4B_"):
                b["v4b"] += 1
                has_v4 = True
            elif f.startswith("V4C_"):
                b["v4c"] += 1
                has_v4 = True
            elif f.startswith("V4D_"):
                b["v4d"] += 1
                has_v4 = True
        if has_v4:
            b["any_v4"] += 1
        if retry_count and int(retry_count) > 0:
            b["retried"] += 1

    # Build profile entries
    profile_entries: list[dict] = []
    for (provider_id, model), b in buckets.items():
        total = b["total"]
        entry: dict[str, Any] = {
            "provider_id": provider_id,
            "model": model,
            "sample_size": total,
            "days_back": days_back,
        }
        if total >= 1:
            entry["v4a_rate"] = round(b["v4a"] / total, 4)
            entry["v4b_rate"] = round(b["v4b"] / total, 4)
            entry["v4c_rate"] = round(b["v4c"] / total, 4)
            entry["v4d_rate"] = round(b["v4d"] / total, 4)
            entry["any_v4_rate"] = round(b["any_v4"] / total, 4)
            entry["retry_rate"] = round(b["retried"] / total, 4)
        profile_entries.append(entry)

    from datetime import datetime, timezone
    profile = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days_back": days_back,
        "entries": profile_entries,
    }

    # Write atomically
    try:
        _PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PROFILE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(profile, indent=2))
        tmp.replace(_PROFILE_PATH)
        _logger.info(
            "[HALLUCINATION_PROFILE] Written %d entries to %s", len(profile_entries), _PROFILE_PATH
        )
    except Exception as exc:
        _logger.warning("[HALLUCINATION_PROFILE] Failed to write profile: %s", exc)

    return profile


# ── P14: Guard builder ────────────────────────────────────────────────────────

def build_hallucination_guard(
    provider_id: str,
    model: str,
) -> str:
    """
    Read the persisted hallucination profile and return a personalised
    anti-hallucination instruction block for the given provider/model.

    Returns empty string when:
    - profile file does not exist
    - sample size < _MIN_SAMPLE
    - no flag rates exceed _WARN_THRESHOLD
    """
    try:
        if not _PROFILE_PATH.exists():
            return ""
        raw = _PROFILE_PATH.read_text()
        profile = json.loads(raw)
    except Exception as exc:
        _logger.debug("[HALLUCINATION_GUARD] Could not load profile: %s", exc)
        return ""

    entries: list[dict] = profile.get("entries") or []
    entry: dict | None = None
    for e in entries:
        if e.get("provider_id") == provider_id and e.get("model") == model:
            entry = e
            break

    if not entry or entry.get("sample_size", 0) < _MIN_SAMPLE:
        return ""

    warnings: list[str] = []

    if entry.get("v4a_rate", 0) >= _WARN_THRESHOLD:
        warnings.append(
            f"You have cited prices that deviate from the actual entry price in "
            f"{round(entry['v4a_rate'] * 100)}% of recent outputs. "
            "Always use ONLY the exact prices provided in the context."
        )

    if entry.get("v4b_rate", 0) >= _WARN_THRESHOLD:
        warnings.append(
            f"You have stated incorrect indicator values (RSI / ADX / ATR / MACD) in "
            f"{round(entry['v4b_rate'] * 100)}% of recent outputs. "
            "Use ONLY the exact indicator values from the context — never approximate."
        )

    if entry.get("v4c_rate", 0) >= _WARN_THRESHOLD:
        warnings.append(
            f"You have referenced news events that were not in the provided headlines in "
            f"{round(entry['v4c_rate'] * 100)}% of recent outputs. "
            "Only cite news explicitly listed in the Headlines section."
        )

    if entry.get("v4d_rate", 0) >= _WARN_THRESHOLD:
        warnings.append(
            f"You have made temporal errors (wrong time frames, future events as past) in "
            f"{round(entry['v4d_rate'] * 100)}% of recent outputs. "
            "Be precise about time frames — do not invent dates or durations."
        )

    if not warnings:
        return ""

    lines = ["IMPORTANT — based on your recent outputs, watch for these specific errors:"]
    for i, w in enumerate(warnings, 1):
        lines.append(f"{i}. {w}")
    return "\n".join(lines)
