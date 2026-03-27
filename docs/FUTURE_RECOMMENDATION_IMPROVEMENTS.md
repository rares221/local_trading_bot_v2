# Future Development Options: More Accurate Trade Recommendations

This document outlines research-backed options to improve the accuracy of the recommendation engine. **No code changes have been made**; it is a roadmap for future work.

---

## 1. Current Model (Brief Summary)

- **Direction:** Sentiment 30% + technicals 70% (RSI, MACD, Bollinger, ADX, Stochastic, OBV); fixed weights.
- **Confidence:** Rule-based 0–100 score from sentiment alignment, mention volume, and technical agreement; no calibration to real outcomes.
- **Regime:** SPX trend + VIX level → risk_on / cautious / risk_off; gates count and direction (e.g. flip longs to shorts in risk_off).
- **Multi-timeframe:** Daily vs weekly trend alignment; EMA stack; used for ranking and bonus/penalty.
- **SL/TP:** ATR-based with S/R awareness; expiry from estimated days to target.
- **Sentiment:** Time-decay weighted VADER per asset tag from RSS; no source weighting or domain-specific NLP.

---

## 2. Technical & Indicator Improvements

| Option | Description | Effort | Impact |
|--------|-------------|--------|--------|
| **Dynamic indicator weights** | Replace fixed 70% technical / 30% sentiment with weights that depend on regime (e.g. more weight to trend in risk_on, more to mean reversion in range). | Medium | High |
| **Regime-specific thresholds** | Use different RSI/MACD/Stochastic thresholds per regime (e.g. stricter overbought in risk_off). | Low | Medium |
| **Volume profile / VWAP** | Add volume-at-price or VWAP context to confirm levels and breakouts. | Medium | Medium |
| **More timeframes** | Extend beyond daily vs weekly (e.g. 4H for swing, 1H for timing); score alignment across 3+ timeframes. | Medium | Medium |
| **Volatility regime** | Separate ATR regime (expanding vs contracting) to adjust SL/TP width and avoid whipsaws. | Low | Medium |
| **Indicator conflict rules** | Explicit rules when key indicators disagree (e.g. RSI overbought but MACD bullish) to reduce confidence or skip. | Low | Medium |

**Implemented (Section 2):** All six items above are in place. Indicator conflicts apply a 0–25 penalty to confidence; candidates with penalty ≥ 20 are skipped (see `CONFLICT_SKIP_THRESHOLD` in the engine).

---

## 3. Sentiment & News Improvements

| Option | Description | Effort | Impact |
|--------|-------------|--------|--------|
| **Domain-specific sentiment** | Use FinBERT/FinGPT or similar instead of (or in addition to) VADER for financial text. | Medium | High |
| **Source and entity weighting** | Weight sentiment by source reliability and by entity (e.g. central banks, earnings) vs generic headlines. | Medium | Medium |
| **Sentiment–price divergence** | Detect when sentiment improves but price falls (or vice versa) as a separate signal or filter. | Low | Medium |
| **Event tagging** | Tag items by event type (earnings, Fed, geopolitics) and adjust recommendation logic per event type. | Medium | Medium |
| **Multi-step sentiment pipeline** | Combine lexicon + classifier + optional LLM summary for important assets. | High | High |

**Implemented (Section 3):** (1) FinBERT optional blend with VADER when `transformers` and `torch` are installed. (2) Source and entity weighting in aggregation (SOURCE_WEIGHTS by URL, ENTITY_WEIGHTS for Fed/ECB etc.). (3) Sentiment–price divergence: recent vs older sentiment and current vs previous close; divergence reduces confidence and is noted. (4) Event tagging (earnings, Fed, geopolitics, macro) with `tag_event_types`; `active_events` in payload and event penalty in engine. (5) Multi-step pipeline deferred.

---

## 4. Regime & Market Context

