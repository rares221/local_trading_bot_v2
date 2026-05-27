# Signal Quality Improvement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve win rate from 34.7% toward 37%+ by adding an ADX trend-strength hard gate, OBV + sentiment-velocity soft penalties (capped at -15), and re-enabling shorts in bear regime only with a raised confidence floor.

**Architecture:** All changes are confined to `app/services/recommendations/engine.py`. Three additive modifications to `_build_candidate()`: (1) a hard gate after the crisis/high_vol block, (2) a capped soft-penalty block replacing the standalone velocity penalty application, and (3) bear-regime short logic replacing the global SHORTS_ENABLED block. No new files required.

**Tech Stack:** Python, existing `TechnicalSnapshot` (adx, obv fields), existing `MarketContext` (regime), existing `_log_engine_skip` logging helper.

---

## File Map

| File | Change |
|------|--------|
| `app/services/recommendations/engine.py:97` | Add `SHORTS_BEAR_REGIME_ENABLED = True` flag |
| `app/services/recommendations/engine.py:1073–1074` | Add ADX hard gate block |
| `app/services/recommendations/engine.py:1193–1195` | Tighten vel soft-penalty threshold from `< 0` to `< -0.05` |
| `app/services/recommendations/engine.py:1220–1222` | Replace standalone vel penalty application with combined OBV+vel capped block |
| `app/services/recommendations/engine.py:1100–1101` | Replace global short block with bear-regime exception |
| `app/services/recommendations/engine.py` (after final confidence) | Add bear-regime short confidence floor check |

---

## Task 1: Add ADX Hard Gate

**Files:**
- Modify: `app/services/recommendations/engine.py:1073–1074`

- [ ] **Step 1: Locate the insertion point**

Open `engine.py`. Find this block (around line 1073):

```python
    # Hard-block signal generation in crisis and high_vol regimes.
    # Backtest: crisis=12.5% WR (-0.71R avg), high_vol=23.5% WR (-0.009R avg) — both net negative EV.
    # Uses extended_regime (5-label VIX-based) so live labels match backtest definitions.
    if market_ctx and market_ctx.regime.extended_regime in ("crisis", "high_vol"):
        return None
```

- [ ] **Step 2: Insert the ADX gate immediately after the crisis/high_vol block**

Add this block right after the `return None` for crisis/high_vol:

```python
    # ADX trend-strength gate: require ADX ≥ 20 — below this means no clear directional trend.
    # Momentum signals (RSI, MACD, sentiment) are unreliable in trendless/choppy markets.
    # Skip gate if ADX data is unavailable (missing data ≠ no trend — do not penalise data gaps).
    if snap and snap.adx is not None:
        _adx_val = snap.adx[0]
        if _adx_val < 20:
            _log_engine_skip(
                "no_trend_adx",
                category=category,
                asset_id=asset_id,
                symbol=symbol,
                policy=pol.key,
                adx=round(_adx_val, 1),
            )
            return None
```

- [ ] **Step 3: Rebuild and test pipeline manually**

Run:
```bash
make build start
```

Then trigger a recommendation run and check logs:
```bash
docker logs local-trading-bot --tail 100 | grep "no_trend_adx"
```

Expected: some candidates appear with `no_trend_adx` skip reason. If zero appear, ADX data may not be populating — check with:
```bash
docker logs local-trading-bot --tail 200 | grep "ENGINE.*adx"
```

---

## Task 2: Replace Standalone Velocity Penalty with Capped OBV+Velocity Block

**Files:**
- Modify: `app/services/recommendations/engine.py:1193–1222`

This task does two things atomically:
1. Tightens the velocity soft-penalty threshold from `< 0` to `< -0.05` (avoids penalising flat/noise)
2. Replaces the standalone velocity penalty application with a combined OBV+velocity block capped at -15

- [ ] **Step 1: Update the velocity penalty computation threshold**

Find this block (around line 1193):
```python
        elif vel_for_direction < 0:
            # -0.1 vel → -4 pts; -0.149 vel → -5 pts
            vel_confidence_penalty = int(abs(vel_for_direction) * 40)
```

Replace with:
```python
        elif vel_for_direction < -0.05:
            # -0.1 vel → -4 pts; -0.149 vel → -5 pts. Threshold -0.05 avoids penalising flat/noise.
            vel_confidence_penalty = int(abs(vel_for_direction) * 40)
```

- [ ] **Step 2: Replace the standalone velocity penalty application**

Find this block (around line 1220):
```python
    if vel_confidence_penalty > 0:
        confidence = max(0, confidence - vel_confidence_penalty)
        logger.debug("[ENGINE] %s: velocity penalty=%d → conf=%d", asset_id, vel_confidence_penalty, confidence)
```

Replace it entirely with:
```python
    # OBV distribution penalty (longs only): -5 pts when OBV slope is negative.
    # OBV already contributes +10 pts in compute_confidence when confirming direction;
    # explicit -5 for disagreement keeps total OBV swing at 15 pts — proportionate for one indicator.
    _obv_penalty = 0
    if direction == "long" and snap and snap.obv is not None and snap.obv[1] < 0:
        _obv_penalty = 5

    # Combined new-filter penalty cap: OBV + sentiment velocity cannot subtract more than 15 pts.
    # Applied as a single deduction so it cannot stack badly with the existing penalty chain.
    _new_filter_penalty = min(_obv_penalty + vel_confidence_penalty, 15)
    if _new_filter_penalty > 0:
        confidence = max(0, confidence - _new_filter_penalty)
        _nfp_parts = []
        if _obv_penalty > 0:
            _nfp_parts.append(f"obv_distribution=-{_obv_penalty}")
        if vel_confidence_penalty > 0:
            _nfp_parts.append(f"sentiment_fading=-{vel_confidence_penalty}")
        logger.debug(
            "[ENGINE] %s: new_filter_penalty=%d (%s) → conf=%d",
            asset_id, _new_filter_penalty, ", ".join(_nfp_parts), confidence,
        )
```

