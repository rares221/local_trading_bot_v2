"""
Per–asset-class policies for the recommendation engine (Option A).

Scales SPX/VIX-centric regime adjustments, cross-asset divergence haircuts, MTF bonuses,
correlation-divergence penalties, and conflict/R:R floors so commodities, FX, crypto, and
vol indices are not forced through the same constraints as single-name equities.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

# FX majors in category ``other`` (distinct from crypto below).
_OTHER_FX: frozenset[str] = frozenset({"EURUSD", "GBPUSD", "USDJPY"})
_OTHER_CRYPTO: frozenset[str] = frozenset({"BTC", "ETH"})


@dataclass(frozen=True, slots=True)
class AssetClassPolicy:
    """Tuning bundle for one asset class (or subclass like VIX / FX)."""

    key: str

    # Added to regime min-confidence at ranking gate (commodity prefs still add on top).
    min_confidence_offset: int = 0

    # Scale SPX/VIX regime contribution (+5 / -10 / 0).
    equity_regime_adjustment_scale: float = 1.0
    # Scale cross-asset divergence contribution (-5 or 0).
    cross_asset_divergence_scale: float = 1.0

    # Scale integer penalty from _correlation_divergence_penalty before subtracting from confidence.
    correlation_divergence_penalty_scale: float = 1.0

    mtf_aligned_confidence_bonus: int = 8
    mtf_aligned_score_bonus: float = 0.5
    mtf_misalign_score_penalty: float = 0.3

    conflict_skip_threshold: int = 25

    # If True, risk_off forces long → short for equities-like categories (stocks/indices).
    apply_risk_off_long_flip: bool = False

    # Minimum R:R when building a candidate (None = engine global default). Ignored for
    # category ``commodities`` (Dashboard commodity_min_risk_reward wins).
    min_risk_reward: float | None = None

    # When regime is risk_off: subtract from min-confidence gate (looser bar, still bounded 0–100).
    risk_off_gate_relief: int = 0

    # Extra confidence when headlines back the asset (0 min_mentions = disabled).
    news_boost_min_mentions: int = 0
    news_boost_min_abs_sentiment: float = 0.2
    news_boost_points: int = 0

    # Passed to technicals: sentiment / macro weighting and scoring.
    direction_style: str | None = None  # None | "macro_linked" | "index_macro"
    sentiment_signal_scale: float = 1.0
    indicator_conflict_multiplier: float = 1.0


_POLICY_STOCKS = AssetClassPolicy(
    key="stocks",
    apply_risk_off_long_flip=True,
    risk_off_gate_relief=10,
    news_boost_min_mentions=4,
    news_boost_min_abs_sentiment=0.12,
    news_boost_points=10,
    sentiment_signal_scale=1.12,
)

_POLICY_INDICES = AssetClassPolicy(
    key="indices",
    apply_risk_off_long_flip=True,
    risk_off_gate_relief=12,
    news_boost_min_mentions=4,
    news_boost_min_abs_sentiment=0.12,
    news_boost_points=10,
    direction_style="index_macro",
    sentiment_signal_scale=1.18,
    indicator_conflict_multiplier=0.92,
)

# Vol index: do not mirror equity risk-off long flip; looser MTF; more tolerance on conflicts.
_POLICY_VIX = AssetClassPolicy(
    key="indices_vix",
    equity_regime_adjustment_scale=0.55,
    cross_asset_divergence_scale=0.65,
    correlation_divergence_penalty_scale=0.85,
    mtf_aligned_confidence_bonus=4,
    mtf_aligned_score_bonus=0.25,
    mtf_misalign_score_penalty=0.15,
    conflict_skip_threshold=30,
    apply_risk_off_long_flip=False,
    min_risk_reward=1.35,
    risk_off_gate_relief=8,
    sentiment_signal_scale=1.08,
    indicator_conflict_multiplier=0.94,
)

_POLICY_COMMODITIES = AssetClassPolicy(
    key="commodities",
    equity_regime_adjustment_scale=0.45,
    cross_asset_divergence_scale=0.80,
    correlation_divergence_penalty_scale=1.0,
    mtf_aligned_confidence_bonus=4,
    mtf_aligned_score_bonus=0.28,
    mtf_misalign_score_penalty=0.18,
    conflict_skip_threshold=27,
    apply_risk_off_long_flip=False,
    risk_off_gate_relief=12,
    news_boost_min_mentions=3,
    news_boost_min_abs_sentiment=0.10,
    news_boost_points=12,
    direction_style="macro_linked",
    sentiment_signal_scale=1.22,
    indicator_conflict_multiplier=0.88,
)

_POLICY_FX = AssetClassPolicy(
    key="other_fx",
    min_confidence_offset=-2,
    equity_regime_adjustment_scale=0.35,
    cross_asset_divergence_scale=0.55,
    correlation_divergence_penalty_scale=0.65,
    mtf_aligned_confidence_bonus=3,
    mtf_aligned_score_bonus=0.20,
    mtf_misalign_score_penalty=0.12,
    conflict_skip_threshold=28,
    min_risk_reward=1.45,
    apply_risk_off_long_flip=False,
    risk_off_gate_relief=14,
    news_boost_min_mentions=3,
    news_boost_min_abs_sentiment=0.12,
    news_boost_points=10,
    direction_style="macro_linked",
    sentiment_signal_scale=1.25,
    indicator_conflict_multiplier=0.86,
)

_POLICY_CRYPTO = AssetClassPolicy(
    key="other_crypto",
    equity_regime_adjustment_scale=0.55,
    cross_asset_divergence_scale=0.75,
    correlation_divergence_penalty_scale=0.80,
    mtf_aligned_confidence_bonus=5,
    mtf_aligned_score_bonus=0.35,
    mtf_misalign_score_penalty=0.22,
    conflict_skip_threshold=26,
    apply_risk_off_long_flip=False,
    risk_off_gate_relief=10,
    news_boost_min_mentions=4,
    news_boost_min_abs_sentiment=0.14,
    news_boost_points=8,
    direction_style="macro_linked",
    sentiment_signal_scale=1.20,
    indicator_conflict_multiplier=0.90,
)


def engine_policy_to_dict(policy: AssetClassPolicy) -> dict[str, Any]:
    """
    JSON-serializable policy snapshot for APIs (``engine_policy`` on recommendation items).

    Reflects **current** code defaults for this (category, asset_id) class, not values frozen
    at batch time, unless we persist policy on the row later.
    """
    return asdict(policy)


def get_asset_class_policy(category: str, asset_id: str = "") -> AssetClassPolicy:
    """
    Resolve policy for a (category, asset_id) pair.
    ``asset_id`` should match universe spelling (e.g. Brent, VIX, EURUSD).
    """
    cat = (category or "").strip().lower()
    aid = (asset_id or "").strip()
    if cat == "stocks":
        return _POLICY_STOCKS
    if cat == "commodities":
        return _POLICY_COMMODITIES
    if cat == "other":
        au = aid.upper().replace(" ", "")
        if au in _OTHER_CRYPTO:
            return _POLICY_CRYPTO
        return _POLICY_FX
    if cat == "indices":
        if aid.upper() == "VIX":
            return _POLICY_VIX
        return _POLICY_INDICES
    return _POLICY_STOCKS
