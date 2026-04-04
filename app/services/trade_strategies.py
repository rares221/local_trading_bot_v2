"""
Named execution strategies when taking a recommendation (trade builder → active trades).

IDs map to concrete multi-leg TPs derived from system entry / stop / target. Patterns follow
widely taught scale-out ideas: R-multiple partials, midpoint splits, Fibonacci-style staging
along the move to target, and equal ladders.
"""
from __future__ import annotations

from typing import Any

STRATEGY_VANILLA = "vanilla"
STRATEGY_SPLIT_TP_50_50 = "split_tp_50_50"
STRATEGY_RUNNER_50_50_ONE_R = "runner_50_50_one_r"
STRATEGY_SPLIT_25_75_MID = "split_25_75_midpoint"
STRATEGY_THIRDS_EQUAL = "thirds_equal"
STRATEGY_SCALE_HALF_1R_HALF_2R = "scale_half_1r_half_2r"
STRATEGY_THIRDS_1R_2R_FULL = "equal_thirds_1r_2r_full"
STRATEGY_FIB_THIRDS = "fibonacci_thirds_382_618_full"

VALID_STRATEGIES = frozenset({
    STRATEGY_VANILLA,
    STRATEGY_SPLIT_TP_50_50,
    STRATEGY_RUNNER_50_50_ONE_R,
    STRATEGY_SPLIT_25_75_MID,
    STRATEGY_THIRDS_EQUAL,
    STRATEGY_SCALE_HALF_1R_HALF_2R,
    STRATEGY_THIRDS_1R_2R_FULL,
    STRATEGY_FIB_THIRDS,
})

STRATEGY_LEG_VANILLA = "vanilla"
STRATEGY_LEG_SPLIT_A = "split_tp_a"
STRATEGY_LEG_SPLIT_B = "split_tp_b"
STRATEGY_LEG_RUNNER_A = "runner_50_a"
STRATEGY_LEG_RUNNER_B = "runner_50_b"
STRATEGY_LEG_SPLIT25_A = "split25_a"
STRATEGY_LEG_SPLIT25_B = "split25_b"
STRATEGY_LEG_THIRDS_A = "thirds_a"
STRATEGY_LEG_THIRDS_B = "thirds_b"
STRATEGY_LEG_THIRDS_C = "thirds_c"
STRATEGY_LEG_HALF12_A = "half12_a"
STRATEGY_LEG_HALF12_B = "half12_b"
STRATEGY_LEG_FIB_A = "fib_a"
STRATEGY_LEG_FIB_B = "fib_b"
STRATEGY_LEG_FIB_C = "fib_c"
STRATEGY_LEG_RSTEP_A = "rstep_a"
STRATEGY_LEG_RSTEP_B = "rstep_b"
STRATEGY_LEG_RSTEP_C = "rstep_c"