- [ ] **Step 3: Check logs after rebuild**

```bash
docker logs local-trading-bot --tail 150 | grep "new_filter_penalty"
```

Expected: lines like `new_filter_penalty=5 (obv_distribution=-5)` or `new_filter_penalty=7 (sentiment_fading=-7)` or `new_filter_penalty=12 (obv_distribution=-5, sentiment_fading=-7)`. Never more than 15.

---

## Task 3: Re-enable Shorts in Bear Regime Only

**Files:**
- Modify: `app/services/recommendations/engine.py:97` (flags section)
- Modify: `app/services/recommendations/engine.py:1100–1101` (short block)
- Modify: `app/services/recommendations/engine.py` (after full confidence is computed, before final gate)

- [ ] **Step 1: Add the SHORTS_BEAR_REGIME_ENABLED flag**

Find this section near line 95:
```python
# Short trades disabled: backtest shows 24.7% WR for shorts vs 35.8% for longs — shorts are net -EV.
# Re-enable once short signal quality improves (e.g. dedicated short scoring, bear-regime-only filter).
SHORTS_ENABLED = False
```

Add one line after `SHORTS_ENABLED = False`:
```python
# Bear-regime exception: re-enable shorts when market_regime == "bear".
# Backtest aggregate short WR was 22.8% — but bear-regime-only shorts are expected to perform better.
# Raised confidence floor (50) enforced further below in _build_candidate().
SHORTS_BEAR_REGIME_ENABLED = True
```

- [ ] **Step 2: Replace the global short block with a bear-regime exception**

Find this block (around line 1099):
```python
    # Short trades globally disabled: backtest 24.7% WR vs 35.8% longs — net negative EV.
    if direction == "short" and not SHORTS_ENABLED:
        return None
```

Replace with:
```python
    # Short trades globally disabled except in bear regime (SHORTS_BEAR_REGIME_ENABLED).
    # Bear-regime shorts require: market_regime == "bear", SHORTS_BEAR_REGIME_ENABLED=True,
    # AND confidence ≥ 50 (enforced below after full confidence calculation).
    # SHORTS_ENABLED=True overrides everything (global re-enable for future use).
    if direction == "short" and not SHORTS_ENABLED:
        _bear_regime_short_allowed = (
            SHORTS_BEAR_REGIME_ENABLED
            and market_ctx is not None
            and market_ctx.regime.regime == "bear"
        )
        if not _bear_regime_short_allowed:
            return None
```

- [ ] **Step 3: Add the bear-regime short confidence floor**

Find the `# Risk:Reward ratio` comment (around line 1487):
```python
    # Risk:Reward ratio
    rr = calc_risk_reward(price, sl, tp)
```

Insert this block **immediately before** that comment:

```python
    # Bear-regime short confidence floor: only high-conviction short signals are allowed.
    # Raised to 50 (vs ~35-40 for longs) to compensate for historically lower short WR.
    if direction == "short" and market_ctx and market_ctx.regime.regime == "bear":
        _bear_short_min_conf = 50
        if confidence < _bear_short_min_conf:
            _log_engine_skip(
                "short_low_confidence_bear",
                category=category,
                asset_id=asset_id,
                symbol=symbol,
                policy=pol.key,
                confidence=confidence,
                min_confidence=_bear_short_min_conf,
            )
            return None
```

- [ ] **Step 4: Verify bear-regime short candidates appear in logs**

After rebuild, check that:
```bash
docker logs local-trading-bot --tail 200 | grep -E "short_low_confidence_bear|direction.*short"
```

If market is not in bear regime right now, bear-regime shorts won't fire — that's expected. You can verify the logic is wired correctly by checking that `SHORTS_BEAR_REGIME_ENABLED` is referenced in the skip log and no Python errors appear at startup.

---

## Task 4: Rebuild and Run Backtest

- [ ] **Step 1: Rebuild containers**

```bash
make build start
```

Wait for startup:
```bash
docker logs local-trading-bot --tail 50 | grep -E "started|ready|ERROR"
```

- [ ] **Step 2: Run a full recommendation pipeline pass**

Trigger via the UI or API and watch for the new skip reasons:
```bash
docker logs local-trading-bot --tail 300 | grep -E "no_trend_adx|new_filter_penalty|short_low_confidence_bear"
```

Confirm candidates are not being wiped out — total candidate count should stay above 80% of previous runs.

- [ ] **Step 3: Run backtest**

Run the full backtest from the UI. Record:
- Win rate (target: > 34.7%)
- Sharpe ratio (target: ≥ 2.439)
- Max drawdown (target: ≤ 203.59R)
- Avg P&L (target: ≥ 0.114R)
- Total trades (should not drop below ~3,000)

- [ ] **Step 4: Query bear regime short stats (when bear regime occurs in backtest)**

```bash
podman exec trading-timescaledb psql -U trading -d trading -c "
SELECT direction, market_regime,
       COUNT(*) as trades,
       ROUND(100.0 * SUM(CASE WHEN outcome='hit_tp' THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate_pct,
       ROUND(AVG(pnl_r)::numeric, 3) as avg_pnl_r
FROM backtest_trades
WHERE outcome IS NOT NULL
GROUP BY direction, market_regime
ORDER BY direction, trades DESC;
"
```

Bear-regime short WR target: > 30%.
