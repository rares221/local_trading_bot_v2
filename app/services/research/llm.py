"""
Multi-provider LLM dispatch for research rationale (Dexter-style).
Supports OpenAI, Anthropic, Ollama, OpenRouter; uses research_llm_provider + research_llm_model from config.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.services.research.providers import (
    get_provider_by_id,
    is_available,
)

_logger = logging.getLogger(__name__)

PROMPT_VERSION_RATIONALE = "rationale_v1"


STRATEGY_EXPLAIN_SYSTEM = (
    "You are an experienced trading coach. Explain the user's chosen execution strategy "
    "(scale-out / targets) in clear, plain language. "
    "Use 1–2 short paragraphs or a few bullets. Cover: each exit level and position share, "
    "combined risk if stopped before TPs, one pro and one con vs a single full TP. "
    "Use the numbers from the user message. No long disclaimers. "
    "Target about 100–180 words."
)


SYSTEM_PROMPT_NEAR = """\
You are a concise trading research assistant analyzing a SHORT-TERM trade \
(horizon: {horizon_days} trading days, ~{calendar_days} calendar days).

In 2-4 sentences, explain why the SPECIFIC direction ({direction}) makes sense \
FOR THIS TIMEFRAME and identify the most critical near-term risk.

Direction rules (mandatory):
- SHORT direction: explain why price will DECLINE in the next {horizon_days} days. \
Do NOT argue for a bullish position.
- LONG direction: explain why price will RISE in the next {horizon_days} days. \
Do NOT argue for a bearish position.

Data accuracy rules (mandatory — violations are detected and logged):
- Use ONLY indicator values explicitly stated in the provided data.
- Use ONLY prices from context (entry, SL, TP, VPOC, VAH, VAL).
- Reference ONLY headlines explicitly provided below. Do not add news from memory.
- Do not approximate or round technical values.

Required content:
- Reference at least 2 specific indicators with their exact provided values.
- If macro events are listed in the trade window, assess their impact on direction.
- If sentiment contradicts direction, acknowledge and explain the technical override.
- If SL/TP vs Value Area data is provided, mention whether it supports or weakens the setup.

Forbidden:
- Long-term language ("over the coming months", "fundamentally undervalued").
- Generic statements without specific provided data.
- Invented price levels not in context.
- Disclaimers.
{hallucination_guard}"""

SYSTEM_PROMPT_FAR = """\
You are a concise trading research assistant analyzing a MEDIUM-TERM trade \
(horizon: {horizon_days} trading days, ~{calendar_weeks} calendar weeks).

In 2-4 sentences, explain why the SPECIFIC direction ({direction}) makes sense \
STRUCTURALLY OVER THIS TIMEFRAME and identify the most critical medium-term risk.

Direction rules (mandatory):
- SHORT direction: explain structural reasons price will trend LOWER. \
Do NOT argue for a bullish thesis.
- LONG direction: explain structural reasons price will trend HIGHER. \
Do NOT argue for a bearish thesis.

Data accuracy rules (mandatory):
- Use ONLY values explicitly stated in the provided data.
- Reference ONLY macro context provided (DXY trend, SPX regime, yields).
- Do not treat short-term technical patterns as primary drivers on this horizon.

Required content:
- Reference the structural trend (EMA/SMA alignment if provided, ADX trend strength).
- Reference macro context (DXY, market regime, yield environment) if provided.
- Acknowledge the increased uncertainty of a {calendar_weeks}-week prediction.
- If sector/fundamental data is provided, integrate it.

