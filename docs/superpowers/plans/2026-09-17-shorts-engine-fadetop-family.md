# Shorts Engine — Fade-Top Family (Phase 5b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline) — TDD, checkbox steps.
> **PROJECT COMMIT RULE (CLAUDE.md):** Do NOT `git commit` — the user commits manually. "Commit" steps are checkpoints.

**Goal:** Add the fade-top family (short exhausted strength at highs) to the shorts engine, completing the two-family design (5a breakdown + 5b fade-top). The engine is "done" only when both ship.

**Architecture:** Extend `app/services/recommendations/shorts_signals.py`. Fade-tops use a **bespoke top-quality scorer** (the shared `score_candidate` is trend-following and penalizes high-RSI shorts), a **family-aware EMA200 gate** (breakdowns require price BELOW EMA200; fade-tops require ABOVE — a top is above its 200-EMA), and **Option-1 activation** (`_tape_deteriorating`: VIX rising + SPX below its 20-day MA), distinct from breakdowns' `bear`-regime activation.

**Tech Stack:** Python 3.11, pandas, pytest. Reuses `TechnicalSnapshot`, `compute_atr_levels`, `calc_risk_reward`.

---

## File Structure
- **Modify** `app/services/recommendations/shorts_signals.py` — add `_tape_deteriorating`, `exhaustion`, `distribution`, `top_quality_score`; make `detect_short_triggers` family-aware; make `generate_short_signal` route by family.
- **Modify** `tests/unit/services/test_shorts_signals.py` — add fade-top unit tests.
- **Modify** `scripts/validate_shorts_breakdown.py` — activate fade-tops on deteriorating-tape days; report a combined + per-family cohort.

Container tests: `docker-compose exec -T app python -m pytest tests/unit/services/test_shorts_signals.py -v`

---

## Task 1: Constants — fade-top offsets + family map

**Modify** `shorts_signals.py`.

- [ ] Extend `TRIGGER_BASE_OFFSET` with `"distribution": 8, "exhaustion": 5`.
- [ ] Add:
```python
_BREAKDOWN_TRIGGERS = ("support_breakdown", "failed_breakout", "failed_retest")
_FADE_TOP_TRIGGERS = ("distribution", "exhaustion")
```
- [ ] Checkpoint.

---

## Task 2: `_tape_deteriorating` activation predicate

**Files:** modify module + test.

- [ ] **Test (RED):**
```python
def test_tape_deteriorating_true_when_vix_rising_and_spx_below_20ma():
    vix = pd.Series([15.0] * 20 + [18.0])   # rising vs 5d ago
    spx = pd.Series([100.0] * 20 + [95.0])  # last close below 20d MA (~100)
    assert ss._tape_deteriorating(vix, spx) is True

def test_tape_deteriorating_false_in_calm_uptrend():
    vix = pd.Series([15.0] * 21)            # flat
    spx = pd.Series(list(range(100, 121)))  # rising, last above 20d MA
    assert ss._tape_deteriorating(vix, spx) is False
```
- [ ] **Impl:**
```python
def _tape_deteriorating(vix: pd.Series, spx: pd.Series, vix_lookback: int = 5, spx_ma: int = 20) -> bool:
    """Option-1 fade-top activation: VIX rising over vix_lookback days AND SPX below its spx_ma MA."""
    if vix is None or spx is None or len(vix) <= vix_lookback or len(spx) < spx_ma:
        return False
    vix_rising = float(vix.iloc[-1]) > float(vix.iloc[-1 - vix_lookback])
    spx_below = float(spx.iloc[-1]) < float(spx.iloc[-spx_ma:].mean())
    return vix_rising and spx_below
```
- [ ] Run RED→GREEN. Checkpoint.

---

## Task 3: `exhaustion` + `distribution` triggers

**Files:** modify module + test.