| Option | Description | Effort | Impact |
|--------|-------------|--------|--------|
| **More regime dimensions** | Add trend vs range, sector rotation, or cross-asset regimes (e.g. risk-on vs risk-off per region). | Medium | High |
| **Sector/asset-specific regimes** | Regime per sector or asset class (e.g. tech vs energy) instead of only SPX/VIX. | Medium | Medium |
| **Correlation-aware filters** | When adding a new recommendation, check correlation with existing ones to avoid over-concentration. | Low | Medium |
| **Macro calendar overlay** | Reduce or gate recommendations around high-impact events (FOMC, CPI, earnings). | Low | Medium |

**Implemented (Section 4):** Correlation-aware filters: when adding a candidate to the daily list, return correlation (60d) with each already-selected recommendation is computed; if any correlation ≥ 0.75, the candidate is skipped. Macro calendar overlay: on FOMC/CPI/NFP days (±1 day), all candidates’ confidence is reduced by 10; see `app/services/macro_calendar.py` and `MACRO_DAY_CONFIDENCE_PENALTY` in the engine.

---

## 5. Confidence & Calibration

| Option | Description | Effort | Impact |
|--------|-------------|--------|--------|
| **Outcome logging** | Persist recommendation outcome (hit TP, hit SL, expiry, or manual close) for each recommendation/trade. | Medium | Foundation |
| **Calibrate confidence** | Use historical outcomes to calibrate the 0–100 score (e.g. Platt scaling, isotonic regression) so “60%” ≈ 60% win rate for that bucket. | Medium | High |
| **Brier score / ECE** | Track Brier score and Expected Calibration Error over time to monitor and improve calibration. | Low | High |
| **Confidence buckets** | Map raw score to buckets (e.g. 0–30, 30–50, 50–70, 70–100) and show empirical hit rate per bucket in UI. | Low | Medium |

| **Manual trade closure** | Allow user to manually close a trade (e.g., took profit early, cut loss). | Low | Low | ✅ **DONE** |

**Implemented (Section 5 — ALL DONE):** (1) Outcome logging: `ActiveTrade.outcome` and `outcome_at` columns; scheduler sets outcomes when trades hit TP/SL or expire. (2) Platt calibration: `app/services/calibration.py` module with `fit_platt_calibration()` and `apply_platt_calibration()`; `POST /api/calibration/fit` endpoint; Dashboard UI with "Fit Platt Calibration" button. (3) Brier score / ECE: `GET /api/calibration` returns both metrics; displayed on Dashboard calibration section. (4) Confidence buckets: `_confidence_bucket()` helper in routes; Dashboard shows hit rate table by bucket (low/medium/high/very_high). (5) Manual close: `POST /api/active-trades/{id}/close-manually` endpoint; Active Trades page "Close manually" button.

---

## 6. Backtesting & Validation

| Option | Description | Effort | Impact |
|--------|-------------|--------|--------|
| **Historical replay** | Re-run the engine on past dates with past analysis snapshots to generate “what would we have recommended?”. | High | High |
| **Out-of-sample testing** | Split history into train/validation/test; tune weights and thresholds on train, validate on validation, report on test. | High | High |
| **Monte Carlo / stress tests** | Randomize entry timing or skip a % of signals to test robustness and overfitting. | Medium | Medium |
| **Benchmarks** | Compare engine performance (win rate, Sharpe, drawdown) vs buy-and-hold or a simple trend filter. | Medium | High |

---

## 7. Machine Learning & Hybrid Models

| Option | Description | Effort | Impact |
|--------|-------------|--------|--------|
| **ML direction classifier** | Train a classifier (e.g. Random Forest, XGBoost) on technicals + sentiment features to predict direction; use as one input to the engine. | High | High |
| **Hybrid rule + ML** | Keep current rules for SL/TP and regime; use ML only for direction or confidence. | High | High |
| **Transformer / LSTM** | Use sequence models on price + sentiment for next-period move; heavy on data and compute. | Very high | High (uncertain) |
| **Ensemble** | Combine rule-based score and ML probability (e.g. average or meta-learner) for final confidence. | Medium | Medium |

