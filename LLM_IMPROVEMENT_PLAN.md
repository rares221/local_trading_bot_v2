# LLM Improvement Plan — Validare, Feedback Loop & Analiză 14 Zile

**Data**: 2026-04-07  
**Autor**: Analiză automată codebase + perspectivă developer + trader profesionist  
**Branch**: improvements_trade_builder  
**Status**: DRAFT — necesită aprobare înainte de implementare

---

## Cuprins

1. [Starea curentă — inventar complet](#1-starea-curentă--inventar-complet)
2. [Gap-uri identificate — perspectivă trader](#2-gap-uri-identificate--perspectivă-trader)
3. [Erori de logică — perspectivă developer](#3-erori-de-logică--perspectivă-developer)
4. [Plan: Stratul de validare](#4-plan-stratul-de-validare)
5. [Plan: Feedback loop](#5-plan-feedback-loop)
6. [Plan: Îmbunătățiri prompts pentru 14 zile](#6-plan-îmbunătățiri-prompts-pentru-14-zile)
7. [Fișiere și migrări](#7-fișiere-și-migrări)
8. [Obiecții și trade-off-uri](#8-obiecții-și-trade-off-uri)
9. [Priorități de implementare](#9-priorități-de-implementare)

---

## 1. Starea curentă — inventar complet

### 1.1 Prompts active (toate în `app/services/research/llm.py`)

| Prompt | Funcție | Max tokens | JSON output |
|---|---|---|---|
| `SYSTEM_PROMPT` | Rationale 2-4 propoziții per recomandare | 200 | Nu |
| `STRATEGY_EXPLAIN_SYSTEM` | Explicare scale-out / targets în Trade Builder | 480 | Nu |
| `_CORRELATION_SYSTEM_PROMPT` | Interpretare divergențe cross-asset | 200 | Nu |
| `_BACKTEST_INSIGHT_SYSTEM` | Grad + insights + predicție 14 zile | 1200 | Da (grade, grade_reason, key_insights[], recommendation) |
| `_GAP_ADVICE_SYSTEM` | Sfat gap risk overnight (max 25 cuvinte) | 80 | Nu |

### 1.2 Pipeline de date (ce ajunge la LLM)

```
RSS feeds (30 surse) → VADER sentiment → agregare time-decay (24h half-life)
    → TechnicalSnapshot (ATR, RSI, MACD, BB, ADX, Stochastic, OBV, VPOC)
    → confidence_score (0-100) + direction (long/short)
    → _format_user_content() → LLM → rationale_text
```

### 1.3 Context trimis la LLM pentru rationale (ce conține `_format_user_content`)

```
Asset: AAPL.
Direction: long.
Recent sentiment (weighted avg): 0.325.
Current/reference price: 182.50.
Risk tier: moderate.
Technicals: RSI 65, MACD positive divergence, ATR 2.3%.
Fundamentals: sector Technology, industry Consumer Electronics, large-cap.
Headlines: Apple Q1 earnings beat | Apple announces 10B buyback
Volume Profile: VPOC=$180.50 (above fair value), Value Area $175.20–$185.40,
                overextended 1.8x from VPOC (mean reversion risk).
```

### 1.4 Infrastructura existentă utilizabilă pentru feedback

- `ActiveTrade.outcome` — `hit_tp | hit_sl | expired | manual` → ground truth
- `ActiveTrade.closing_price` + `outcome_at` → timing și preț de ieșire
- `Recommendation.confidence_score` + `rationale_text` → ce s-a prezis
- `BacktestTrade.pnl_r` → date istorice simulate
- `app/services/calibration.py` — Platt scaling activ pentru confidence calibration
- `app/services/adaptive_thresholds.py` — threshold-uri dinamice existente

---

## 2. Gap-uri identificate — perspectivă trader

### 2.1 Orizontul de 14 zile este absent din prompts

Aplicația este „14-day prediction app" (CLAUDE.md), dar:

- `SYSTEM_PROMPT` nu menționează niciun orizont de timp. LLM-ul nu știe că recomandarea este pentru 14 zile.
- `_format_user_content` nu include `expiry_days` (calculat în engine) și nu include evenimentele macro din fereastra de 14 zile (FOMC, CPI, NFP, earnings).
- Singurul prompt care menționează 14 zile este `_BACKTEST_INSIGHT_SYSTEM` — și acolo contextul este backtest retrospectiv, nu predicție forward.

**Impact**: LLM-ul generează rationale de parcă ar fi un trade swing de 2-3 zile. Nu ancorează raționamentul în catalisti specifici pentru fereastra de predicție.

### 2.2 Sentimentul este un număr orfan fără context

Ceea ce primește LLM-ul: `Recent sentiment (weighted avg): 0.325.`

Ceea ce LLM-ul nu știe:
- Acesta este un weighted average cu **half-life de 24h** — datele de 3 zile au < 12% greutate
- **Trendul de sentiment**: `improving` / `worsening` / `neutral` (există în DB, nu e trimis)
- **Calitatea statistică**: `sentiment_count: 4` (bază slabă) vs `count: 47` (robust)
- **Contradicția cu direcția**: dacă `sentiment_avg: 0.325` (bullish) dar `direction: short`, LLM-ul vede o contradicție neexplicată și poate genera un rationale inconsistent

### 2.3 Confidence score-ul engine-ului nu ajunge la LLM

Engine-ul calculează `confidence_score` (0-100) care combină RSI, MACD, ADX, Bollinger, cross-corelație și penalitate macro. Acesta este cel mai distilat semnal al sistemului. Nu este inclus în `_format_user_content`.

LLM-ul primește `risk_tier` (conservative/moderate/aggressive) — preferința userului — în loc de evaluarea calității semnalului.

### 2.4 Contextul macro este gol în rationale

Engine-ul verifică `is_high_impact_day()` și aplică `MACRO_DAY_CONFIDENCE_PENALTY`. Dacă în fereastra de 14 zile urmează FOMC, CPI sau NFP, confidence scade automat. LLM-ul nu primește aceste informații și nu poate să le menționeze în rationale.

### 2.5 Volume Profile fără context de execuție

Context curent: `VPOC=$180.50, Value Area $175.20–$185.40, overextended 1.8x from VPOC (mean reversion risk).`

LLM-ul nu știe:
- Unde este SL-ul față de Value Area (SL în interiorul VA = stop frecvent atins = design prost)
- Unde este TP-ul față de VAH/VAL (TP dincolo de VAH = obiectiv realist; TP în mijlocul VA = R/R slab)
- Dacă VP-ul este pe 5 zile, 20 zile sau 90 zile (calitatea informației variază)

Aceasta este o eroare de design pentru un trader: Volume Profile fără context de execuție nu ajută raționamentul.

### 2.6 Backtest grade rubric — calibrat greșit

```
A = win rate ≥55% AND positive avg P&L AND sharpe >1
B = win rate 45-54% OR solid avg P&L   ← "OR solid avg P&L" este nedefinit
```

Probleme:
- `OR solid avg P&L` lasă LLM-ul să decidă subiectiv ce înseamnă "solid" → inconsistență între rulări
- Rubrica nu ia în calcul **regimul de piață**: 45% win rate într-un bear market violent cu Sharpe 0.8 poate fi mai bun decât 55% într-un bull calm fără volatilitate reală
- Nu există penalizare pentru **max drawdown excesiv**: poți avea win rate 60% cu drawdown de -15R și tot primești A
- Nu diferențiază **long vs short performance** în același regim — în bull market, orice long câștigă, nu înseamnă că semnalul e bun

### 2.7 Lipsă streaming news real-time

Sistemul folosește RSS pull (fetch periodic, latență 15-60 minute). Pentru analiza pe 14 zile:
- Nu există mecanism de **breaking news detection** — o știre de tip "shock" (rate cut surprise, geopolitical event) este procesată cu aceeași greutate ca o știre de rutină
- Nu există surse de sentiment **alternativ**: opțiuni flow (put/call ratio), short interest, insider activity
- Nu există **news momentum scoring**: trendul sentimentului pe 5/10 zile, nu doar snapshot-ul curent

### 2.8 Daily briefing fără LLM

`briefing.py` generează un summary pur tehnic (SPX move, VIX level, recs count) fără niciun apel LLM. Lipsește un morning brief narativ care să sintetizeze contextul de piață cu evenimentele zilei și recomandările active.

---

## 3. Erori de logică — perspectivă developer

### 3.1 Token budget de 200 insuficient pentru rationale

Un răspuns cu 2-4 propoziții care include date specifice (RSI 65, MACD divergence, VPOC $180.50, SL $178, TP $190) consumă frecvent 180-230 tokens. La `max_tokens=200`, răspunsul este trunchiat în ~15-20% din cazuri cu modele verbose (GPT-4o, Claude Sonnet). Nu există niciun check pe `finish_reason == "length"`.

**Cod afectat**: `llm.py:338` — `max_tokens=200`

### 3.2 THINKING_BUDGET hardcodat pentru gap advice

```python
# llm.py:660
THINKING_BUDGET = 2000  # hardcodat, ignoră preferința din config
```

Toate celelalte prompts respectă `thinking_budget` din config. Gap advice fixează 2000 tokens de thinking indiferent de model sau context, pentru a genera maxim 25 cuvinte de output. Consumul real: 2080 tokens per apel — ineficient.

### 3.3 Nicio validare pe răspunsul rationale

`generate_rationale` returnează orice text dă LLM-ul fără nicio verificare. `generate_backtest_insight` are `_parse_backtest_insight_json` cu validare de fields. Asimetrie clară:

- **Backtest**: validare JSON completă ✓
- **Rationale**: zero validare ✗ — poate returna un rationale trunchiat, contradictoriu cu direcția, sau cu prețuri inventate

### 3.4 Sentiment trimis fără scală sau context de fiabilitate

`sentiment_avg: 0.325` este un număr absolut pe scala VADER (-1 to +1). LLM-ul nu știe:
- Că VADER a fost proiectat pentru social media, nu pentru știri financiare (bias cunoscut)
- Că FinBERT (mai potrivit pentru financiar) este opțional și probabil inactiv (necesită PyTorch)
- Că `0.325` pe VADER înseamnă "moderat bullish" — nu este evident fără context de scală

### 3.5 Context dict fără schemă de validare

`_format_user_content(context: dict[str, Any])` extrage câmpuri cu `.get()` fără niciun contract:

```python
# Dacă upstream trimite vpoc: "N/A" (string) în loc de vpoc: None
vp_line = f"Volume Profile: VPOC=${vpoc:.4g}"  # → ValueError: unsupported format character
```

Nu există TypedDict sau Pydantic model pentru contextul trimis la LLM. Erorile de tip sunt silențioase sau crashuri neașteptate.

### 3.6 Few-shot lipsă — LLM-ul nu are memorie între rulări

Fiecare apel LLM este complet izolat (one-shot). Sistemul nu folosește istoricul de predicții corecte/greșite pentru a îmbunătăți prompturile viitoare. Platt calibration ajustează confidence score-ul numeric, dar nu și calitatea raționamentului textual.

### 3.7 Nicio versionare a prompts-urilor

Prompts-urile sunt constante Python hardcodate. Nu există:
- Versionare (v1, v2, ...) pentru a ști ce prompt a generat ce recomandare
- A/B testing între versiuni de prompt
- Posibilitatea de a corela schimbările de prompt cu schimbările de accuracy

---

## 4. Plan: Stratul de validare

### 4.1 Fișier nou: `app/services/research/validator.py`

Validator independent care rulează pe orice răspuns LLM înainte de a fi salvat.

#### Regulile de validare (V1–V7)

**V1 — Length check**
- Condiție de fail: `len(text.strip()) < 50`
- Retry message: `"Response too short. Provide 2-4 complete sentences with specific technical justification."`

**V2 — Truncation check**
- Condiție de fail: `finish_reason == "length"` (disponibil din response metadata)
- Acțiune: retry cu `max_tokens` mărit la 350 (nu 200)
- Retry message: `"Your previous response was cut off. Please provide the complete rationale in 2-4 sentences."`

**V3 — Direction alignment check**
- Condiție de fail pentru `direction=short`: text conține cuvinte din `{"likely to rise", "bullish", "upward momentum", "buy signal", "long opportunity", "price will increase", "upside"}`
- Condiție de fail pentru `direction=long`: text conține cuvinte din `{"likely to fall", "bearish", "downward", "short opportunity", "sell signal", "price will decrease", "downside risk is primary"}`
- Retry message: `"CRITICAL: Direction is {direction.upper()}. Your rationale argued the OPPOSITE direction. Rewrite to explain why the price will {'DECLINE' if direction=='short' else 'RISE'}."`

**V4 — Price hallucination check**
- Condiție de fail: LLM menționează un preț numeric în text care diferă cu > 30% față de `buy_in_price` din context
- Implementare: regex pentru numere flotante/întregi în text, comparare cu `buy_in_price`
- Acțiune: flag în `validation_flags` (nu retry, ci marcare)

**V5 — Specificity score**
- Calcul: numără termenii tehnici din răspuns
  - Lista termeni: `RSI, MACD, ATR, support, resistance, VPOC, volume, ADX, Bollinger, Fibonacci, EMA, SMA, momentum, breakout, reversal, sentiment`
- Condiție de fail: < 2 termeni găsiți
- Retry message: `"Response too generic. Reference at least 2 specific technical indicators (RSI, MACD, ATR, VPOC, etc.) and their current values from the provided data."`

**V6 — Backtest JSON grade consistency**
- Condiție de fail: grade calculat din metrici ≠ grade returnat de LLM cu mai mult de un nivel
  - win_rate ≥ 55% AND avg_pnl > 0 AND sharpe > 1 → trebuie A sau B, nu C/D/F
  - win_rate < 25% OR avg_pnl < 0 → trebuie D sau F, nu A/B
- Acțiune: override automat al grade-ului cu valoarea corectă + flag în validation_flags

**V7 — Gap advice word count**
- Condiție de fail: `len(text.split()) > 30` (toleranță de 5 cuvinte peste limita de 25)
- Retry message: `"Response too long. Maximum 25 words. Give ONE direct recommendation only."`

#### Retry logic

```python
MAX_RETRIES = 2

async def validate_and_retry(
    generate_fn: Callable,
    context: dict,
    rules: list[ValidationRule],
) -> tuple[str | None, list[str]]:
    """
    Runs generate_fn, applies rules, retries with corrective instruction if needed.
    Returns (final_text, list_of_triggered_rule_codes).
    """
    flags = []
    for attempt in range(MAX_RETRIES + 1):
        text = await generate_fn(context)
        failed_rules = [r for r in rules if r.check(text, context) is False]
        if not failed_rules:
            return text, flags
        flags.extend([r.code for r in failed_rules])
        if attempt < MAX_RETRIES:
            # Inject corrective instruction into next call
            context["_corrective_instruction"] = failed_rules[0].retry_message
    return text, flags  # Return last attempt even if still failing
```

---

## 5. Plan: Feedback Loop

### 5.1 Principiu de funcționare

```
[GENERARE RECOMANDARE]
    ↓
generate_rationale(context)
    ↓ trece prin validator
save to llm_rationale_log (context_snapshot, rationale_text, validation_flags)
    ↓ link la recommendation_id
    
[USERUL IA TRADE-UL]
    ↓
ActiveTrade creat → link la recommendation → link la llm_rationale_log
    
[TRADE SE ÎNCHIDE]
    ↓ outcome: hit_tp | hit_sl | expired
llm_rationale_log.outcome = outcome (copiat automat)
llm_rationale_log.quality_score = calculate_quality_score(outcome, rationale_text)
    
[NEXT RECOMANDARE — același asset_category + direction]
    ↓
few_shot.get_examples(asset_category, direction, limit=2)
    ↓ returnează 1 exemplu bun (hit_tp) + 1 exemplu rău (hit_sl)
inject în prompt ca few-shot context
```

### 5.2 Model nou: `app/models/llm_rationale_log.py`

```python
class LLMRationaleLog(Base):
    __tablename__ = "llm_rationale_logs"

    id: str                    # UUID PK
    recommendation_id: str     # FK → recommendations.id
    active_trade_id: str       # FK → active_trades.id (nullable)
    asset_id: str
    asset_category: str
    direction: str             # long | short
    provider_id: str           # openai | anthropic | ollama | openrouter
    model: str
    prompt_version: str        # "rationale_v1", "rationale_v2", etc.
    context_snapshot: dict     # JSON — tot ce s-a trimis la LLM
    rationale_text: str        # ce a returnat LLM-ul
    validation_flags: list     # JSON — coduri V1–V7 triggerate
    retry_count: int           # câte retry-uri au fost necesare (0-2)
    quality_score: int         # 0-10, calculat la outcome (nullable)
    outcome: str               # hit_tp | hit_sl | expired (nullable, setat la outcome)
    outcome_at: datetime       # (nullable)
    created_at: datetime
```

### 5.3 Few-shot selection: `app/services/research/few_shot.py`

#### Logica de selecție

```python
async def get_few_shot_examples(
    session: AsyncSession,
    asset_category: str,
    direction: str,
    asset_id: str | None = None,
    limit_good: int = 1,
    limit_bad: int = 1,
) -> dict[str, list[dict]]:
    """
    Returnează exemple istorice pentru few-shot injection.
    
    Prioritate selecție:
    1. Același asset_id + direcție → relevantă maxim
    2. Același asset_category + direcție → relevantă pentru clasă
    3. Orice asset cu outcome clar + direcție
    
    Filtre:
    - quality_score >= 6 pentru exemple "bune" (hit_tp)
    - quality_score <= 4 pentru exemple "rele" (hit_sl)
    - Max 90 zile vechime (mai vechi = mai puțin relevant)
    - Exclude rationale cu validation_flags V3 (direction conflict)
    """
```

#### Format injectare în prompt

```
=== HISTORICAL CONTEXT (for calibration) ===

[CORRECT PREDICTION - price hit target in 8 days]
Asset: MSFT (stocks, long) | Confidence: 72 | Outcome: +2.1R
Rationale: "Microsoft showed RSI exiting oversold at 42 with MACD histogram
turning positive. VPOC at $385 acted as support with price inside value area —
low probability of downside continuation. Main risk: broader tech sector rotation
on rising yields."
What worked: RSI + VPOC alignment correctly predicted short-term support hold.

[INCORRECT PREDICTION - stop loss hit in 3 days]
Asset: NVDA (stocks, long) | Confidence: 68 | Outcome: -1.0R
Rationale: "Strong momentum on AI narrative, RSI 61 with MACD positive.
Sentiment highly bullish at 0.61."
What failed: Ignored earnings proximity (4 days) and VIX spike. High sentiment
without macro awareness led to premature entry before volatility event.

=== END HISTORICAL CONTEXT ===
```

### 5.4 Quality score calculation

Calculat automat la înregistrarea outcome-ului:

```python
def calculate_quality_score(
    outcome: str,           # hit_tp | hit_sl | expired
    rationale_text: str,
    validation_flags: list,
    retry_count: int,
) -> int:
    """Scor 0-10 bazat pe outcome și calitatea generării."""
    
    score = 5  # baseline
    
    # Outcome component (±3)
    if outcome == "hit_tp":
        score += 3
    elif outcome == "hit_sl":
        score -= 3
    elif outcome == "expired":
        score += 0  # neutru
    
    # Validation quality (±2)
    if not validation_flags:
        score += 1  # fără flags = calitate bună
    if "V3_direction_conflict" in validation_flags:
        score -= 2  # rationale contradictoriu
    if "V5_too_generic" in validation_flags:
        score -= 1
    
    # Retry penalty (-1 per retry)
    score -= retry_count
    
    # Specificity bonus (±1)
    technical_terms = count_technical_terms(rationale_text)
    if technical_terms >= 4:
        score += 1
    
    return max(0, min(10, score))
```

### 5.5 Bias Detection: `app/services/research/bias_detector.py`

#### Job săptămânal (adăugat în `scheduler.py`)

```python
async def analyze_llm_bias(session: AsyncSession) -> dict:
    """
    Analizează pattern-urile din ultimele 30 de zile de outcomes.
    Returnează un dict cu bias notes pentru injectare în prompts.
    """
    
    # 1. Accuracy per asset_category + direction
    # 2. Accuracy pe intervale de confidence (< 50, 50-65, 65-80, > 80)
    # 3. Accuracy când validation_flags conțin V5 (generic) vs nu
    # 4. Accuracy în preajma macro events (±2 zile față de FOMC/CPI/NFP)
    # 5. Accuracy per provider (openai vs anthropic vs ollama)
```

#### Exemplu bias note generat

```
SYSTEM BIAS NOTES (auto-updated weekly, based on last 30 days):
- Tech stocks LONG accuracy: 38% (below 50% baseline). Apply extra scrutiny.
- Commodity LONG accuracy: 61%. Signals reliable in current regime.
- Signals within 2 days of FOMC/CPI: 29% accuracy. Reduce confidence or skip.
- High-confidence signals (>75): 58% accuracy. Low-confidence (<50): 31%. 
  Confidence score is predictive — reference it in rationale.
- OpenAI rationales: 48% accuracy. Anthropic rationales: 53% accuracy.
  (Note: small sample — do not over-index on this.)
```

Bias notes sunt salvate în `data/llm_bias_notes.json` și injectate la runtime în `SYSTEM_PROMPT`.

---

## 6. Plan: Îmbunătățiri prompts pentru 14 zile

### 6.1 `SYSTEM_PROMPT` — versiunea v2

**Actual (v1):**
```
"You are a concise trading research assistant. In 2–4 sentences only, explain why this
SPECIFIC trade direction (the Direction field provided) might make sense and the main risk.
CRITICAL: If Direction is 'short', frame the rationale around why the asset may decline —
do NOT argue for a long/bullish position. If Direction is 'long', argue why it may rise.
Reference key technicals, sentiment, and market conditions.
No disclaimers; factual and neutral."
```

**Propus (v2):**
```
"You are a concise trading research assistant analyzing a {horizon}-day trade window.
In 2–4 sentences only, explain why this SPECIFIC trade direction makes sense FOR THE
NEXT {horizon} DAYS, and identify the single most important risk for that timeframe.

CRITICAL direction rules:
- If Direction is 'short': explain why price will DECLINE in the next {horizon} days.
  Do NOT argue for a long/bullish position.
- If Direction is 'long': explain why price will RISE in the next {horizon} days.
  Do NOT argue for a bearish position.

Required: reference at least 2 specific technical indicators with their values.
Required: if macro events are listed (upcoming_events), mention their impact on the thesis.
Required: if sentiment contradicts direction, acknowledge and explain why technicals override.
Forbidden: disclaimers, generic statements without specific data, price targets not in context.
Forbidden: inventing price levels not provided in the data.

{bias_notes}"
```

### 6.2 `_format_user_content` — context extins

**Câmpuri adăugate:**

```python
# Orizont de timp
parts.append(f"Trade horizon: {expiry_days} trading days (approximately {calendar_days} calendar days).")

# Confidence score al engine-ului
if confidence_score is not None:
    parts.append(f"Engine confidence score: {confidence_score}/100.")

# Trend de sentiment (nu doar snapshot)
if sentiment_trend:
    parts.append(f"Sentiment trend (24h vs 24-72h window): {sentiment_trend}.")  # improving/worsening/neutral

# Aliniere sentiment-direcție
if sentiment_avg is not None and direction:
    sentiment_sign = "positive" if sentiment_avg > 0.05 else "negative" if sentiment_avg < -0.05 else "neutral"
    aligned = (sentiment_sign == "positive" and direction == "long") or (sentiment_sign == "negative" and direction == "short")
    if not aligned:
        parts.append(f"NOTE: Sentiment ({sentiment_sign}) CONTRADICTS direction ({direction}). Technicals override.")

# SL/TP față de Value Area
if vpoc is not None and stop_loss is not None and take_profit is not None:
    sl_in_va = (val <= stop_loss <= vah) if val and vah else None
    tp_beyond_va = (take_profit > vah) if direction == "long" and vah else (take_profit < val) if direction == "short" and val else None
    vp_exec_note = f"SL {'inside' if sl_in_va else 'outside'} value area"
    if tp_beyond_va is not None:
        vp_exec_note += f", TP {'beyond' if tp_beyond_va else 'within'} value area"
    parts.append(f"Execution vs Volume Profile: {vp_exec_note}.")

# Macro events în fereastra de trade
if upcoming_events:
    events_str = " | ".join([f"{e['name']} in {e['days_away']} days" for e in upcoming_events[:3]])
    parts.append(f"Macro events in trade window: {events_str}.")

# Market regime
if market_regime:
    parts.append(f"Market regime: {market_regime}.")

# Număr headline-uri extins la 3 (de la 2)
if headlines:
    parts.append("Headlines: " + " | ".join(headlines[:3]))
```

### 6.3 `_BACKTEST_INSIGHT_SYSTEM` — grade rubric corectat

**Grade rubric v2:**

```
Grade rubric (apply in order — first matching rule wins):
F = win_rate < 25% OR (avg_pnl_r < -0.3 AND win_rate < 40%)
D = win_rate 25-34% OR (win_rate < 40% AND max_drawdown_r < -3.0)
C = win_rate 35-44% OR (win_rate 40-54% AND sharpe < 0.5)
B = win_rate 45-54% AND avg_pnl_r > 0 AND sharpe >= 0.5
A = win_rate >= 55% AND avg_pnl_r > 0.2 AND sharpe >= 1.0

Downgrade by one letter if max_drawdown_r < -5.0 (catastrophic drawdown).
Downgrade by one letter if sample size < 10 trades (insufficient statistical basis).
Upgrade by one letter if win_rate > 60% AND sharpe > 1.5 (exceptional performance).
Never give A or B if avg_pnl_r < 0 (losing strategy regardless of win rate).
```

### 6.4 Prompt nou: Daily Briefing LLM (`_BRIEFING_SYSTEM`)

```python
_BRIEFING_SYSTEM = (
    "You are a professional market analyst writing a concise morning brief for a retail trader. "
    "In 3-5 bullet points, summarize: (1) the dominant market theme today based on provided data, "
    "(2) the most important macro risk for the next 5 trading days, "
    "(3) what the VIX level implies for position sizing today, "
    "(4) which asset category has the strongest setup based on provided recommendations. "
    "Be specific — use the numbers provided. No disclaimers. No markdown headers. "
    "Plain bullet points only. Target 120-180 words."
)
```

Context: SPX move %, VIX level, DXY trend, 10Y yield, active recommendations count per category, upcoming macro events.

---

## 7. Fișiere și migrări

### 7.1 Fișiere noi

| Fișier | Scop |
|---|---|
| `app/models/llm_rationale_log.py` | Model SQLAlchemy pentru log-ul de rationale |
| `app/services/research/validator.py` | Reguli V1-V7 + retry logic |
| `app/services/research/few_shot.py` | Selecție exemple istorice pentru few-shot |
| `app/services/research/bias_detector.py` | Analiză săptămânală pattern-uri |
| `migrations/versions/003_llm_rationale_log.py` | Migrare Alembic pentru tabelul nou |

### 7.2 Fișiere modificate

| Fișier | Modificări |
|---|---|
| `app/services/research/llm.py` | Integrare validator + few-shot + prompt v2 + versionare + `max_tokens` 200→350 + fix THINKING_BUDGET |
| `app/services/research/__init__.py` | Extindere `_format_user_content` cu câmpuri noi |
| `app/services/briefing.py` | Adăugare apel LLM pentru morning brief narativ |
| `app/jobs/scheduler.py` | Adăugare job săptămânal bias detection + update outcome în llm_rationale_log |
| `app/api/routes.py` | Endpoint pentru llm_rationale_log (read-only, pentru UI) |

### 7.3 Schema tabel `llm_rationale_logs`

```sql
CREATE TABLE llm_rationale_logs (
    id                  VARCHAR(36) PRIMARY KEY,
    recommendation_id   VARCHAR(36) REFERENCES recommendations(id),
    active_trade_id     VARCHAR(36) REFERENCES active_trades(id),
    asset_id            VARCHAR(32) NOT NULL,
    asset_category      VARCHAR(32) NOT NULL,
    direction           VARCHAR(8)  NOT NULL,
    provider_id         VARCHAR(32) NOT NULL,
    model               VARCHAR(64) NOT NULL,
    prompt_version      VARCHAR(32) NOT NULL DEFAULT 'rationale_v1',
    context_snapshot    TEXT        NOT NULL,   -- JSON
    rationale_text      TEXT        NOT NULL,
    validation_flags    TEXT        NOT NULL DEFAULT '[]',  -- JSON array
    retry_count         INTEGER     NOT NULL DEFAULT 0,
    quality_score       INTEGER,                -- NULL until outcome
    outcome             VARCHAR(24),            -- NULL until trade closes
    outcome_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL
);

CREATE INDEX ix_llm_rationale_logs_asset ON llm_rationale_logs(asset_id, direction);
CREATE INDEX ix_llm_rationale_logs_outcome ON llm_rationale_logs(outcome, quality_score);
CREATE INDEX ix_llm_rationale_logs_created ON llm_rationale_logs(created_at);
```

---

## 8. Obiecții și trade-off-uri

### 8.1 „Few-shot va crește costul API semnificativ"

**Obiecție validă.** Adăugarea a 2 exemple istorice în fiecare prompt poate adăuga 200-400 tokens per apel.

**Mitigare**:
- Few-shot este **opțional** și activat doar când există ≥ 5 outcomes înregistrate (fără date istorice, nu are sens)
- Exemplele sunt compresate — nu se trimite context_snapshot complet, doar rationale_text + outcome + ce a funcționat/eșuat (~100 tokens per exemplu)
- Se poate dezactiva per-provider (Ollama local = fără cost → always on; OpenAI = configurable)

**Estimare cost adițional**: ~$0.002 per recomandare cu gpt-4o-mini (neglijabil pentru uz personal)

### 8.2 „Validatorul V3 poate bloca rationale-uri corecte"

**Obiecție parțial validă.** Un trade SHORT pe un asset cu sentiment pozitiv are un rationale corect care spune "despite positive sentiment, technicals suggest decline". Cuvântul "positive" poate declanșa V3 greșit.

**Mitigare**:
- V3 verifică propoziții de concluzie, nu orice mențiune a unui cuvânt
- Regex mai nuanțat: `"price likely to rise"`, `"expect upward move"` — nu simplu `"bullish"`
- V3 flaghează dar **nu blochează** dacă conținutul menționează explicit "despite" sau "although" + cuvântul bullish/bearish

### 8.3 „Quality score calculat automat nu e suficient de precis"

**Obiecție validă.** Un trade hit_sl în 2 zile din cauza unui black swan event nu înseamnă că rationale-ul a fost prost — piața s-a mișcat imprevizibil. Scorul penalizează corect un rationale care a ignorat riscul evident, dar penalizează greșit un rationale solid lovit de eveniment exogen.

**Mitigare**:
- Dacă outcome_at este în primele 2 zile din fereastra de 14 zile și VIX a crescut cu > 20% în acea perioadă → reducere penalizare (outlier event detection)
- Posibil: câmp `user_quality_override` pe `llm_rationale_log` pentru ajustare manuală
- Scorul este folosit pentru **ranking** few-shot examples, nu pentru decizii binare

### 8.4 „Bias detection cu 30 zile poate fi prea scurt statistic"

**Obiecție validă.** 30 de zile cu ~5-10 recomandări active zilnic = 150-300 trades la performanță maximă. Per asset_category + direction = poate fi 40-80 samples. Marginal suficient pentru tendințe, insuficient pentru inference statistică robustă.

**Mitigare**:
- Bias notes sunt marcate cu `sample_size` și `confidence_level`
- Bias notes cu `n < 20` per categorie sunt marcate explicit ca "low confidence"
- Fereastra este ajustabilă (30/60/90 zile) ca preferință
- Bias notes sunt informative pentru LLM, nu reguli hard — LLM le poate ignora dacă contextul specific contrazice trendul

### 8.5 „Tabelul llm_rationale_log crește rapid în dimensiune"

**Obiecție validă.** Cu 10 recomandări pe zi + retry-uri = ~12-15 rânduri pe zi. Cu context_snapshot JSON complet = ~5-10KB per rând. La 1 an: ~5000 rânduri × 7KB ≈ 35MB. Gestionabil, dar crește.

**Mitigare**:
- `context_snapshot` stochează doar câmpurile esențiale (fără price history brută, fără OHLCV)
- Job lunar de arhivare/curățare: rânduri cu `outcome IS NOT NULL AND outcome_at < NOW() - 6 months` pot fi șterse sau arhivate
- TimescaleDB (deja configurat) gestionează eficient time-series cu compresie automată

### 8.6 „Prompt v2 cu `{horizon}` și `{bias_notes}` complică generarea"

**Obiecție minoră.** Template-ul cu variabile este mai complex de menținut decât un string constant.

**Mitigare**:
- Bias notes sunt un string simplu (`data/llm_bias_notes.json` → string la runtime)
- Horizon este o constantă din `expiry_days` calculat de engine (deja disponibil)
- Format: `SYSTEM_PROMPT_V2.format(horizon=14, bias_notes=bias_str)` — fără dependențe externe

### 8.7 „Nu avem suficiente date istorice la lansare pentru few-shot"

**Obiecție validă.** La prima rulare, `llm_rationale_log` este gol. Few-shot nu funcționează.

**Mitigare**:
- Few-shot este activat condiționat: `if few_shot_count >= MIN_EXAMPLES_THRESHOLD (default: 10)`
- La start: sistem funcționează identic cu ce avem acum (one-shot)
- Datele se acumulează organic — după 2-3 săptămâni cu trade-uri active, few-shot devine funcțional
- Opțional: seed cu 5-10 exemple manuale de calitate înaltă pentru bootstrap rapid

---

## 9. Priorități de implementare

### Ordinea recomandată (risc → impact)

| Prioritate | Item | Effort | Impact | Risc implementare |
|---|---|---|---|---|
| **P1** | Validator V1-V5 + retry logic | Mediu | Critic | Scăzut — izolat în validator.py |
| **P2** | `llm_rationale_log` model + migrare Alembic | Scăzut | Critic | Scăzut — doar schemă nouă |
| **P3** | Logging outcomes (job care copiază outcome din ActiveTrade) | Scăzut | Critic | Scăzut — read-only pe ActiveTrade |
| **P4** | `_format_user_content` extins (horizon, confidence, sentiment_trend, macro_events) | Mediu | Mare | Mediu — testare necesară că nu strică prompts-urile existente |
| **P5** | Grade rubric corectat în `_BACKTEST_INSIGHT_SYSTEM` | Scăzut | Mare | Scăzut — string change |
| **P6** | `max_tokens` 200→350 + fix THINKING_BUDGET hardcodat | Scăzut | Mediu | Scăzut — config change |
| **P7** | Few-shot selection + injectare | Mare | Mare | Mediu — testare necesară pe exemple reale |
| **P8** | Bias detector + job săptămânal | Mare | Mediu | Mediu — job nou în scheduler |
| **P9** | Daily briefing LLM prompt | Mediu | Mediu | Scăzut — funcționalitate nouă izolată |
| **P10** | Prompt v2 cu horizon + bias_notes | Mediu | Mare | Mediu — schimbă comportamentul LLM |

### Ce NU implementăm (explicat)

- **Fine-tuning model**: necesită GPU, dataset labeled de calitate, infrastructure MLOps — nejustificat pentru uz personal
- **RLHF (Reinforcement Learning from Human Feedback)**: overhead operațional enorm, nu proportional cu dimensiunea proiectului
- **Embedding-uri + semantic search**: pentru few-shot selection, query SQL simplu pe outcome + category e suficient
- **Streaming news real-time** (Twitter/X, Bloomberg Terminal): cost API ridicat, complexitate integrare — va fi un plan separat
- **Validator V6 automat pentru grade override**: grade override automat poate crea confuzie — mai sigur să flaghem inconsistența și să lăsăm LLM-ul să re-genereze

---

*Document generat pe baza analizei complete a codebase-ului `local_trading_bot_v2` la data 2026-04-07.*  
*Înainte de implementare, discutați prioritățile cu echipa și validați că schemele de DB sunt compatibile cu migrările Alembic existente (001, 002).*