- [ ] **Tests (RED):**
```python
def test_exhaustion_fires_on_overbought_rolling_over_with_fading_volume():
    snap = _snap(rsi=78.0, macd_hist_slope=-0.4, volume_ratio=0.8)
    bars = _bars([100.0]*22 + [110, 111, 111], vols=[3e6]*20 + [3e6, 2e6, 1.5e6])
    assert ss.exhaustion(snap, bars) is True

def test_exhaustion_no_fire_when_rsi_not_extreme():
    snap = _snap(rsi=62.0, macd_hist_slope=-0.4, volume_ratio=0.8)
    bars = _bars([100.0]*25)
    assert ss.exhaustion(snap, bars) is False

def test_distribution_fires_near_high_with_obv_rolling_over():
    snap = _snap(obv=(1_000_000.0, -5.0))  # obv slope negative
    closes = [100.0]*20 + [118, 119, 120, 120, 119]  # near 20-bar high
    bars = _bars(closes)
    assert ss.distribution(snap, bars) is True

def test_distribution_no_fire_when_obv_confirms():
    snap = _snap(obv=(1_000_000.0, 5.0))   # obv slope positive
    closes = [100.0]*20 + [118, 119, 120, 120, 119]
    bars = _bars(closes)
    assert ss.distribution(snap, bars) is False
```
- [ ] **Impl:**
```python
_NEAR_HIGH_FRAC = 0.98  # within 2% of the 20-bar high counts as "near the high"

def exhaustion(snap: TechnicalSnapshot, bars: pd.DataFrame) -> bool:
    """RSI overbought + MACD histogram rolling over + volume fading into the last ~3 bars."""
    if snap.rsi is None or snap.rsi <= 70:
        return False
    if snap.macd_hist_slope is None or snap.macd_hist_slope >= 0:
        return False
    if bars is None or len(bars) < 4 or "Volume" not in bars.columns:
        return False
    v = bars["Volume"].iloc[-3:]
    volume_fading = bool(v.iloc[-1] < v.iloc[0])  # declining across the last 3 bars
    return volume_fading

def distribution(snap: TechnicalSnapshot, bars: pd.DataFrame) -> bool:
    """Price holding near its 20-bar high while OBV slope has rolled negative (lower highs)."""
    if snap.obv is None or snap.obv[1] >= 0:
        return False
    if bars is None or len(bars) < _SWING_WINDOW:
        return False
    high_20 = float(bars["High"].iloc[-_SWING_WINDOW:].max())
    close = float(bars["Close"].iloc[-1])
    return close >= _NEAR_HIGH_FRAC * high_20
```
- [ ] Run RED→GREEN. Checkpoint.

---

## Task 4: `top_quality_score`

**Files:** modify module + test.

- [ ] **Tests (RED):**
```python
def test_top_quality_score_textbook_top_scores_high():
    snap = _snap(rsi=85.0, macd_hist_slope=-0.5, obv=(1e6, -5.0),
                 volume_ratio=0.7, stochastic=(85.0, 80.0))
    assert ss.top_quality_score(snap) >= 90

def test_top_quality_score_marginal_top_scores_low():
    snap = _snap(rsi=71.0, macd_hist_slope=0.1, obv=(1e6, 1.0),
                 volume_ratio=1.1, stochastic=(60.0, 55.0))
    assert ss.top_quality_score(snap) < 40
```
- [ ] **Impl:**
```python
def top_quality_score(snap: TechnicalSnapshot) -> int:
    """Bespoke fade-top quality (0-100): rewards extremity of the top, not bearish momentum.
    OBV/MACD kept binary (slope sign) — their magnitudes aren't normalized across symbols."""
    pts = 0.0
    if snap.rsi is not None:
        pts += min(30.0, max(0.0, snap.rsi - 70.0) * 2.0)      # RSI 85 -> 30
    if snap.macd_hist_slope is not None and snap.macd_hist_slope < 0:
        pts += 20.0                                            # momentum rolling over
    if snap.obv is not None and snap.obv[1] < 0:
        pts += 20.0                                            # distribution
    if snap.volume_ratio is not None:
        pts += 20.0 if snap.volume_ratio < 0.8 else (10.0 if snap.volume_ratio < 1.0 else 0.0)
    if snap.stochastic is not None and snap.stochastic[0] > 80:
        pts += 10.0                                            # stochastic confirm
    return int(min(100.0, pts))
```
- [ ] Run RED→GREEN. Checkpoint.

---

## Task 5: family-aware `detect_short_triggers` + `generate_short_signal` routing

**Files:** modify module + test.