---

## 8. Position Sizing & Risk

| Option | Description | Effort | Impact | Status |
|--------|-------------|--------|--------|--------|
| **Kelly or fractional Kelly** | Suggest position size from confidence (as probability) and R:R (e.g. half-Kelly). | Low | Medium | ✅ **DONE** — `suggested_risk_pct` computed via half-Kelly style in `_suggested_risk_pct()`, shown in Recommendations page "Size" column |
| **Risk budget per batch** | Cap total risk (e.g. sum of suggested_risk_pct across recommendations) per batch. | Low | Medium | ✅ **DONE** — `risk_budget_1r` in `build_daily_candidates()` (default **10.0** sum of Size %); Dashboard; see README if batch looks “empty” after two trades |
| **Confidence-based sizing** | Map confidence to suggested size (e.g. 0.5% risk at 40%, 1% at 70%). | Low | Medium | ✅ **DONE** — Implemented as part of half-Kelly calculation with tier-based fallback |

---

## 9. Data & Infrastructure

| Option | Description | Effort | Impact |
|--------|-------------|--------|--------|
| **Higher-frequency price data** | Use 1H or 4H bars where available for better technicals and alignment. | Medium | Medium |
| **Earnings and fundamentals** | Incorporate earnings dates, surprise, or simple fundamental ratios to filter or weight. | High | Medium |
| **Stored feature store** | Precompute and store technical and sentiment features per symbol/date for fast backtests and ML. | High | High |
| **A/B testing framework** | Run two engine variants (e.g. different weights) and compare outcomes over time. | Medium | Medium |

---

## 10. Suggested Priorities (No Order Imposed)

1. **Outcome logging + calibration** — Enables everything else (backtest, ML, sizing).
2. **Regime-specific weights and thresholds** — Low effort, uses existing regime.
3. **Sentiment–price divergence and source weighting** — Better use of existing sentiment.
4. **Backtesting with historical replay** — Validate changes before going live.
5. **Domain sentiment (e.g. FinBERT)** — If more text sources are added.
6. **ML direction or confidence** — Once enough outcome data exists.

---

## What to see in the app / What to test

### Section 2 (Technical & Indicator Improvements)

After running the app and opening the recommendations view:

1. **Dynamic weights & regime thresholds**  
   - Change in market regime (e.g. risk_off vs risk_on) should change which assets pass and how many; in risk_off, equities may flip to short and confidence thresholds are stricter.  
   - Check logs for `Market regime: ...` to see current regime.

2. **VWAP**  
   - In recommendation or technical details (if exposed), price vs VWAP can support long (above) or short (below). Confidence gets a small bonus when price agrees with direction vs VWAP.

3. **More timeframes (4H)**  
   - Trend alignment now includes 4H; “MTF: aligned” in volatility notes means daily, weekly, and (when not neutral) 4H agree. Conflicting timeframes reduce alignment bonus and can lower confidence.

4. **Volatility regime (ATR)**  
   - SL/TP widths are adjusted: **contracting** volatility → slightly wider (1.1× ATR) to avoid whipsaw; **expanding** → slightly tighter (0.95×). **`Vol regime: expanding|contracting|stable`** is included in each recommendation’s stored `volatility_notes` and shown in the Recommendations page **Context** column (hover for full text).

5. **Indicator conflict (reduce confidence + skip)**  
   - **Confidence:** When RSI is overbought but direction is long (or similar conflicts), confidence is reduced by up to 25 points.  
   - **Skip:** If conflict penalty ≥ 20, the candidate is dropped. In logs you should see:  
     `[ENGINE] Skip <asset_id> (<symbol>): indicator conflict penalty=<N> (direction=long|short)`  
   - So: fewer recommendations when several key indicators disagree with the chosen direction; remaining ones have higher conviction.