Forbidden:
- Short-term language ("this week", "by Friday", "intraday pattern").
- Generic momentum without trend context.
- Invented support/resistance levels.
- Disclaimers.
{hallucination_guard}"""


def _format_user_content(context: dict[str, Any]) -> str:
    asset       = context.get("asset_id", "this asset")
    direction   = context.get("direction", "long")
    horizon     = context.get("horizon", "near")
    expiry_days = context.get("expiry_days")
    sentiment   = context.get("sentiment_avg")
    sent_trend  = context.get("sentiment_trend")
    confidence  = context.get("confidence_score")
    price       = context.get("buy_in_price")
    risk        = context.get("risk_tier")
    market_reg  = context.get("market_regime")
    fundamentals = context.get("fundamentals") or {}
    headlines   = context.get("headlines") or []
    hl_ages     = context.get("headline_ages") or []
    upcoming    = context.get("upcoming_events") or []

    parts = [f"Asset: {asset}.", f"Direction: {direction}."]

    # ── Horizon (common) ─────────────────────────────────────────────────────
    if expiry_days is not None:
        cal_days = round(int(expiry_days) * 1.4)
        parts.append(f"Trade horizon: {horizon} ({expiry_days} trading days, ~{cal_days} calendar days).")

    # ── Confidence score (common) ─────────────────────────────────────────────
    if confidence is not None:
        parts.append(f"Engine confidence: {int(confidence)}/100.")

    # ── Sentiment (common) ───────────────────────────────────────────────────
    if sentiment is not None:
        parts.append(f"Recent sentiment (weighted avg): {sentiment:.3f}.")
        if sent_trend:
            parts.append(f"Sentiment trend (24h vs 24-72h window): {sent_trend}.")
        # Flag contradiction between sentiment polarity and trade direction
        if direction == "long" and sentiment < -0.05:
            parts.append("NOTE: Sentiment is negative but direction is LONG — technicals override.")
        elif direction == "short" and sentiment > 0.05:
            parts.append("NOTE: Sentiment is positive but direction is SHORT — technicals override.")

    if price is not None:
        parts.append(f"Current/reference price: {price}.")
    if risk:
        parts.append(f"Risk tier: {risk}.")

    # ── Market regime (common) ────────────────────────────────────────────────
    if market_reg:
        parts.append(f"Market regime: {market_reg}.")

    # ── Technicals (common) ──────────────────────────────────────────────────
    vol_notes = context.get("volatility_notes")
    if vol_notes:
        parts.append(f"Technicals: {vol_notes}.")

    # ── Fundamentals (common, with far-horizon extension) ────────────────────
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
        # Far-horizon only: P/E + 52-week position
        if horizon == "far":
            pe = fundamentals.get("pe_ratio")
            pos52 = fundamentals.get("position_52w")
            if pe is not None or pos52 is not None:
                far_f = []
                if pe is not None:
                    far_f.append(f"P/E: {pe}")
                if pos52 is not None:
                    far_f.append(f"52-week position: {pos52:.0f}%")
                parts.append(" | ".join(far_f) + ".")

    # ── Relative Strength (common) ────────────────────────────────────────────
    rs_20d = context.get("rs_20d")
    if rs_20d is not None:
        rs_60d = context.get("rs_60d")
        sector_rs = context.get("sector_rs")
        sector_etf = context.get("sector_etf", "sector ETF")
        rs_label = "outperforming" if rs_20d > 1.0 else "underperforming"
        rs_line = f"Relative Strength vs S&P 500 (20d): {rs_20d:.2f}x ({rs_label} the market)"
        if rs_60d is not None:
            rs_line += f"; 60d: {rs_60d:.2f}x"
        if sector_rs is not None:
            sector_label = "outperforming" if sector_rs > 1.0 else "underperforming"
            rs_line += f"; vs {sector_etf}: {sector_rs:.2f}x ({sector_label} its sector)"
        parts.append(rs_line + ".")

    # ── Horizon-specific sections ─────────────────────────────────────────────
    if horizon == "near":
        # Volume Profile (near only — irrelevant on far horizon)
        vpoc = context.get("vpoc")
        if vpoc is not None:
            vah = context.get("vah")
            val = context.get("val")
            vp_bias = context.get("vp_bias", "at_vpoc")
            vp_ext = context.get("vp_overextension")
            in_va = context.get("in_value_area", False)
            bias_label = {"above_vpoc": "above fair value", "below_vpoc": "below fair value"}.get(vp_bias, "at fair value")
            vp_line = f"Volume Profile: VPOC=${vpoc:.4g} ({bias_label})"
            if vah and val:
                vp_line += f", Value Area ${val:.4g}–${vah:.4g}"
            if in_va:
                vp_line += ", price inside value area (choppy equilibrium zone)"
            if vp_ext is not None and abs(vp_ext) > 1.3:
                vp_line += f", overextended {abs(vp_ext):.1f}× from VPOC (mean reversion risk)"
            parts.append(vp_line + ".")

            # SL/TP vs Value Area (near only)
            sl = context.get("stop_loss")
            tp = context.get("take_profit")
            if sl and tp and vah and val:
                sl_in_va = float(val) <= float(sl) <= float(vah)
                tp_beyond = float(tp) > float(vah) if direction == "long" else float(tp) < float(val)
                parts.append(f"SL vs Value Area: {'INSIDE (frequently tested)' if sl_in_va else 'outside (protected)'}.")
                parts.append(f"TP vs Value Area: {'beyond boundary (realistic target)' if tp_beyond else 'within VA (low R/R zone)'}.")

        # Macro events in trade window (near only)
        if upcoming:
            ev_strs = [
                f"high-impact macro event in {e['days_away']}d ({e['date']})"
                for e in upcoming[:3]
            ]
            parts.append(f"Macro events in trade window: {' | '.join(ev_strs)}.")

        # Headlines with age (near only — timing matters on short horizon)
        for i, title in enumerate(headlines[:3]):
            age = hl_ages[i] if i < len(hl_ages) else None
            if age is not None:
                parts.append(f"Headline ({age:.0f}h ago): {title}")
            else:
                parts.append(f"Headline: {title}")

    else:  # far horizon
        # DXY trend detail (far only — structural USD context matters on multi-week trades)
        dxy_trend = context.get("dxy_trend")
        dxy_5d = context.get("dxy_change_5d")
        dxy_20d = context.get("dxy_change_20d")
        if dxy_trend:
            dxy_line = f"DXY trend: {dxy_trend}"
            if dxy_5d is not None and dxy_20d is not None:
                dxy_line += f" (5d: {dxy_5d:+.1f}%, 20d: {dxy_20d:+.1f}%)"
            parts.append(dxy_line + ".")

        # Extended sentiment windows (far only — 24h decay avg is near-zero on old articles)
        s5d = context.get("sentiment_5d_avg")
        s10d = context.get("sentiment_10d_avg")
        if s5d is not None:
            parts.append(f"Sentiment 5-day average: {s5d:.3f}.")
        if s10d is not None:
            parts.append(f"Sentiment 10-day average: {s10d:.3f}.")

        # Headlines (no ages on far horizon — recent news less relevant)
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
    model: str, system: str, user: str, api_key: str, *, max_tokens: int = 400,
    thinking_budget: int = 0,
    output_buffer: int = 1024,
) -> str | None:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)

    kwargs: dict = dict(
        model=model,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if thinking_budget > 0:
        # Extended thinking: max_tokens = budget + output buffer.
        # output_buffer controls how many tokens the actual response can use.
        kwargs["max_tokens"] = thinking_budget + output_buffer
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
    else:
        kwargs["max_tokens"] = max_tokens

    msg = await client.messages.create(**kwargs)

    # Content may include thinking blocks — return the first text block
    for block in (msg.content or []):
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            return block.text.strip()
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


async def _capture_metrics_at_entry_async(
    recommendation_id: str,
    active_trade_id: str,
    asset_id: str,
    asset_category: str,
    risk_tier: str,
    direction: str,
    sentiment_avg: float | None,
) -> None:
    """
    Fire-and-forget: capture live TechnicalSnapshot at trade entry and persist
    to llm_rationale_logs.metrics_at_entry + active_trade_id for all matching logs.
    """
    try:
        import asyncio
        from app.core.database import AsyncSessionLocal
        from app.models.llm_rationale_log import LLMRationaleLog
        from app.services.recommendations.engine import get_yf_symbol, compute_sl_tp_for_symbol
        from sqlalchemy import select

        symbol = get_yf_symbol(asset_category, asset_id)
        if not symbol and asset_category.lower() in ("stocks", "etfs", "etf", "stock"):
            symbol = asset_id.upper()

        snap = None
        if symbol:
            result = await asyncio.to_thread(compute_sl_tp_for_symbol, symbol, risk_tier or "moderate", direction)
            if result:
                _, _, _, snap = result

        metrics: dict = {"captured_at": datetime.now(timezone.utc).isoformat()}
        if snap:
            metrics["price"] = snap.price
            if snap.rsi is not None:
                metrics["rsi"] = round(snap.rsi, 1)
            if snap.macd is not None:
                metrics["macd_histogram"] = round(snap.macd[2], 4)
            if snap.atr is not None:
                metrics["atr"] = round(snap.atr, 4)
            if snap.atr_pct is not None:
                metrics["atr_pct"] = round(snap.atr_pct * 100, 2)  # stored as % (e.g. 2.3)
            if snap.adx is not None:
                metrics["adx"] = round(snap.adx[0], 1)
            if snap.vpoc is not None:
                metrics["vpoc"] = snap.vpoc
            if snap.vah is not None:
                metrics["vah"] = snap.vah
            if snap.val is not None:
                metrics["val"] = snap.val
            metrics["vp_bias"] = snap.vp_bias
        if sentiment_avg is not None:
            metrics["sentiment_avg"] = round(sentiment_avg, 4)

        metrics_json = json.dumps(metrics, default=str)

        async with AsyncSessionLocal() as session:
            q = (
                select(LLMRationaleLog)
                .where(
                    LLMRationaleLog.recommendation_id == recommendation_id,
                    LLMRationaleLog.metrics_at_entry.is_(None),
                )
            )
            rows = (await session.execute(q)).scalars().all()
            for row in rows:
                row.active_trade_id = active_trade_id
                row.metrics_at_entry = metrics_json
            await session.commit()
    except Exception as exc:
        _logger.warning("Failed to capture metrics at entry (non-fatal): %s", exc)


async def _log_rationale_async(
    context: dict[str, Any],
    rationale_text: str,
    provider_id: str,
    model: str,
    flags: list[str],
    retry_count: int,
) -> None:
    """
    Fire-and-forget: persist rationale + validation result to llm_rationale_logs.
    After logging, runs P11 news cross-reference in the same task.
    """
    log_id: str | None = None
    try:
        from app.core.database import AsyncSessionLocal
        from app.models.llm_rationale_log import LLMRationaleLog

        expiry_days = context.get("expiry_days") or context.get("horizon_days") or 14
        horizon = "near" if int(expiry_days) <= 14 else "far"

        # Slim context snapshot — exclude bulky / redundant fields.
        snapshot_keys = (
            "asset_id", "asset_category", "direction", "horizon", "expiry_days",
            "sentiment_avg", "sentiment_count", "sentiment_trend",
            "sentiment_5d_avg", "sentiment_10d_avg",
            "buy_in_price", "stop_loss", "take_profit", "risk_tier",
            "confidence_score", "volatility_notes", "market_regime",
            "dxy_trend", "dxy_change_5d", "dxy_change_20d",
            "headlines", "headline_ages",
            "vpoc", "vah", "val", "vp_bias", "vp_overextension", "in_value_area",
        )
        snapshot = {k: context[k] for k in snapshot_keys if k in context}

        entry = LLMRationaleLog(
            created_at=datetime.now(timezone.utc),
            recommendation_id=context.get("recommendation_id"),
            asset_id=str(context.get("asset_id") or ""),
            asset_category=str(context.get("category") or context.get("asset_category") or ""),
            direction=str(context.get("direction") or "long"),
            horizon=horizon,
            horizon_days=int(expiry_days),
            provider_id=provider_id,
            model=model,
            prompt_version=context.get("_prompt_version") or PROMPT_VERSION_RATIONALE,
            context_snapshot=json.dumps(snapshot, default=str),
            rationale_text=rationale_text,
            validation_flags=json.dumps(flags),
            retry_count=retry_count,
        )
        async with AsyncSessionLocal() as session:
            session.add(entry)
            await session.commit()
            await session.refresh(entry)
            log_id = entry.id
    except Exception as exc:
        _logger.warning("Failed to log rationale to DB (non-fatal): %s", exc)

    # P11: news cross-reference — runs in same task after log is committed
    if log_id:
        await _verify_news_for_log(
            log_id=log_id,
            rationale_text=rationale_text,
            headlines=context.get("headlines") or [],
            asset_id=str(context.get("asset_id") or ""),
        )


async def _verify_news_for_log(
    log_id: str,
    rationale_text: str,
    headlines: list[str],
    asset_id: str,
) -> None:
    """P11: run news cross-reference and persist result to llm_rationale_log."""
    try:
        from app.core.database import AsyncSessionLocal
        from app.models.llm_rationale_log import LLMRationaleLog
        from app.services.research.hallucination import verify_news_references

        async with AsyncSessionLocal() as session:
            result = await verify_news_references(
                rationale_text=rationale_text,
                context_headlines=headlines,
                session=session,
                asset_id=asset_id,
            )
            log = await session.get(LLMRationaleLog, log_id)
            if log:
                log.news_verified = result["verified"]
                log.news_verification_detail = json.dumps(result, default=str)
                await session.commit()
    except Exception as exc:
        _logger.warning("Failed to verify news references (non-fatal): %s", exc)


async def generate_rationale(context: dict[str, Any]) -> str | None:
    """
    LLM call with validation + retry loop (max 2 retries on blocking failures).
    Logs every attempt to llm_rationale_logs (fire-and-forget).
    Returns 2-4 sentences on why the trade direction makes sense and the main risk,
    or None if the provider is unavailable or all attempts fail.
    """
    from app.core.config import settings
    from app.core.preferences import get_research_llm_config
    from app.services.research.validator import validate_rationale, MAX_RETRIES

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

    ollama_base_url = (cfg.get("ollama_base_url") or "").strip() or (settings.ollama_base_url or "http://localhost:11434")
    thinking_budget = int(cfg.get("thinking_budget") or 0)

    # Select prompt template and version based on trade horizon
    _horizon = (context.get("horizon") or "near").lower()
    _expiry_days = int(context.get("expiry_days") or 14)
    _direction = (context.get("direction") or "long").lower()
    _calendar_days = round(_expiry_days * 1.4)

    # P14: personalised hallucination guard from bias profile
    from app.services.research.bias_detector import build_hallucination_guard
    _guard = build_hallucination_guard(provider_id, model)

    if _horizon == "far":
        _calendar_weeks = round(_expiry_days / 5, 1)
        _system_tpl = SYSTEM_PROMPT_FAR.format(
            direction=_direction,
            horizon_days=_expiry_days,
            calendar_weeks=_calendar_weeks,
            hallucination_guard=_guard,
        )
        _prompt_version = "rationale_far_v1"
    else:
        _system_tpl = SYSTEM_PROMPT_NEAR.format(
            direction=_direction,
            horizon_days=_expiry_days,
            calendar_days=_calendar_days,
            hallucination_guard=_guard,
        )
        _prompt_version = "rationale_near_v1"

    # Stamp prompt version into context so _log_rationale_async can persist it
    context = dict(context, _prompt_version=_prompt_version)

    # P15: few-shot examples from verified historical rationales
    _few_shot_block = ""
    try:
        from app.core.database import AsyncSessionLocal
        from app.services.research.few_shot import get_verified_few_shot_examples
        async with AsyncSessionLocal() as _fs_session:
            _few_shot_block = await get_verified_few_shot_examples(
                _fs_session, provider_id, model, horizon=_horizon
            )
    except Exception as _fse:
        _logger.debug("[FEW_SHOT] Failed to fetch examples (non-fatal): %s", _fse)

    all_flags: list[str] = []
    last_text: str | None = None

    for attempt in range(MAX_RETRIES + 1):
        system = _system_tpl
        user = _format_user_content(context)
        if _few_shot_block:
            user = _few_shot_block + "\n\n" + user
        corrective = context.get("_corrective_instruction")
        if corrective:
            user = user + f"\n\n[CORRECTION REQUIRED]: {corrective}"

        try:
            if provider_id == "openai":
                text = await _call_openai(model, system, user, api_key or "", max_tokens=400)
            elif provider_id == "anthropic":
                text = await _call_anthropic(
                    model, system, user, api_key or "",
                    max_tokens=400, thinking_budget=thinking_budget, output_buffer=400,
                )
            elif provider_id == "ollama":
                text = await _call_ollama(model, system, user, ollama_base_url)
            elif provider_id == "openrouter":
                text = await _call_openrouter(model, system, user, api_key or "", max_tokens=400)
            else:
                return None
        except Exception as exc:
            _logger.warning("LLM call failed (%s/%s) attempt %d: %s", provider_id, model, attempt, exc)
            break

        if not text:
            break

        last_text = text
        result = validate_rationale(text, context)

        # Accumulate unique flags across attempts.
        for flag in result.flags:
            if flag not in all_flags:
                all_flags.append(flag)

        if result.passed:
            # Log and return immediately.
            asyncio.create_task(
                _log_rationale_async(context, text, provider_id, model, all_flags, attempt)
            )
            return text

        if attempt < MAX_RETRIES and result.retry_message:
            _logger.debug(
                "Rationale validation failed on attempt %d (%s). Retrying. Flags: %s",
                attempt, provider_id, result.flags,
            )
            context = dict(context, _corrective_instruction=result.retry_message)

    # All attempts exhausted — log whatever we have and return it (better than None).
    if last_text:
        asyncio.create_task(
            _log_rationale_async(context, last_text, provider_id, model, all_flags, MAX_RETRIES)
        )
        if all_flags:
            _logger.warning(
                "Rationale returned with unresolved flags after %d retries (%s/%s): %s",
                MAX_RETRIES, provider_id, model, all_flags,
            )
        return last_text

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


_BACKTEST_INSIGHT_SYSTEM = (
    "You are a quantitative trading analyst reviewing short-term backtesting results and making a forward-looking prediction. "
    "Return ONLY valid JSON with this exact structure — no markdown, no extra text:\n"
    "{\n"
    '  "grade": "A"|"B"|"C"|"D"|"F",\n'
    '  "grade_reason": "one short sentence explaining the grade",\n'
    '  "key_insights": ["insight 1", "insight 2", "insight 3"],\n'
    '  "recommendation": "one concrete actionable recommendation for the next {horizon_days} trading days"\n'
    "}\n\n"
    "Grade rubric (apply strictly in order — first matching rule wins):\n"
    "  A = win_rate ≥55% AND avg_pnl >0.3R AND sharpe >1.0 AND max_drawdown <4R\n"
    "  B = (win_rate ≥45% AND avg_pnl >0.2R AND max_drawdown <5R) "
    "OR (win_rate ≥55% but fails exactly one A criterion)\n"
    "  C = win_rate 35-44% OR (win_rate ≥45% AND avg_pnl 0–0.2R) OR max_drawdown 5–8R\n"
    "  D = win_rate 25-34% OR avg_pnl negative but > -0.2R per trade\n"
    "  F = win_rate <25% OR avg_pnl ≤ -0.2R per trade\n"
    "IMPORTANT: a high win rate does NOT override a dangerous drawdown. "
    "A system with max_drawdown >8R must be graded D or F regardless of win rate.\n\n"
    "key_insights: exactly 3 bullet strings:\n"
    "  1. Signal quality in last 90 days (win rate trend, consistency, any regime bias)\n"
    "  2. Risk profile: reference sharpe ratio AND max drawdown R-value explicitly — "
    "flag if drawdown >5R as 'high drawdown risk'\n"
    "  3. Forward outlook: what TODAY's signal + current conditions imply for the next {horizon_days} days — "
    "MUST reference confidence score and sentiment if provided\n"
    "Write for a mixed audience: plain enough for a beginner, precise enough for a pro trader. No disclaimers."
)


def _parse_backtest_insight_json(raw: str | None) -> dict | None:
    """Extract and parse JSON from LLM response, tolerating markdown fences."""
    import json, re
    if not raw:
        return None
    # Strip markdown code fences if present
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()
    # Find first { ... } block
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        # Validate required fields
        if all(k in data for k in ("grade", "grade_reason", "key_insights", "recommendation")):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return None


async def generate_backtest_insight(
    asset_id: str,
    direction: str,
    risk_tier: str,
    metrics: dict,
    direction_stats: dict,
    by_confidence_bucket: dict,
    current_signal: dict | None = None,
    horizon_days: int = 14,
) -> dict | None:
    """
    Post-backtest LLM interpretation with 14-day forward prediction.
    Returns a structured dict: {grade, grade_reason, key_insights[], recommendation}
    or None if LLM unavailable or response cannot be parsed.
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

    dir_s = direction_stats.get(direction, {})
    other_dir = "short" if direction == "long" else "long"
    other_s = direction_stats.get(other_dir, {})

    def _pct(v):
        return f"{round((v or 0) * 100, 1)}%" if v is not None else "N/A"

    bucket_lines = []
    for bucket, bd in (by_confidence_bucket or {}).items():
        wr = bd.get("win_rate")
        n = bd.get("total", 0)
        bucket_lines.append(f"  {bucket}: {_pct(wr)} win rate ({n} trades)")

    # Current signal section
    cs_lines = ""
    if current_signal:
        cs = current_signal
        cs_lines = (
            f"\n--- TODAY's engine signal (current conditions) ---\n"
            f"Direction: {cs.get('direction','?').upper()} | Confidence: {cs.get('confidence','?')} | "
            f"Risk/Reward: {cs.get('risk_reward','N/A')}x\n"
            f"Price: {cs.get('price','?')} | SL: {cs.get('stop_loss','?')} | TP: {cs.get('take_profit','?')}\n"
            f"Trend bias: {cs.get('trend_bias','?')} | Volatility: {cs.get('volatility_regime','?')}\n"
            f"Sentiment avg (recent): {cs.get('sentiment_avg','N/A')} ({cs.get('sentiment_count',0)} articles)\n"
        )

    user_content = (
        f"Asset: {asset_id} | Direction analysed: {direction.upper()} | Risk tier: {risk_tier}\n"
        f"Backtest window: last 90 days | Outcome horizon: {horizon_days} trading days\n"
        f"\n--- {direction.upper()} direction (last 90 days) ---\n"
        f"Signals: {dir_s.get('total', 0)} | Wins: {dir_s.get('wins', 0)} | Win rate: {_pct(dir_s.get('win_rate'))}\n"
        f"\n--- {other_dir.upper()} direction (last 90 days) ---\n"
        f"Signals: {other_s.get('total', 0)} | Wins: {other_s.get('wins', 0)} | Win rate: {_pct(other_s.get('win_rate'))}\n"
        f"\n--- Overall (last 90 days) ---\n"
        f"Win rate: {_pct(metrics.get('win_rate'))} | Avg P&L: {metrics.get('avg_pnl_r', 'N/A')}R\n"
        f"Sharpe: {metrics.get('sharpe_ratio', 'N/A')} | Max drawdown: {metrics.get('max_drawdown_r', 'N/A')}R\n"
        f"Wins: {metrics.get('wins', 0)} | Losses: {metrics.get('losses', 0)} | Expired: {metrics.get('expired', 0)}\n"
        f"\n--- Win rate by confidence bucket ---\n"
        + ("\n".join(bucket_lines) if bucket_lines else "  No bucket data")
        + cs_lines
    )

    raw: str | None = None
    import logging as _log
    _logger = _log.getLogger(__name__)

    thinking_budget = int(cfg.get("thinking_budget") or 0)
    # Backtest insight JSON can be verbose — use 1500 token output buffer.
    _BACKTEST_OUTPUT_BUFFER = 1500

    # Inject horizon_days into the system prompt
    _system = _BACKTEST_INSIGHT_SYSTEM.replace("{horizon_days}", str(horizon_days))

    try:
        if provider_id == "openai":
            raw = await _call_openai(model, _system, user_content, api_key or "", max_tokens=1500)
        elif provider_id == "anthropic":
            raw = await _call_anthropic(
                model, _system, user_content, api_key or "",
                max_tokens=1500, thinking_budget=thinking_budget, output_buffer=_BACKTEST_OUTPUT_BUFFER,
            )
        elif provider_id == "ollama":
            combined = f"{_system.strip()}\n\n---\n\n{user_content.strip()}"
            raw = await _call_ollama(model, "", combined, ollama_base_url, num_predict=600, timeout=120.0)
        elif provider_id == "openrouter":
            raw = await _call_openrouter(model, _system, user_content, api_key or "", max_tokens=1200)
        else:
            return None
    except Exception as exc:
        _logger.warning("Backtest insight LLM call failed (%s/%s): %s", provider_id, model, exc)
        return None

    _logger.debug("Backtest insight raw response (%s): %r", provider_id, (raw or "")[:300])
    parsed = _parse_backtest_insight_json(raw)
    if parsed is None:
        _logger.warning("Backtest insight JSON parse failed for %s. Raw: %r", asset_id, (raw or "")[:200])
    return parsed