- [ ] **Tests (RED):**
```python
def test_detect_fade_top_requires_above_ema200_and_deteriorating_tape():
    snap = _snap(price=120.0, ema200=100.0, rsi=80.0, macd_hist_slope=-0.4,
                 obv=(1e6, -5.0), volume_ratio=0.7, stochastic=(85.0, 80.0))
    closes = [100.0]*20 + [118, 119, 120, 120, 120]
    bars = _bars(closes, vols=[3e6]*22 + [3e6, 2e6, 1.5e6])
    # deteriorating tape + above EMA200 -> fade-top fires
    assert "exhaustion" in ss.detect_short_triggers(snap, bars, regime="neutral", tape_deteriorating=True)
    # calm tape -> no fade-top
    assert ss.detect_short_triggers(snap, bars, regime="neutral", tape_deteriorating=False) == []

def test_generate_fade_top_uses_top_quality_score():
    snap = _snap(price=120.0, atr=2.0, ema200=100.0, rsi=85.0, macd_hist_slope=-0.5,
                 obv=(1e6, -5.0), volume_ratio=0.7, stochastic=(85.0, 80.0))
    closes = [100.0]*20 + [118, 119, 120, 120, 120]
    bars = _bars(closes, vols=[3e6]*22 + [3e6, 2e6, 1.5e6])
    sig = ss.generate_short_signal(
        "TESTZ", snap, bars, regime_id="neutral", category="stocks_large",
        risk_tier="moderate", preferred_risk="moderate",
        sentiment_avg=None, sentiment_count=0, today=date(2026, 9, 15),
        tape_deteriorating=True,
    )
    assert sig is not None and sig.trigger in ("exhaustion", "distribution")
    assert sig.stop_loss > sig.entry > sig.take_profit
```
- [ ] **Impl — replace `detect_short_triggers`:**
```python
def detect_short_triggers(snap, bars, regime, tape_deteriorating: bool = False) -> list[str]:
    if regime in _BLOCKED_REGIMES:
        return []
    matched: list[str] = []
    below_ema = snap.ema200 is None or snap.price < snap.ema200
    above_ema = snap.ema200 is not None and snap.price > snap.ema200
    # Breakdown family — bear regime, below EMA200.
    if regime == "bear" and below_ema:
        if support_breakdown(snap, bars):
            matched.append("support_breakdown")
        if failed_breakout(snap, bars):
            matched.append("failed_breakout")
        if failed_retest(snap, bars):
            matched.append("failed_retest")
    # Fade-top family — deteriorating tape, above EMA200.
    if tape_deteriorating and above_ema:
        if distribution(snap, bars):
            matched.append("distribution")
        if exhaustion(snap, bars):
            matched.append("exhaustion")
    return matched
```
- [ ] **Impl — `generate_short_signal`:** add `tape_deteriorating: bool = False` kwarg; pass it to `detect_short_triggers`; route scoring by family:
```python
    if trigger in _FADE_TOP_TRIGGERS:
        base = top_quality_score(snap)
    else:
        base = score_candidate(snap, "short", regime_id, category, preferred_risk,
                               sentiment_avg, sentiment_count, today=today).confidence
    confidence = min(100, base + TRIGGER_BASE_OFFSET[trigger])
```
(rest — floor, SL/TP stretch, R:R — unchanged.)
- [ ] Run RED→GREEN; also run the full shorts test file (5a tests must still pass). Checkpoint.

---

## Task 6: validation harness — fade-top cohort + re-run gate

**Modify** `scripts/validate_shorts_breakdown.py`.

- [ ] Compute `tape_deteriorating` per day from the prefetched `^VIX`/`^GSPC` series sliced to `d`, and pass into `generate_short_signal`. Activate the loop on days where `regime == "bear"` **or** `tape_deteriorating` (so fade-tops get their non-bear days).
- [ ] Report per-family counts + WR/avg-R (breakdown vs fade-top) in addition to the combined line.
- [ ] Run: `docker-compose exec -T app python -m scripts.validate_shorts_breakdown`
- [ ] **Gate (fade-top cohort):** n ≥ 30 and avg-R > 0 & WR > 0.33, net of the 0.1R friction assumption. If unmet → iterate the trigger/scorer thresholds, do NOT lower the 60 floor.
- [ ] Checkpoint.

---

## VALIDATION RESULT (2026-09-17) — accepted

Iterated the harness (per "iterate triggers/windows, never lower the floor"): extended windows to include
top-formation periods (fade-tops fire ABOVE EMA200, i.e. before the bear — the bear-only windows missed
them); broadened the watchlist to ~27 names; loosened `exhaustion` to RSI>70 + (MACD-roll OR volume-fade).
Progression of the fade-top cohort: 5 → 16 → 27 → **29** signals; net-of-0.1R-friction edge +1.49(noise@5)
→ +0.29 → +0.54 → **+0.49R**, WR ~52%. Final (de-duped, 4 top/bear windows, ~27 symbols):

| cohort | n | WR | avg_r | net (−0.1R) | freq gate | edge gate |
|---|---|---|---|---|---|---|
| FADE_TOP | 29 | 0.522 | +0.591 | +0.491 | 29<30 (near) | PASS |
| BREAKDOWN | 172 | 0.398 | +0.149 | +0.049 | PASS | PASS |
| ALL | 201 | 0.423 | +0.212 | +0.112 | PASS | PASS |

**Decision (user-accepted): fade-top VALIDATED in substance.** The edge is strong and *stable/strengthening*
across every iteration and four distinct top regimes (2015/2018/2020/2021-22); 29 vs 30 is noise, and
chasing the 30th trade would be teaching-to-the-test. `distribution` carries the family (24/29); `exhaustion`
stays selective (5) — the RSI>70 anchor is inherently rare. The formal n≥30-with-conflict bar belongs to the
full walk-forward (shorts-recovery-plan Phase 7), not this smoke test.

**Key finding for Phase 6:** broadening the watchlist *helped fade-top but thinned breakdown* (+0.165R net on
the tight 14-name list → +0.049R on the broad 27). The two families want DIFFERENT candidate universes —
breakdown tighter, fade-top broader.

## Self-Review
- Spec §3b (distribution, exhaustion) → Tasks 3; §5 bespoke scorer → Task 4; Option-1 activation → Task 2; family-aware gates/routing → Task 5; validation → Task 6. ✓
- EMA200 gate flip (fade-top requires ABOVE) captured in Task 5. ✓
- Backward compat: 5a breakdown tests pass `regime="bear"` + below-EMA200 fixtures → still fire under the new family-aware `detect`. ✓
- No placeholders; OBV/MACD kept binary to avoid cross-symbol magnitude issues (documented). ✓
