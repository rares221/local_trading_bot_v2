# Domain-Aware Sentiment Correction

**Date:** 2026-04-13  
**Status:** Implemented (not yet in production)  
**Scope:** All macro-linked assets (energy, precious metals, FX)

---

## Problem

The VADER/FinBERT sentiment model reads article tone, not commodity supply/demand dynamics. For macro-linked assets, geopolitical and supply-disruption language is structurally bullish but lexically negative:

- "Blockade of Strait of Hormuz" → VADER: -0.296 → but is **bullish for oil**
- "Oil Tankers U-Turn as Talks Break Down" → VADER: -0.802 → but is **bullish for oil**
- "War Drives Oil Shock" → VADER: -0.572 → but is **bullish for oil**

This causes the recommendations engine to generate SHORT signals on oil during supply crises — the opposite of what geopolitics implies. The same misscoring pattern applies to gold (safe-haven demand), USD (hawkish Fed language), and risk currencies.

---

## Approach: LLM-Based Narrative Classification

A keyword-based approach was evaluated and rejected as brittle (requires manual maintenance of keyword lists, breaks for novel terminology and paraphrasing). Instead, the LLM classifies the dominant supply/demand narrative from article headlines.

**Why LLM:**
- Handles novel geopolitical events and emerging terminology without code changes
- Understands context: "tankers U-turn as talks break down" reads as bearish tone but is bullish supply disruption
- One call per macro-linked tag per pipeline run (~4-6 calls total, low cost)

---

## Scope

Correction applies to assets in `MACRO_LINKED_TAGS` registry in `domain_correction.py`:
- **Energy:** `oil`, `WTI`, `Brent`, `gas`, `crude`, `CL=F`, `BZ=F`, `NG=F`
- **Precious metals:** `gold`, `silver`, `XAUUSD`, `GC=F`, `SI=F`
- **USD-bullish FX:** `USDJPY=X`, `USDCAD=X`, `DXY`
- **Risk-currency FX:** `EURUSD=X`, `GBPUSD=X`, `AUDUSD=X`

Indices are excluded — directional mapping is ambiguous for equity indices.

---

## Architecture

### New file: `app/services/sentiment/domain_correction.py`

Self-contained module. No dependencies on engine or aggregate internals.

**Exports:**
- `MACRO_LINKED_TAGS: dict[str, str]` — maps asset tag → human-readable description passed to LLM
- `apply_domain_correction(tag, raw_avg, titles, blend_weight) -> (float, bool)` — main entry point
- `_classify_narrative(tag, asset_description, titles) -> float | None` — makes the LLM call
- `_parse_score(response, tag) -> float | None` — parses LLM JSON response
- `_dispatch_llm(provider_id, model, system, user) -> str | None` — routes to correct provider

**LLM provider resolution:** Uses `get_research_llm_config()` from `app.core.preferences` — respects UI-configured provider preferences before falling back to `.env` settings.

### Modified file: `app/services/analysis/aggregate.py`

Two changes only:

1. **Build `tag_titles`** alongside `by_asset_weighted` — collects `(title + description)` text per tag
2. **Apply correction** after computing `weighted_avg` — calls `apply_domain_correction`, stores result as `avg`, adds `domain_correction_applied: bool` to the tag's dict for audit

No changes to function signatures, no new imports elsewhere.

---

## Correction Formula

```
corrected_avg = raw_avg * (1 - CORRECTION_BLEND_WEIGHT) + llm_score * CORRECTION_BLEND_WEIGHT
```

**Constants:**
- `CORRECTION_BLEND_WEIGHT = 0.40` — 40% LLM domain score, 60% raw VADER (conservative to start)
- `MIN_ARTICLES_FOR_LLM = 3` — minimum articles required; below this the LLM is not called
- LLM score clamped to `[-1.0, 1.0]`

**Fallback:** if tag is not macro-linked, article count < MIN, LLM provider not configured, or LLM call fails — raw_avg is returned unchanged and `domain_correction_applied = false`.

---

## LLM Prompt

**System:** Instructs the LLM to classify as a macro price-sentiment classifier. Provides explicit guidelines per asset class (e.g. "energy: supply disruption → bullish for price"). Instructs response as `{"score": float, "reasoning": str}` JSON only.

**User:** Asset description + numbered list of up to 20 recent headlines.

---

## Audit Trail

`by_asset_sentiment[tag]` gains one new field:

```json
{
  "avg": -0.12,
  "count": 14,
  "sentiment_trend": "worsening",
  "price_trend": "up",
  "divergence": true,
  "domain_correction_applied": true
}
```

Stored in `market_analyses.payload`. No schema migration required.

---

## What This Does Not Change

- `feed_items.sentiment_score` — raw scores are never modified
- Engine function signatures — no changes to `determine_direction`, `compute_confidence`, or `build_daily_candidates`
- Backtest — naturally benefits because backtests consume `market_analyses` records
- Live analyzer — currently queries `feed_items` directly (pre-existing gap, separate work item)

---

## Testing

Unit tests: `tests/unit/sentiment/test_domain_correction.py`
- `_parse_score`: valid JSON, clamping, markdown fence stripping, invalid input
- `apply_domain_correction`: non-macro tag passthrough, article count guard, blend formula, LLM failure fallback, all MACRO_LINKED_TAGS recognized
- LLM call is mocked — tests run without a real provider configured