# ── Morning Brief Narrator ───────────────────────────────────────────────────

_BRIEF_NARRATIVE_SYSTEM = (
    "You are a senior trading desk morning analyst. "
    "Given today's market data, write a 3-5 sentence morning brief for a systematic trader.\n\n"
    "Structure (flowing prose, no headers, no bullet points, no markdown):\n"
    "1. Macro environment: what VIX level + SPX move signal about today's risk regime (risk-on / risk-off / neutral). "
    "If VIX >30, warn explicitly about tail risk.\n"
    "2. Implications for active positions and new entries: sizing, direction bias, key risk to watch. "
    "If SPX move >1.5% flag as a significant session.\n"
    "3. Today's recommendation context: number of signals, direction skew, whether they align with the macro regime. "
    "Give a clear directional bias for the day (longs preferred / shorts preferred / stay flat).\n\n"
    "Rules: reference specific numbers from the data. No disclaimers. Plain text only."
)


async def generate_brief_narrative(
    checklist_items: list[dict],
    recs_count: int,
    recs_long: int,
    recs_short: int,
    active_trades_count: int,
    market_regime: str | None = None,
    dxy_trend: str | None = None,
    dxy_change_5d: float | None = None,
) -> str | None:
    """
    P18: Generate a 3-5 sentence morning brief narrative from daily briefing data.
    Returns plain text or None if LLM unavailable.
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

    ollama_base_url = (cfg.get("ollama_base_url") or "").strip() or (settings.ollama_base_url or "http://localhost:11434")

    # Build user message from checklist data
    from datetime import date
    checklist_lines = "\n".join(
        f"  {item.get('label', item.get('id', ''))}: {item.get('result', '—')}"
        + (" [NOTABLE]" if item.get("highlight") else "")
        for item in checklist_items
    )
    direction_str = []
    if recs_long:
        direction_str.append(f"{recs_long} long")
    if recs_short:
        direction_str.append(f"{recs_short} short")
    recs_str = f"{recs_count} ({', '.join(direction_str)})" if direction_str else str(recs_count)

    user_lines = [
        f"Date: {date.today().isoformat()}",
        f"\nMarket data:",
        checklist_lines,
        f"\nActive recommendations today: {recs_str}",
        f"Open trades: {active_trades_count}",
    ]
    if market_regime:
        user_lines.append(f"Market regime: {market_regime}")
    if dxy_trend:
        dxy_line = f"DXY: {dxy_trend}"
        if dxy_change_5d is not None:
            dxy_line += f" ({dxy_change_5d:+.1f}% 5d)"
        user_lines.append(dxy_line)

    user = "\n".join(user_lines)

    try:
        if provider_id == "openai":
            return await _call_openai(model, _BRIEF_NARRATIVE_SYSTEM, user, api_key or "", max_tokens=250)
        if provider_id == "anthropic":
            return await _call_anthropic(model, _BRIEF_NARRATIVE_SYSTEM, user, api_key or "", max_tokens=250)
        if provider_id == "ollama":
            combined = f"{_BRIEF_NARRATIVE_SYSTEM.strip()}\n\n---\n\n{user.strip()}"
            return await _call_ollama(model, "", combined, ollama_base_url, num_predict=200, timeout=90.0)
        if provider_id == "openrouter":
            return await _call_openrouter(model, _BRIEF_NARRATIVE_SYSTEM, user, api_key or "", max_tokens=250)
    except Exception as exc:
        _logger.warning("Brief narrative LLM call failed (%s/%s): %s", provider_id, model, exc)
    return None


# ── Gap Risk Advisor ──────────────────────────────────────────────────────────

_GAP_ADVICE_SYSTEM = (
    "You are a risk manager advising a retail trader on an active position that has elevated overnight gap risk. "
    "Your job: analyze the provided trade data and give ONE specific, actionable recommendation in plain English. "
    "Be direct and concrete. Maximum 25 words. No markdown, no bullet points, no disclaimers. "
    "Examples of good output: "
    "'Reduce to half size before Friday close; this short spans 2 weekends with bearish momentum.' "
    "'Consider closing 30% now — high ATR plus earnings in 3 days makes overnight gap destructive.' "
    "'Hold but place a tighter mental stop; gap avg is small relative to your SL buffer.'"
)


async def generate_gap_advice(context: dict[str, Any]) -> str | None:
    """
    Generate a short LLM gap risk recommendation for an active trade.
    Uses extended thinking (Anthropic) or standard call (other providers).
    Returns a single plain-text sentence or None if LLM unavailable.
    """
    import logging as _log
    _logger = _log.getLogger(__name__)

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

    # Build user message from context
    asset      = context.get("asset_id", "Unknown")
    direction  = context.get("direction", "long").upper()
    category   = context.get("category", "stocks")
    gap_level  = context.get("gap_level", "medium").upper()
    gap_factors = context.get("gap_factors", "")
    hold_days  = context.get("hold_days")
    atr_pct    = context.get("atr_pct")
    gap_avg    = context.get("overnight_gap_avg")
    gap_p95    = context.get("overnight_gap_p95")
    sentiment  = context.get("sentiment_score")
    regime     = context.get("market_regime", "unknown")
    near_earn  = context.get("near_earnings", False)
    days_earn  = context.get("days_to_earnings")
    confidence = context.get("confidence_score")
    weekday    = context.get("weekday_name", "")

    parts = [
        f"Asset: {asset} ({category}, {direction}).",
        f"Gap risk level: {gap_level}. Active risk factors: {gap_factors or 'hold period, category'}.",
    ]
    if hold_days is not None:
        parts.append(f"Days until expiry: {hold_days}.")
    if weekday:
        parts.append(f"Today is {weekday}.")
    if atr_pct is not None:
        parts.append(f"ATR (daily %): {atr_pct*100:.2f}%.")
    if gap_avg is not None:
        parts.append(f"Avg overnight gap: {gap_avg*100:.2f}%.")
    if gap_p95 is not None:
        parts.append(f"95th-pct overnight gap: {gap_p95*100:.2f}%.")
    if sentiment is not None:
        sentiment_label = "bullish" if sentiment > 0.05 else "bearish" if sentiment < -0.05 else "neutral"
        parts.append(f"Current sentiment: {sentiment:.2f} ({sentiment_label}).")
    if regime and regime != "unknown":
        parts.append(f"Market regime: {regime}.")
    if confidence is not None:
        parts.append(f"Signal confidence: {confidence}%.")
    if near_earn and days_earn is not None:
        parts.append(f"Earnings report in {days_earn} day(s) — volatility spike risk.")

    user_content = " ".join(parts) + "\n\nWhat should the trader do about gap risk on this position?"

    ollama_base_url = (cfg.get("ollama_base_url") or "").strip() or (settings.ollama_base_url or "http://localhost:11434")

    # Read thinking budget from config — same source as all other prompts.
    # Output for gap advice is short (~25 words), so 150 tokens is a generous buffer.
    thinking_budget = int(cfg.get("thinking_budget") or 0)
    _GAP_OUTPUT_BUFFER = 150

    from app.services.research.validator import validate_rationale, MAX_RETRIES

    corrective: str | None = None
    last_text: str | None = None

    for attempt in range(MAX_RETRIES + 1):
        user_msg = user_content if corrective is None else user_content + f"\n\n[CORRECTION REQUIRED]: {corrective}"

        try:
            if provider_id == "openai":
                text = await _call_openai(model, _GAP_ADVICE_SYSTEM, user_msg, api_key or "", max_tokens=100)
            elif provider_id == "anthropic":
                text = await _call_anthropic(
                    model, _GAP_ADVICE_SYSTEM, user_msg, api_key or "",
                    max_tokens=100, thinking_budget=thinking_budget, output_buffer=_GAP_OUTPUT_BUFFER,
                )
            elif provider_id == "ollama":
                combined = f"{_GAP_ADVICE_SYSTEM.strip()}\n\n---\n\n{user_msg.strip()}"
                text = await _call_ollama(model, "", combined, ollama_base_url, num_predict=60, timeout=60.0)
            elif provider_id == "openrouter":
                text = await _call_openrouter(model, _GAP_ADVICE_SYSTEM, user_msg, api_key or "", max_tokens=100)
            else:
                text = None
        except Exception as exc:
            _logger.warning("Gap advice LLM call failed (%s/%s) attempt %d: %s", provider_id, model, attempt, exc)
            break

        if not text:
            break

        last_text = text
        result = validate_rationale(text, context, prompt_type="gap")

        if result.passed:
            return text

        if attempt < MAX_RETRIES and result.retry_message:
            corrective = result.retry_message
            _logger.debug(
                "Gap advice retry %d/%d for %s — flags: %s",
                attempt + 1, MAX_RETRIES, context.get("asset_id"), result.flags,
            )

    return last_text
