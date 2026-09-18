# Confidence model

_Last updated 2026-09-18 (engine-parity plan, Phase 2)._

This app produces one **gating confidence number** per candidate and, separately, an optional
**display-only calibrated probability**. Keeping these distinct is deliberate — earlier the two were
conflated across the batch, live, and calibration code and none agreed. This document is the source of
truth for which number does what.

## 1. The gating number: engine confidence score (`confidence_score`, 0–100)

This is the only number that decides whether a trade is emitted or filtered. It is produced by the
**shared scorer** `app/services/recommendations/scoring.py::score_candidate`, used identically by all
three paths (batch engine, backtest replay, live analyzer) so they cannot drift:

```
base = compute_confidence(sentiment, snapshot, direction, regime, …)   # indicator + sentiment base
      → − compute_soft_penalties(...)        # pooled weak-signal penalties, capped at 25
      → + apply_signal_bonuses(...)          # confluence / momentum / turn-of-month bonuses
      → − STALE_CONFIDENCE_PENALTY (8)       # only if the snapshot is a stale fallback; additive,
                                             #   applied OUTSIDE the pool cap so it always bites
```

Hard gates (RSI-exhaustion ceiling, OFI imbalance, bear-regime short floor) and path-specific context
adjustments (market regime, earnings/event proximity, conviction) are applied by each caller around this
core because their placement differs per path — but the **core number is computed in one place**.

The persisted `Recommendation.confidence_score` is this value. It is what every gate (min-confidence per
category, policy offsets, shadow-B threshold) compares against.

## 2. The UI bucket (`confidence_bucket`)

`routes._confidence_bucket()` maps the **raw `confidence_score`** (the gating number) to
low/medium/high/very_high. It is intentionally derived from the same number the UI shows as primary, so a
badge and its bucket never disagree.

## 3. Display-only overlays (never gate)

Two numbers exist purely for display and are explicitly labeled as such in the UI (tooltips state "does
not affect gating"):

- **`confidence_calibrated`** — Platt scaling (`services/calibration.py`) of the raw score against closed-
  trade outcomes, shown as a smaller `cal N%` subline. It is a probability estimate for the human reader;
  it does **not** re-gate. Rendered in `recommendations.html` (`confCell`, detail card, Tabulator column).
- **`historical_confidence`** (live only) — the old `compute_live_confidence` backtest-win-rate blend.
  As of Phase 1 it no longer gates; it is emitted by `live_analyzer` as a labeled overlay
  ("historical hit-rate adjustment"). Kept because the per-symbol historical hit rate is useful context
  next to the live signal; **remove it** if that overlay ever stops earning its space in the UI.

## 4. Rule of thumb

- Anything that **decides** (emit/skip, ranking, thresholds) → raw `confidence_score` only.
- Anything **calibrated** or **historical** → display only, labeled, never fed back into a gate.
- The bucket always follows the displayed primary number (the raw score).

## 5. Where it lives

| Concern | Location |
| --- | --- |
| Shared core scorer | `app/services/recommendations/scoring.py::score_candidate` |
| Stale discount constant | `scoring.STALE_CONFIDENCE_PENALTY` |
| Batch use | `engine.py` (`score_candidate` at ~1336) |
| Live use | `live_analyzer.py` (`score_candidate` at ~423; `historical_confidence` overlay) |
| Replay use | `replay.py` (manual `compute_confidence → soft_penalties → bonuses`, ~261–290) |
| Bucket | `routes._confidence_bucket` |
| Calibration (display) | `services/calibration.py`; applied in `routes._rec_to_item` |