**Quick checks:**  
- Run the daily recommendation job (scheduler or manual trigger).  
- Check console/logs for regime, skip messages, and final candidate counts per category.  
- Compare recommendation list and confidence scores across different market days (e.g. strong trend vs choppy) to see regime and conflict behaviour.

### Section 5 (Outcome Logging & Calibration) + Section 8 (Position Sizing)

After accumulating some closed trades:

1. **Outcome logging**  
   - Active Trades page shows `outcome` (hit_tp, hit_sl, expired, manual) and `outcome_at` timestamp for closed trades.  
   - Scheduler automatically sets outcomes when trades hit TP/SL or expire.

2. **Calibration UI (Dashboard)**  
   - **Brier Score** and **ECE** metrics displayed (lower is better).  
   - **Hit rate table** by confidence bucket (low/medium/high/very_high) with counts for hit_tp, hit_sl, expired.  
   - **Platt Calibration** status showing fitted A/B parameters.  
   - **"Fit Platt Calibration" button** to train on closed trades (requires ≥10 closed trades).

3. **Position sizing (Recommendations page)**  
   - New **"Size"** column shows `suggested_risk_pct` (e.g., "2.50%") for each recommendation.  
   - Calculated via half-Kelly style from confidence and risk-reward, with tier-based fallback.

4. **Risk budget (Dashboard)**  
   - New input field: "Risk budget per batch (1R = 100%)".  
   - Engine skips candidates if adding them would exceed the budget (sum of `suggested_risk_pct`).  
   - Check logs for: `[ENGINE] Skip <asset>: risk budget X% + Y% > Z% (budget)`.

**Quick checks:**  
- Take some recommendations, let them close (TP/SL/expiry) or manually close them.  
- Visit Dashboard → see calibration stats appear once you have closed trades.  
- Click "Fit Platt Calibration" (after 10+ closed trades) to train the model.  
- Set risk budget to 0.5 (50%) on Dashboard, run a new batch → should see fewer recommendations and logs showing budget constraint.

---

## Next quick wins (~30 minutes each)

**Done:** macro banner + `macro_overlay` on `GET /api/recommendations`; calibration strip on `/recommendations`; survivor **indicator conflict penalty** appended to `volatility_notes`. `MACRO_DAY_CONFIDENCE_PENALTY` now lives in `macro_calendar.py` (imported by the engine).

Further ideas from **Sections 6–9** or polish:

1. ~~**2026+ macro dates**~~ — **Done:** Now **auto-fetched** from public sources (no API key needed). `macro_calendar_fetcher.py` scrapes Fed calendar HTML + press releases JSON for FOMC; CPI/NFP are pattern-based. Cached locally with 7-day TTL; static fallback if fetch fails. **API:** `GET /api/macro-calendar`, `POST /api/macro-calendar/refresh`.
2. **Show calibrated confidence in API** — If Platt is fitted, add optional `confidence_calibrated` to recommendation items.
3. **A/B or shadow mode** — Run two threshold profiles and log which would have fired (Section 9).

**Earlier polish:** Vol regime in notes; **market_regime_note** merged into stored `volatility_notes`; **Context** column on Recommendations.

---

## References (Conceptual)

- Hybrid sentiment + technical models and transformer/LSTM approaches (e.g. Springer 2024, MDPI ML frameworks).
- Position sizing: risk unit per trade, Kelly and fractional Kelly (e.g. BacktestBase, CryptoAdventure).
- Calibration: Brier score, ECE, reliability curves, Platt/isotonic/temperature scaling (e.g. sklearn, Probability Trader Pro, sports-ai.dev).
- Backtesting: randomized and Monte Carlo stress tests to reduce overfitting (e.g. PyQuantLab, BacktestBase).

---

*Document generated for planning only. Implement when you are ready to adopt a given option.*