def _merge_price_fractions(
    entry: float,
    take_profit: float,
    priced_fracs: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Sum fractions that share the same TP (2 dp); order from entry toward take_profit."""
    bucket: dict[float, float] = {}
    for price, frac in priced_fracs:
        k = round(float(price), 2)
        bucket[k] = bucket.get(k, 0.0) + float(frac)
    ascending = take_profit >= entry
    keys = sorted(bucket.keys(), reverse=not ascending)
    return [(k, bucket[k]) for k in keys]


def leg_count_for_strategy(
    strategy: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    direction: str,
) -> int:
    try:
        s = normalize_strategy(strategy)
    except ValueError:
        return 1
    return len(legs_for_strategy(s, entry, stop_loss, take_profit, direction))


def strategy_catalog() -> list[dict[str, str]]:
    """UI / API: id + short label for dropdowns (established execution patterns)."""
    return [
        {
            "id": STRATEGY_VANILLA,
            "label": "All-in — single position to full target (simple TP/SL)",
        },
        {
            "id": STRATEGY_RUNNER_50_50_ONE_R,
            "label": "Classic +1R scale-out — 50% off at +1R, 50% to full target",
        },
        {
            "id": STRATEGY_SCALE_HALF_1R_HALF_2R,
            "label": "R-multiple split — 50% at +1R, 50% at +2R (capped at system TP)",
        },
        {
            "id": STRATEGY_SPLIT_TP_50_50,
            "label": "Midpoint scale-out — 50% at half the move, 50% at full target",
        },
        {
            "id": STRATEGY_SPLIT_25_75_MID,
            "label": "Light partial — 25% at midpoint, 75% runner to full target",
        },
        {
            "id": STRATEGY_THIRDS_EQUAL,
            "label": "Equal ladder — thirds at ⅓, ⅔, and full target along the move",
        },
        {
            "id": STRATEGY_THIRDS_1R_2R_FULL,
            "label": "R-ladder thirds — equal exits at +1R, +2R, and full target (merged if same level)",
        },
        {
            "id": STRATEGY_FIB_THIRDS,
            "label": "Fibonacci-style thirds — 38.2%, 61.8%, and full target along the move",
        },
    ]


def midpoint_price(entry: float, tp: float) -> float:
    return round(entry + 0.5 * (tp - entry), 2)


def target_at_nr_clamped(
    entry: float, stop_loss: float, take_profit: float, n: float
) -> float:
    """Price n×R from entry toward take_profit, clamped so it does not overshoot TP."""
    risk = abs(entry - stop_loss)
    if risk <= 0:
        return round(entry, 2)
    if take_profit > entry:
        return round(min(entry + n * risk, take_profit), 2)
    if take_profit < entry:
        return round(max(entry - n * risk, take_profit), 2)
    return round(entry, 2)


def first_target_one_r(entry: float, stop_loss: float, take_profit: float) -> float:
    return target_at_nr_clamped(entry, stop_loss, take_profit, 1.0)


def third_waypoints(entry: float, take_profit: float) -> tuple[float, float, float]:
    d = take_profit - entry
    t1 = round(entry + d / 3.0, 2)
    t2 = round(entry + 2.0 * d / 3.0, 2)
    return t1, t2, round(take_profit, 2)


def fibonacci_waypoints(entry: float, take_profit: float) -> tuple[float, float, float]:
    """Along entry→target: common 38.2% / 61.8% / 100% staging (Fibonacci extension style)."""
    d = take_profit - entry
    t1 = round(entry + 0.382 * d, 2)
    t2 = round(entry + 0.618 * d, 2)
    return t1, t2, round(take_profit, 2)


def strategy_static_explanation(
    strategy: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    direction: str,
) -> str:
    """Instant summary (no LLM)."""
    r_dist = abs(entry - stop_loss)
    r_full = abs(take_profit - entry) / r_dist if r_dist > 0 else 0
    d = (direction or "long").lower()
    if strategy == STRATEGY_VANILLA:
        return (
            f"One full-size position: take-profit {take_profit:.4g}, stop {stop_loss:.4g}. "
            f"If the move completes, about {r_full:.2f}R before costs; if the stop hits first, about −1R. "
            "Simplest to manage."
        )
    if strategy == STRATEGY_SPLIT_TP_50_50:
        mid = midpoint_price(entry, take_profit)
        return (
            f"Two equal legs, same stop {stop_loss:.4g}. First exits at the midpoint {mid:.4g}; "
            f"second at full TP {take_profit:.4g}. Locks in partial gain if price stalls halfway; "
            f"still keeps half for the full {d} target. Stopped before any fill ≈ −1R combined."
        )
    if strategy == STRATEGY_RUNNER_50_50_ONE_R:
        t1 = first_target_one_r(entry, stop_loss, take_profit)
        return (
            f"Classic risk-based scale-out: two equal legs, same stop. First target +1R at {t1:.4g} "
            f"(clamped to system TP); second at {take_profit:.4g}. Banks structured profit early while "
            "keeping half for extension — a standard playbook in R-multiple trading."
        )
    if strategy == STRATEGY_SCALE_HALF_1R_HALF_2R:
        t1 = first_target_one_r(entry, stop_loss, take_profit)
        t2 = target_at_nr_clamped(entry, stop_loss, take_profit, 2.0)
        return (
            f"Two equal legs: first at +1R {t1:.4g}, second at +2R toward the move, capped at {t2:.4g} "
            f"(system TP). Common staged exit at round risk multiples; if your target is inside 2R, "
            "the second leg sits on the same final target."
        )
    if strategy == STRATEGY_SPLIT_25_75_MID:
        mid = midpoint_price(entry, take_profit)
        return (
            f"Smaller early take at midpoint {mid:.4g} (25% of risk) and main runner (75%) to {take_profit:.4g}. "
            "Same stop for both. Light de-risk at half distance; most size still aims for the system target."
        )
    if strategy == STRATEGY_THIRDS_EQUAL:
        a, b, c = third_waypoints(entry, take_profit)
        return (
            f"Three equal legs along the path to TP: first {a:.4g}, then {b:.4g}, then {c:.4g}. "
            "Same stop. Lets you scale out in steps; more management, smoother P&L path if the trend grinds."
        )
    if strategy == STRATEGY_THIRDS_1R_2R_FULL:
        t1 = first_target_one_r(entry, stop_loss, take_profit)
        t2 = target_at_nr_clamped(entry, stop_loss, take_profit, 2.0)
        merged = _merge_price_fractions(
            entry,
            take_profit,
            [
                (t1, 1.0 / 3.0),
                (t2, 1.0 / 3.0),
                (round(take_profit, 2), 1.0 / 3.0),
            ],
        )
        parts = [f"{p:.4g} ({w * 100:.0f}%)" for p, w in merged]
        return (
            "Equal-weight scale-out at +1R, +2R, and full target (thirds), with same stop. "
            f"After merging identical prices: {'; '.join(parts)}. "
            "Widely used swing-style ladder when you want equal slices at round R-levels."
        )
    if strategy == STRATEGY_FIB_THIRDS:
        a, b, c = fibonacci_waypoints(entry, take_profit)
        return (
            f"Three equal legs at Fibonacci-style distances along entry→target: {a:.4g} (~38.2%), "
            f"{b:.4g} (~61.8%), {c:.4g} (full). Same stop. Common in Fibonacci-taught partial-profit plans; "
            "not a prediction, just staged exits on your system’s levels."
        )
    return "Select a strategy to see details."


def build_strategy_explanation_user_message(
    strategy: str,
    asset_id: str,
    asset_category: str,
    direction: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    risk_reward: float | None,
    volatility_notes: str | None,
) -> str:
    """Facts for the LLM user message."""
    lines = [
        f"Asset: {asset_id} ({asset_category}). Direction: {direction}.",
        f"Planned entry: {entry}, stop loss: {stop_loss}, take profit: {take_profit}.",
    ]
    if risk_reward is not None:
        lines.append(f"System risk:reward to full TP: about {risk_reward:.2f}:1.")
    r_dist = abs(entry - stop_loss)
    if r_dist > 0:
        lines.append(f"1R (risk per unit to stop): {r_dist:.6g} (price distance).")

    if strategy == STRATEGY_VANILLA:
        lines.append("Chosen execution: ALL-IN — one position, full size, single TP or stop.")
    elif strategy == STRATEGY_SPLIT_TP_50_50:
        mid = midpoint_price(entry, take_profit)
        lines.append(
            "Chosen execution: MIDPOINT 50/50 — leg1 50% at midpoint TP "
            f"{mid}, leg2 50% at full TP {take_profit}."
        )
    elif strategy == STRATEGY_RUNNER_50_50_ONE_R:
        t1 = first_target_one_r(entry, stop_loss, take_profit)
        lines.append(
            "Chosen execution: CLASSIC +1R SCALE-OUT — leg1 50% at +1R "
            f"{t1}, leg2 50% at full TP {take_profit}."
        )
    elif strategy == STRATEGY_SCALE_HALF_1R_HALF_2R:
        t1 = first_target_one_r(entry, stop_loss, take_profit)
        t2 = target_at_nr_clamped(entry, stop_loss, take_profit, 2.0)
        lines.append(
            "Chosen execution: 50% AT +1R, 50% AT +2R (capped at TP) — "
            f"leg1 at {t1}, leg2 at {t2}."
        )
    elif strategy == STRATEGY_SPLIT_25_75_MID:
        mid = midpoint_price(entry, take_profit)
        lines.append(
            "Chosen execution: 25/75 MID — leg1 25% at midpoint "
            f"{mid}, leg2 75% at full TP {take_profit}."
        )
    elif strategy == STRATEGY_THIRDS_EQUAL:
        a, b, c = third_waypoints(entry, take_profit)
        lines.append(
            "Chosen execution: EQUAL THIRDS ALONG PATH — three legs ~33% each at "
            f"{a}, {b}, {c}."
        )
    elif strategy == STRATEGY_THIRDS_1R_2R_FULL:
        t1 = first_target_one_r(entry, stop_loss, take_profit)
        t2 = target_at_nr_clamped(entry, stop_loss, take_profit, 2.0)
        lines.append(
            "Chosen execution: R-LADDER THIRDS — ~33% each at +1R, +2R, full TP "
            f"({t1}, {t2}, {take_profit}); identical prices may be merged into fewer legs."
        )
    elif strategy == STRATEGY_FIB_THIRDS:
        a, b, c = fibonacci_waypoints(entry, take_profit)
        lines.append(
            "Chosen execution: FIB-STYLE THIRDS (~38.2% / ~61.8% / full along move) at "
            f"{a}, {b}, {c}."
        )

    if volatility_notes:
        notes = volatility_notes.strip()
        if len(notes) > 1200:
            notes = notes[:1200] + "…"
        lines.append(f"System technical / context notes: {notes}")
    lines.append(
        "Explain this execution plan briefly but clearly (plain language). "
        "Cover exits, sizing across legs, pros/cons, and combined loss if stopped before TPs. "
        "Stay under ~160 words. Do not contradict direction. Skip long disclaimers."
    )
    return "\n".join(lines)


def normalize_strategy(value: str | None) -> str:
    s = (value or STRATEGY_VANILLA).strip().lower()
    if s not in VALID_STRATEGIES:
        raise ValueError(f"strategy must be one of: {sorted(VALID_STRATEGIES)}")
    return s


def strategy_label_for_leg(strategy_leg: str) -> str:
    labels = {
        STRATEGY_LEG_VANILLA: "Vanilla",
        STRATEGY_LEG_SPLIT_A: "Midpoint — 1st (50%)",
        STRATEGY_LEG_SPLIT_B: "Midpoint — full TP (50%)",
        STRATEGY_LEG_RUNNER_A: "+1R scale — 1st (50%)",
        STRATEGY_LEG_RUNNER_B: "+1R scale — full TP (50%)",
        STRATEGY_LEG_SPLIT25_A: "25% — midpoint",
        STRATEGY_LEG_SPLIT25_B: "75% — full TP",
        STRATEGY_LEG_THIRDS_A: "Ladder — 1/3 move",
        STRATEGY_LEG_THIRDS_B: "Ladder — 2/3 move",
        STRATEGY_LEG_THIRDS_C: "Ladder — full TP",
        STRATEGY_LEG_HALF12_A: "+1R / +2R — 1st (50%)",
        STRATEGY_LEG_HALF12_B: "+1R / +2R — 2nd (50%)",
        STRATEGY_LEG_FIB_A: "Fib — ~38.2%",
        STRATEGY_LEG_FIB_B: "Fib — ~61.8%",
        STRATEGY_LEG_FIB_C: "Fib — full TP",
        STRATEGY_LEG_RSTEP_A: "R-ladder — leg 1",
        STRATEGY_LEG_RSTEP_B: "R-ladder — leg 2",
        STRATEGY_LEG_RSTEP_C: "R-ladder — leg 3",
    }
    return labels.get(strategy_leg, strategy_leg)


def _legs_rthird_merged(
    entry: float,
    stop_loss: float,
    take_profit: float,
) -> list[dict[str, Any]]:
    third = 1.0 / 3.0
    t1 = first_target_one_r(entry, stop_loss, take_profit)
    t2 = target_at_nr_clamped(entry, stop_loss, take_profit, 2.0)
    t3 = round(take_profit, 2)
    merged = _merge_price_fractions(
        entry,
        take_profit,
        [(t1, third), (t2, third), (t3, third)],
    )
    keys = [STRATEGY_LEG_RSTEP_A, STRATEGY_LEG_RSTEP_B, STRATEGY_LEG_RSTEP_C]
    return [
        {"strategy_leg": keys[i], "take_profit": p, "position_fraction": w}
        for i, (p, w) in enumerate(merged)
    ]


def _legs_fib_merged(entry: float, take_profit: float) -> list[dict[str, Any]]:
    a, b, c = fibonacci_waypoints(entry, take_profit)
    third = 1.0 / 3.0
    merged = _merge_price_fractions(
        entry,
        take_profit,
        [(a, third), (b, third), (c, third)],
    )
    keys = [STRATEGY_LEG_FIB_A, STRATEGY_LEG_FIB_B, STRATEGY_LEG_FIB_C]
    return [
        {"strategy_leg": keys[i], "take_profit": p, "position_fraction": w}
        for i, (p, w) in enumerate(merged)
    ]


def legs_for_strategy(
    strategy: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    direction: str,
) -> list[dict[str, Any]]:
    """
    Each leg: strategy_leg, take_profit (this leg's TP), position_fraction (0–1], same SL as recommendation.
    """
    _ = direction
    if strategy == STRATEGY_VANILLA:
        return [
            {
                "strategy_leg": STRATEGY_LEG_VANILLA,
                "take_profit": take_profit,
                "position_fraction": 1.0,
            }
        ]
    if strategy == STRATEGY_SPLIT_TP_50_50:
        tp1 = midpoint_price(entry, take_profit)
        return [
            {"strategy_leg": STRATEGY_LEG_SPLIT_A, "take_profit": tp1, "position_fraction": 0.5},
            {"strategy_leg": STRATEGY_LEG_SPLIT_B, "take_profit": take_profit, "position_fraction": 0.5},
        ]
    if strategy == STRATEGY_RUNNER_50_50_ONE_R:
        tp1 = first_target_one_r(entry, stop_loss, take_profit)
        return [
            {"strategy_leg": STRATEGY_LEG_RUNNER_A, "take_profit": tp1, "position_fraction": 0.5},
            {"strategy_leg": STRATEGY_LEG_RUNNER_B, "take_profit": take_profit, "position_fraction": 0.5},
        ]
    if strategy == STRATEGY_SCALE_HALF_1R_HALF_2R:
        t1 = first_target_one_r(entry, stop_loss, take_profit)
        t2 = target_at_nr_clamped(entry, stop_loss, take_profit, 2.0)
        merged = _merge_price_fractions(entry, take_profit, [(t1, 0.5), (t2, 0.5)])
        if len(merged) == 1:
            p, w = merged[0]
            return [{"strategy_leg": STRATEGY_LEG_VANILLA, "take_profit": p, "position_fraction": w}]
        return [
            {"strategy_leg": STRATEGY_LEG_HALF12_A, "take_profit": merged[0][0], "position_fraction": merged[0][1]},
            {"strategy_leg": STRATEGY_LEG_HALF12_B, "take_profit": merged[1][0], "position_fraction": merged[1][1]},
        ]
    if strategy == STRATEGY_SPLIT_25_75_MID:
        tp1 = midpoint_price(entry, take_profit)
        return [
            {"strategy_leg": STRATEGY_LEG_SPLIT25_A, "take_profit": tp1, "position_fraction": 0.25},
            {"strategy_leg": STRATEGY_LEG_SPLIT25_B, "take_profit": take_profit, "position_fraction": 0.75},
        ]
    if strategy == STRATEGY_THIRDS_EQUAL:
        t1, t2, t3 = third_waypoints(entry, take_profit)
        third = 1.0 / 3.0
        return [
            {"strategy_leg": STRATEGY_LEG_THIRDS_A, "take_profit": t1, "position_fraction": third},
            {"strategy_leg": STRATEGY_LEG_THIRDS_B, "take_profit": t2, "position_fraction": third},
            {"strategy_leg": STRATEGY_LEG_THIRDS_C, "take_profit": t3, "position_fraction": third},
        ]
    if strategy == STRATEGY_THIRDS_1R_2R_FULL:
        legs = _legs_rthird_merged(entry, stop_loss, take_profit)
        if len(legs) == 1:
            legs[0]["strategy_leg"] = STRATEGY_LEG_VANILLA
        return legs
    if strategy == STRATEGY_FIB_THIRDS:
        legs = _legs_fib_merged(entry, take_profit)
        if len(legs) == 1:
            legs[0]["strategy_leg"] = STRATEGY_LEG_VANILLA
        return legs
    raise ValueError(f"unknown strategy: {strategy}")
