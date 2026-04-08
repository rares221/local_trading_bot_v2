"""
P15: Few-shot example selector.

get_verified_few_shot_examples() — queries llm_rationale_logs for verified
high-quality rationales and formats them as a few-shot block to inject into
the user message.

Selection criteria:
  - outcome IS NOT NULL (trade has closed)
  - metrics_accuracy_score > 0.8 (indicator claims were accurate)
  - validation_flags contains no V4a or V4b flags (no price/indicator hallucinations)
  - rationale_text is non-empty

Bootstrap guard: returns empty string when fewer than MIN_OUTCOMES logs have
outcomes — not enough signal to pick reliable examples.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_logger = logging.getLogger(__name__)

MIN_OUTCOMES = 10       # minimum closed trades before we enable few-shot
MAX_EXAMPLES = 2        # inject at most 2 examples to keep prompt compact
_MIN_ACCURACY = 0.8     # metrics_accuracy_score threshold


async def get_verified_few_shot_examples(
    session: AsyncSession,
    provider_id: str,
    model: str,
    horizon: str = "near",
    limit: int = MAX_EXAMPLES,
) -> str:
    """
    Return a formatted few-shot block for insertion into the user message, or
    empty string if there are not enough verified examples yet.

    Preference order:
      1. Same provider + model + horizon
      2. Same horizon (any provider)
      3. Any verified example
    """
    from app.models.llm_rationale_log import LLMRationaleLog

    # Bootstrap guard — count total closed logs across all providers
    count_q = select(LLMRationaleLog.id).where(LLMRationaleLog.outcome.isnot(None))
    total_closed = len((await session.execute(count_q)).all())
    if total_closed < MIN_OUTCOMES:
        return ""

    # Fetch candidates — prefer same provider/model/horizon, fall back gracefully
    base_filter = [
        LLMRationaleLog.outcome.isnot(None),
        LLMRationaleLog.metrics_accuracy_score > _MIN_ACCURACY,
        LLMRationaleLog.rationale_text.isnot(None),
    ]

    examples: list[dict] = []

    for provider_filter, model_filter, horizon_filter in [
        (provider_id, model, horizon),   # exact match
        (None, None, horizon),            # same horizon, any model
        (None, None, None),               # anything
    ]:
        if len(examples) >= limit:
            break

        filters = list(base_filter)
        if provider_filter:
            filters.append(LLMRationaleLog.provider_id == provider_filter)
        if model_filter:
            filters.append(LLMRationaleLog.model == model_filter)

        q = (
            select(
                LLMRationaleLog.rationale_text,
                LLMRationaleLog.validation_flags,
                LLMRationaleLog.outcome,
                LLMRationaleLog.prompt_version,
            )
            .where(*filters)
            .order_by(LLMRationaleLog.created_at.desc())
            .limit(50)  # fetch more, filter below
        )

        rows = (await session.execute(q)).all()

        for rationale_text, flags_raw, outcome, prompt_version in rows:
            if len(examples) >= limit:
                break

            # Skip if any V4a/V4b flags present
            flags: list[str] = []
            if flags_raw:
                try:
                    flags = (
                        json.loads(flags_raw)
                        if isinstance(flags_raw, str)
                        else list(flags_raw)
                    )
                except Exception:
                    pass
            upper_flags = [f.upper() for f in flags]
            # F2-1: flags are stored as full codes e.g. "V4a_price_hallucination"
            # → upper → "V4A_PRICE_HALLUCINATION". Must use startswith, not exact match.
            if any(f.startswith("V4A_") or f.startswith("V4B_") for f in upper_flags):
                continue

            # Optional horizon filter (check prompt_version tag)
            if horizon_filter:
                pv = str(prompt_version or "").lower()
                if horizon_filter == "near" and "far" in pv:
                    continue
                if horizon_filter == "far" and "near" in pv:
                    continue

            # Avoid duplicates
            if any(e["text"] == rationale_text for e in examples):
                continue

            examples.append({"text": rationale_text, "outcome": outcome})

    if not examples:
        return ""

    lines = [
        "--- VERIFIED EXAMPLES (past rationales confirmed accurate after trade close) ---"
    ]
    for i, ex in enumerate(examples, 1):
        outcome_label = ex["outcome"] or "closed"
        lines.append(f"Example {i} (outcome: {outcome_label}):")
        lines.append(ex["text"].strip())
        lines.append("")
    lines.append("--- END EXAMPLES ---")

    return "\n".join(lines)
