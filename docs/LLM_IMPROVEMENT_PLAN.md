# LLM Improvement Plan — Validare, Feedback Loop Anti-Halucinație & Orizonturi de Predicție

**Data**: 2026-04-07 (rev 2)  
**Autor**: Analiză automată codebase + perspectivă developer + trader profesionist  
**Branch**: improvements_trade_builder  
**Status**: DRAFT — necesită aprobare înainte de implementare

---

## Cuprins

1. [Starea curentă — inventar complet](#1-starea-curentă--inventar-complet)
2. [Orizonturi de predicție: Near vs Far](#2-orizonturi-de-predicție-near-vs-far)
3. [Gap-uri identificate — perspectivă trader](#3-gap-uri-identificate--perspectivă-trader)
4. [Erori de logică — perspectivă developer](#4-erori-de-logică--perspectivă-developer)
5. [Plan: Stratul de validare + detecție halucinații](#5-plan-stratul-de-validare--detecție-halucinații)
6. [Plan: Feedback loop anti-halucinație cu news + metrici](#6-plan-feedback-loop-anti-halucinație-cu-news--metrici)
7. [Plan: Îmbunătățiri prompts per orizont](#7-plan-îmbunătățiri-prompts-per-orizont)
8. [Fișiere și migrări](#8-fișiere-și-migrări)
9. [Obiecții și trade-off-uri](#9-obiecții-și-trade-off-uri)
10. [Priorități de implementare](#10-priorități-de-implementare)

---

## 1. Starea curentă — inventar complet

### 1.1 Prompts active (toate în `app/services/research/llm.py`)

| Prompt | Funcție | Max tokens | JSON |
|---|---|---|---|
| `SYSTEM_PROMPT` | Rationale 2-4 propoziții per recomandare | 200 | Nu |
| `STRATEGY_EXPLAIN_SYSTEM` | Explicare scale-out / targets în Trade Builder | 480 | Nu |
| `_CORRELATION_SYSTEM_PROMPT` | Interpretare divergențe cross-asset | 200 | Nu |
| `_BACKTEST_INSIGHT_SYSTEM` | Grad + insights + predicție forward | 1200 | Da |
| `_GAP_ADVICE_SYSTEM` | Sfat gap risk overnight (max 25 cuvinte) | 80 | Nu |

### 1.2 Context trimis la LLM (`_format_user_content`) — ce conține azi

```
Asset, Direction, Sentiment avg (float), Price, Risk tier, Technicals (string),
Fundamentals (sector, industry, market cap), Headlines (2 max), Volume Profile (VPOC, VAH, VAL)
```

**Ce LIPSEȘTE din context**: expiry_days, confidence_score, sentiment_trend, market_regime,
DXY trend, macro events în fereastră, SL/TP față de Value Area, aliniere sentiment-direcție.

### 1.3 Infrastructura existentă utilizabilă

- `ActiveTrade.outcome` — `hit_tp | hit_sl | expired | manual` → ground truth
- `ActiveTrade.closing_price` + `outcome_at` → timing și preț de ieșire real
- `Recommendation.confidence_score` + `rationale_text` → ce s-a prezis
- `estimate_expiry_days()` în `technicals.py` — calculează expiry (max 30 zile azi)
- `detect_market_regime()` + `detect_dxy_macro()` în `market_context.py` — calculate dar netrimise la LLM
- `calibration.py` — Platt scaling activ
- `adaptive_thresholds.py` — threshold-uri dinamice existente
- `BacktestTrade.pnl_r` — date istorice simulate

---

## 2. Orizonturi de predicție: Near vs Far

### 2.1 Definiție

| Orizont | Durată | Clasificare |
|---|---|---|
| **Near horizon** | ≤ 14 trading days (~3 săptămâni calendar) | Termen scurt |
| **Far horizon** | > 14 trading days (săptămâni → luni) | Termen mediu-lung |

Această distincție **nu** schimbă ce recomandări se generează — schimbă **ce factori sunt relevanți** în raționament și ce informații trimitem la LLM.

### 2.2 Ce diferă pe fiecare orizont

#### Near horizon (≤ 14 zile)

**Factori dominanți:**
- Indicatori tehnici de moment: RSI, MACD, Stochastic, Bollinger Bands
- Volume Profile și VPOC — foarte relevant (prețul se întoarce la VPOC pe termen scurt)
- Sentimentul de news — de mare impact (decay 24h, dar știrile recente sunt esențiale)
- Evenimentele macro în fereastră (FOMC, CPI, earnings în ≤14 zile) — critice, blochează sau accelerează mișcarea
- ATR și gap risk — relevant pentru sizing și SL plasament

**Ce primește LLM-ul în plus față de azi:**
- Expiry days exact
- Macro events în fereastră cu naming și zile rămase
- Sentiment trend (improving/worsening) cu clarificarea că e window de 24-72h
- Market regime (risk_on/risk_off/cautious)
- Alinierea SL/TP față de Value Area

**Tone de raționament**: specific, tactic, orientat spre entry timing și events iminente.

#### Far horizon (> 14 zile)

**Factori dominanți:**
- Trend structural: EMA50 vs EMA200, ADX (forța trendului pe durată)
- Fundamentale: sector, industry, market cap, poziție în ciclu economic
- Macro trend (nu eventi punctuali): ciclul Fed, curba randamentelor, DXY trend pe 20d/60d
- Rotație sectorială: SPX sector performance relativ
- Sentiment pe fereastră extinsă (5 zile, 10 zile) — snapshot-ul de 24h nu mai e relevant
- Earnings ciclice (nu doar next earnings, ci ciclul trimestrial)

**Ce primește LLM-ul diferit față de near:**
- DXY trend 20d și 60d (nu doar spot)
- Market regime + SPX trend structural (above/below SMA200)
- Sentiment window extinsă: mediana pe 5 și 10 zile (dacă există date)
- Macro cycle context (unde suntem în ciclul Fed: hiking/pausing/cutting)
- Expiry range (nu exact days — ex: "4-8 săptămâni estimat")
- Nu se mai trimite VPOC și intraday Volume Profile (irelevant pe termen lung)

**Tone de raționament**: strategic, orientat pe tendință structurală, recunoaște incertitudinea mai mare.

### 2.3 Unde se determină orizontul

`estimate_expiry_days()` în `technicals.py` (linia 979) calculează zilele estimat până la TP, cu cap la `max_days=30`.

**Modificare necesară**: ridicăm `max_days` la 60 (pentru far horizon până la ~3 luni). Orizontul se clasifică la generarea raționamentului:

```python
NEAR_HORIZON_DAYS = 14  # trading days

def classify_horizon(expiry_days: int) -> str:
    return "near" if expiry_days <= NEAR_HORIZON_DAYS else "far"
```

### 2.4 Prompt-uri separate per orizont

**Decizie**: 2 constante separate `SYSTEM_PROMPT_NEAR` și `SYSTEM_PROMPT_FAR` — mai ușor de ajustat independent, versioning mai clar față de un singur prompt cu ramuri condiționale.

---

## 3. Gap-uri identificate — perspectivă trader

### 3.1 Orizontul de predicție absent din toate prompts-urile

Aplicația suportă orizonturi variabile, dar **niciun prompt nu știe pe câte zile se face predicția**. LLM-ul generează același tip de raționament indiferent dacă trade-ul expiră în 3 zile sau în 6 săptămâni.

**Impact concret**: un trade near horizon cu FOMC peste 2 zile primește același raționament ca un far horizon trade unde FOMC nu contează. Halucinația de timp este o consecință directă.

### 3.2 Sentimentul — număr fără context și fără trend

`Recent sentiment (weighted avg): 0.325` — LLM-ul nu știe:
- Că half-life este 24h (datele vechi de 3 zile = aproape zero greutate)
- Că trendul este `improving` / `worsening` (există în DB, nu e trimis)
- Că `count: 4` vs `count: 47` are o diferență uriașă de fiabilitate statistică
- Că sentiment pozitiv cu direcție SHORT = contradicție neexplicată

### 3.3 Confidence score al engine-ului lipsește din context

Engine-ul calculează `confidence_score` (0-100) combinând RSI, MACD, ADX, Bollinger, cross-corelație, penalitate macro. Nu este trimis la LLM. Consecință: LLM poate genera un rationale puternic pentru un semnal cu `confidence: 31`.

### 3.4 Contextul macro este complet absent din rationale

Engine-ul verifică `is_high_impact_day()` și penalizează confidence. DXY, SPX regime, VIX sunt calculate. Niciunul nu ajunge la LLM în `_format_user_content`.

### 3.5 Volume Profile fără context de execuție

`VPOC=$180.50, Value Area $175.20–$185.40` fără să spui unde sunt SL și TP față de aceste niveluri. Un SL plasat în interiorul Value Area este o problemă serioasă — LLM-ul nu poate comenta pe asta fără datele de execuție.

### 3.6 Far horizon fără date fundamentale adecvate

Fundamentalele trimise azi (sector, industry, market cap) sunt statice și subțiri. Pentru far horizon ai nevoie de: P/E relativ, 52-week range position, DXY impact pe industry. Niciuna din acestea nu este trimisă.

### 3.7 News context prea subțire și fără verificare temporală

2 headlines trimise la LLM, fără timestamp. Nu se verifică dacă headline-ul este din ultimele 24h sau din 5 zile în urmă. Nu se detectează headlines contradictorii pentru același asset.

### 3.8 Backtest grade rubric slab calibrat

```
B = win_rate 45-54% OR solid avg P&L   ← "solid avg P&L" este nedefinit
```
Nu există penalizare pentru max drawdown excesiv. Nu diferențiază long vs short în același regim.

---

## 4. Erori de logică — perspectivă developer

### 4.1 `max_tokens=200` trunchiază frecvent rationale

2-4 propoziții cu date specifice depășesc 200 tokens cu modele verbale. Nu există check pe `finish_reason == "length"`.

### 4.2 `THINKING_BUDGET = 2000` hardcodat în gap advice (`llm.py:660`)

Ignoră preferința din config. 2080 tokens total pentru a genera 25 cuvinte.

### 4.3 Zero validare pe răspunsul rationale

`generate_backtest_insight` → validare JSON completă. `generate_rationale` → zero validare. Orice text trunchiat, contradictoriu sau cu prețuri inventate este salvat direct în DB.

### 4.4 Context dict fără schemă (`llm.py:35`)

`_format_user_content(context: dict[str, Any])` — dacă upstream trimite `vpoc: "N/A"` (string), `f"VPOC=${vpoc:.4g}"` → `ValueError` la runtime. Niciun TypedDict sau Pydantic model.

### 4.5 Halucinații numerice nedetectate

LLM-ul poate inventa valori pentru indicatori (RSI, MACD, ATR) care nu există în context sau diferă semnificativ față de ce i s-a trimis. Salvate în `rationale_text` și afișate utilizatorului ca fapte.

### 4.6 News halucinații nedetectate

LLM-ul poate referenția știri care nu sunt în headline-urile furnizate sau poate atribui greșit timing-ul. Nu există cross-referențiere față de `feed_items`.

### 4.7 Nicio versionare a prompts-urilor

Nu se știe ce versiune de prompt a generat ce recomandare. Imposibil de corelat schimbările de prompt cu accuracy.

### 4.8 `estimate_expiry_days` capsat la 30 de zile (`technicals.py:985`)

Far horizon (> 14 zile) este posibil în teorie dar cap-ul de 30 limitează trades cu obiective mai largi. Nu există nicio distincție azi între un trade de 10 zile și unul de 28 zile.

---

## 5. Plan: Stratul de validare + detecție halucinații

### 5.1 Tipuri de halucinații în context trading

| Tip | Descriere | Exemplu |
|---|---|---|
| **Numerică** | Valori de indicatori inventate | "RSI la 72" când contextul arată RSI=58 |
| **De preț** | Prețuri inventate față de context | "Support la $195" când buy_in_price=$182, niciun nivel $195 în context |
| **De știre** | Referință la o știre care nu e în headlines | "Following yesterday's earnings beat" absent din headlines |
| **Temporală** | Timing greșit al unui eveniment | "Recent FOMC decision" când FOMC era cu 6 săptămâni în urmă |
| **Direcțională** | Raționament opus direcției | direction=SHORT dar LLM explică de ce prețul va crește |
| **De relație** | Corelații inventate | "USD fell, boosting gold" când DXY era bullish în context |

### 5.2 Reguli V1–V8

**V1 — Length check**
- Fail: `len(text.strip()) < 50`
- Retry: `"Response too short. Provide 2-4 complete sentences with specific technical justification."`

**V2 — Truncation check**
- Fail: `finish_reason == "length"` din response metadata
- Acțiune: retry cu `max_tokens` 350

**V3 — Direction alignment**
- Fail SHORT: text conține fraze de concluzie bullish (`"likely to rise"`, `"expect upward move"`, `"bullish outlook"`, `"price will increase"`)
- Fail LONG: text conține fraze de concluzie bearish (`"likely to fall"`, `"bearish pressure dominant"`, `"price will decrease"`)
- Excepție: prezența `"despite"` sau `"although"` înainte de cuvântul contradictoriu → nu este halucinație
- Retry: `"CRITICAL: Direction is {direction.upper()}. Your rationale argued the OPPOSITE. Rewrite."`

**V4a — Price hallucination**
- Extrage toate numerele flotante/întregi din text
- Fail: număr găsit care diferă cu > 15% față de orice preț din context (`buy_in_price`, `stop_loss`, `take_profit`, `vpoc`, `vah`, `val`)
- Retry: `"Do not invent price levels. Only reference prices explicitly provided in the data."`

**V4b — Indicator value hallucination**
- Extrage afirmații numerice despre indicatori (pattern: `"RSI [at/of/around] {N}"`)
- Fail: valoarea diferă cu > 15% față de valoarea reală din `context_snapshot`
- Retry: `"Use only the exact indicator values provided. Do not approximate or invent values."`

**V4c — News/event hallucination** *(flag-only, nu retry)*
- Extrage referințe la știri/evenimente cu verbe de raportare
- Verifică dacă există în headlines furnizate (fuzzy match, threshold 0.6)
- Acțiune: flag `V4c_news_hallucination` în log

**V4d — Temporal hallucination** *(flag-only, nu retry)*
- Detectează expresii temporale: `"yesterday"`, `"last week"`, `"recently"`, `"just announced"`
- Cross-referențiază cu timestamp-urile headline-urilor
- Fail: LLM spune "yesterday" dar cel mai recent headline e de 4+ zile

**V5 — Specificity score**
- Lista termeni tehnici: RSI, MACD, ATR, support, resistance, VPOC, volume, ADX, Bollinger, Fibonacci, EMA, SMA, momentum, breakout, reversal, sentiment
- Fail: < 2 termeni găsiți
- Retry: `"Too generic. Reference at least 2 specific technical indicators with their values."`

**V6 — Backtest JSON grade consistency** *(override automat)*
- win_rate ≥ 55% AND avg_pnl > 0 AND sharpe > 1 → trebuie A sau B
- win_rate < 25% OR avg_pnl < 0 → trebuie D sau F
- Acțiune: override automat + flag `V6_grade_override`

**V7 — Gap advice word count**
- Fail: `len(text.split()) > 30`
- Retry: `"Too long. Maximum 25 words. One direct recommendation only."`

**V8 — Horizon consistency** *(NOU)*
- Near horizon: text nu trebuie să conțină `"long-term"`, `"over the months"`, `"fundamentally"`
- Far horizon: text nu trebuie să conțină `"this week"`, `"by Friday"`, `"intraday"`
- Retry: `"This is a {horizon} horizon trade. Adjust your timeframe references."`

### 5.3 Retry logic

```python
MAX_RETRIES = 2
BLOCKING_RULES = {"V1", "V2", "V3", "V4a", "V4b", "V5", "V7", "V8"}
FLAG_ONLY_RULES = {"V4c", "V4d", "V6"}

async def validate_and_retry(generate_fn, context, rules):
    flags = []
    for attempt in range(MAX_RETRIES + 1):
        text = await generate_fn(context)
        triggered = [r for r in rules if not r.check(text, context)]
        blocking = [r for r in triggered if r.code in BLOCKING_RULES]
        flags.extend([r.code for r in triggered])
        if not blocking:
            return text, flags
        if attempt < MAX_RETRIES:
            context["_corrective_instruction"] = blocking[0].retry_message
    return text, flags  # returnează ultimul attempt chiar și cu flags
```

---

## 6. Plan: Feedback loop anti-halucinație cu news + metrici

### 6.1 Principiul fundamental

Halucinațiile nu se elimină printr-un singur filtru la generare. Se **reduc în timp** prin:

1. **Detecție la generare** (V4a–V4d) → flag imediat
2. **Verificare post-outcome cu date reale** → știm ce s-a întâmplat efectiv vs ce a prezis LLM
3. **Construire profil de halucinații per model/provider** → ce tipuri de erori face fiecare LLM
4. **Injecție de avertizări specifice în prompts viitoare** → hallucination guard personalizat

### 6.2 Schema tabelului `llm_rationale_logs`

```sql
CREATE TABLE llm_rationale_logs (
    id                      VARCHAR(36)  PRIMARY KEY,
    recommendation_id       VARCHAR(36)  REFERENCES recommendations(id),
    active_trade_id         VARCHAR(36)  REFERENCES active_trades(id),  -- nullable
    asset_id                VARCHAR(32)  NOT NULL,
    asset_category          VARCHAR(32)  NOT NULL,
    direction               VARCHAR(8)   NOT NULL,   -- long | short
    horizon                 VARCHAR(8)   NOT NULL,   -- near | far
    horizon_days            INTEGER      NOT NULL,

    -- LLM metadata
    provider_id             VARCHAR(32)  NOT NULL,
    model                   VARCHAR(64)  NOT NULL,
    prompt_version          VARCHAR(32)  NOT NULL,  -- rationale_near_v1, rationale_far_v1, etc.

    -- Input / Output
    context_snapshot        TEXT NOT NULL,  -- JSON: tot ce s-a trimis la LLM
    rationale_text          TEXT NOT NULL,

    -- Validare
    validation_flags        TEXT NOT NULL DEFAULT '[]',    -- JSON: ["V4a_price_hallucination", ...]
    hallucination_flags     TEXT NOT NULL DEFAULT '[]',    -- JSON: detalii per halucinație detectată
    retry_count             INTEGER NOT NULL DEFAULT 0,

    -- News cross-reference (populat imediat după generare)
    news_verified           BOOLEAN,
    news_verification_detail TEXT,   -- JSON: {cited, found_in_context, found_in_db, missing}

    -- Market metrics verification (populat la intrarea în trade + la outcome)
    metrics_at_entry        TEXT,    -- JSON: snapshot tehnic real la data intrării
    metrics_verification    TEXT,    -- JSON: ce a spus LLM vs ce era real
    metrics_accuracy_score  FLOAT,   -- 0-1: proporție claims corecte

    -- Outcome (populat când trade-ul se închide)
    outcome                 VARCHAR(24),
    outcome_at              TIMESTAMPTZ,
    closing_price           FLOAT,
    pnl_r                   FLOAT,

    -- Scoring final
    quality_score           INTEGER,    -- 0-10 (calculat la outcome)
    hallucination_rate      FLOAT,      -- 0-1 (raport flags / total claims detectate)

    created_at              TIMESTAMPTZ NOT NULL
);

CREATE INDEX ix_llm_rl_asset_direction    ON llm_rationale_logs(asset_id, direction, outcome);
CREATE INDEX ix_llm_rl_category_horizon   ON llm_rationale_logs(asset_category, horizon, outcome);
CREATE INDEX ix_llm_rl_provider           ON llm_rationale_logs(provider_id, model, outcome);
CREATE INDEX ix_llm_rl_created            ON llm_rationale_logs(created_at);
```

### 6.3 Componentele feedback loop-ului

#### Componenta 1: News Cross-Reference (la generare, `hallucination.py`)

```python
async def verify_news_references(
    rationale_text: str,
    context_headlines: list[str],
    session: AsyncSession,
    asset_id: str,
    hours_back: int = 72,
) -> dict:
    """
    1. Extrage referințe la știri din rationale (propoziții cu verbe de raportare:
       announced, reported, following, after, due to + substantiv)
    2. Verifică față de:
       a) headlines furnizate (context) — fuzzy match threshold 0.6
       b) feed_items din DB pentru asset_id, ultimele hours_back ore
    3. "found_in_db" (nu în context) = halucinație de context — LLM știa din antrenament
    4. "missing" (nicăieri) = halucinație pură
    """
```

#### Componenta 2: Metrics Snapshot la intrare în trade (inline la ActiveTrade create)

```python
async def capture_metrics_at_entry(recommendation_id: str, session: AsyncSession) -> dict:
    """
    Capturat INLINE la POST /active-trades (nu job separat — evită stale data).
    Returnează: {rsi, macd, macd_signal, adx, atr_pct, bb_position,
                 vpoc, vix, dxy_trend, spx_regime, captured_at}
    Salvat în llm_rationale_log.metrics_at_entry.
    """
```

#### Componenta 3: Metrics Verification la outcome (`hallucination.py`)

```python
async def verify_metrics_claims(
    rationale_text: str,
    metrics_at_entry: dict,
) -> dict:
    """
    Extrage afirmații numerice din rationale și compară cu realul:
    - "RSI [at/of/around/near] {N}" → compară cu metrics_at_entry["rsi"]
    - "ATR [of/at] {N}%" → compară cu metrics_at_entry["atr_pct"]
    - "MACD [positive/negative/above/below signal]" → compară cu macd vs macd_signal
    - "ADX [above/below/around] {N}" → compară cu metrics_at_entry["adx"]

    Returnează: {
        claims: [{claim, actual, delta_pct, is_hallucination}],
        accuracy_score: float,   -- 0-1
        hallucinated_indicators: list[str]
    }
    """
```

#### Componenta 4: Hallucination Profile Builder (săptămânal)

```python
async def build_hallucination_profile(session: AsyncSession, days_back: int = 30) -> dict:
    """
    Agregă halucinațiile per provider/model din ultimele days_back zile.

    Output per model:
    {
        "openai/gpt-4o-mini": {
            "total_rationales": 145,
            "price_hallucination_rate": 0.08,
            "indicator_hallucination_rate": 0.23,
            "news_hallucination_rate": 0.12,
            "most_hallucinated_indicators": ["rsi", "atr"],
            "temporal_accuracy": 0.78,
            "direction_conflict_rate": 0.04,
            "avg_metrics_accuracy_score": 0.71,
            "sample_size_note": "n=145, confidence: high"
        }
    }
    Salvat în data/hallucination_profile.json
    """
```

#### Componenta 5: Hallucination Guard (injectat în prompts)

```python
def build_hallucination_guard(profile: dict, provider_id: str, model: str) -> str:
    """
    Generează instrucțiuni anti-halucinație personalizate per model, pe baza profilului.

    Exemplu output pentru openai/gpt-4o-mini cu indicator_hallucination_rate=0.23:

    'ACCURACY REQUIREMENTS (based on 30-day validation data):
    - RSI values: 23% error rate detected on this model. Use ONLY the exact RSI value
      from the provided data. Do not round, estimate, or approximate.
    - ATR values: Elevated error rate. Use only what is explicitly stated.
    - News references: Only reference headlines explicitly provided above.
      Do not use news from training memory.
    - Price levels: Only use prices from context (entry, SL, TP, VPOC).
      Do not invent support/resistance levels.'
    """
```

#### Componenta 6: Few-shot cu exemple verificate (`few_shot.py`)

```python
async def get_verified_few_shot_examples(
    session: AsyncSession,
    asset_category: str,
    direction: str,
    horizon: str,
    limit_good: int = 1,
    limit_bad: int = 1,
) -> list[dict]:
    """
    Selectează exemple istorice cu:
    - outcome cunoscut (hit_tp / hit_sl)
    - metrics_accuracy_score > 0.8 (indicatorii citați erau reali)
    - zero V4a sau V4b flags (fără halucinații numerice)

    IMPORTANT: un exemplu "bun" cu halucinații tehnice NU este util —
    chiar dacă trade-ul a mers bine, raționamentul era bazat pe date false.

    Prioritate selecție:
    1. Același asset_id + direction + horizon → relevantă maxim
    2. Același asset_category + direction + horizon
    3. Orice asset cu outcome clar + direction + horizon

    Limită: max 90 zile vechime.
    Activare: doar dacă există >= 10 outcomes în DB (evită bootstrap problem).
    """
```

Format few-shot injectat în user message:

```
=== VERIFIED HISTORICAL EXAMPLES (for calibration) ===

[CORRECT PREDICTION — verified metrics, target hit in 9 days]
Horizon: near | Asset: MSFT stocks LONG | Outcome: +2.1R | Metrics accuracy: 0.91
RSI=42 (actual: 41.8 ✓), MACD histogram positive (verified ✓), VPOC=$385 (verified ✓)
Rationale used: "Microsoft exiting oversold territory with RSI at 42 and MACD histogram
turning positive. VPOC at $385 acts as strong support with price inside value area.
Main risk: tech rotation on yield spike."
Why it worked: All cited indicators accurate. VP support held as predicted.

[INCORRECT PREDICTION — hallucinated metrics, stop hit in 3 days]
Horizon: near | Asset: NVDA stocks LONG | Outcome: -1.0R | Metrics accuracy: 0.41
Validation flags: V4b (RSI cited: 71, actual: 58), V4c (earnings ref not in headlines)
What went wrong: LLM cited RSI=71 (was 58) and referenced earnings not provided.
Avoid: over-confident momentum framing without verifying indicator values.

=== END HISTORICAL EXAMPLES ===
```

### 6.4 Jobs în scheduler

| Job | Trigger | Scop |
|---|---|---|
| `job_capture_metrics_at_entry` | Inline la POST /active-trades | Capturează TechnicalSnapshot real |
| `job_verify_news_references` | Imediat după generate_rationale | Cross-ref news citate vs feed_items |
| `job_verify_outcomes` | La 6h | Copiază outcome din ActiveTrade, calculează metrics_accuracy, quality_score |
| `job_build_hallucination_profile` | Săptămânal (Duminică 06:00) | Agregă profilul per model, generează hallucination_guard |

### 6.5 Quality score — formula completă

```python
def calculate_quality_score(
    outcome: str,                    # hit_tp | hit_sl | expired
    validation_flags: list[str],
    metrics_accuracy_score: float,   # 0-1
    news_verified: bool | None,
    retry_count: int,
    pnl_r: float | None,
) -> int:
    score = 5  # baseline

    # Outcome (±3)
    if outcome == "hit_tp":   score += 3
    elif outcome == "hit_sl": score -= 3

    # Metrics accuracy (±2)
    if   metrics_accuracy_score >= 0.85: score += 2
    elif metrics_accuracy_score >= 0.65: score += 1
    elif metrics_accuracy_score <  0.50: score -= 2

    # News accuracy (±1)
    if news_verified is True:  score += 1
    elif news_verified is False: score -= 1

    # Validation flags
    for flag in validation_flags:
        if flag in ("V3_direction_conflict", "V4a_price_hallucination", "V4b_indicator_hallucination"):
            score -= 2
        elif flag in ("V4c_news_hallucination", "V4d_temporal_hallucination", "V5_too_generic"):
            score -= 1

    # Retry penalty
    score -= retry_count

    # P&L bonus pentru hit_tp cu R mare
    if outcome == "hit_tp" and pnl_r is not None and pnl_r >= 2.0:
        score += 1

    return max(0, min(10, score))
```

---

## 7. Plan: Îmbunătățiri prompts per orizont

### 7.1 `SYSTEM_PROMPT_NEAR` (≤ 14 trading days)

```python
SYSTEM_PROMPT_NEAR = """
You are a concise trading research assistant analyzing a SHORT-TERM trade
(horizon: {horizon_days} trading days, ~{calendar_days} calendar days).

In 2-4 sentences, explain why the SPECIFIC direction ({direction}) makes sense
FOR THIS TIMEFRAME and identify the most critical near-term risk.

Direction rules (mandatory):
- SHORT direction: explain why price will DECLINE in the next {horizon_days} days.
  Do NOT argue for a bullish position.
- LONG direction: explain why price will RISE in the next {horizon_days} days.
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

{hallucination_guard}
"""
```

### 7.2 `SYSTEM_PROMPT_FAR` (> 14 trading days)

```python
SYSTEM_PROMPT_FAR = """
You are a concise trading research assistant analyzing a MEDIUM-TERM trade
(horizon: {horizon_days} trading days, ~{calendar_weeks} calendar weeks).

In 2-4 sentences, explain why the SPECIFIC direction ({direction}) makes sense
STRUCTURALLY OVER THIS TIMEFRAME and identify the most critical medium-term risk.

Direction rules (mandatory):
- SHORT direction: explain structural reasons price will trend LOWER.
  Do NOT argue for a bullish thesis.
- LONG direction: explain structural reasons price will trend HIGHER.
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

{hallucination_guard}
"""
```

### 7.3 Context extins per orizont (`_format_user_content`)

**Câmpuri comune (near + far) — adăugate față de azi:**
```python
parts.append(f"Trade horizon: {horizon} ({horizon_days} trading days, ~{calendar_days} calendar days).")
parts.append(f"Engine confidence score: {confidence_score}/100.")
if sentiment_trend:
    parts.append(f"Sentiment trend (24h vs 24-72h window): {sentiment_trend}.")
if sentiment contradicts direction:
    parts.append(f"NOTE: Sentiment ({sentiment_avg:.3f}) CONTRADICTS direction. Technicals override.")
parts.append(f"Market regime: {market_regime}.")
```

**Câmpuri adiționale NEAR horizon:**
```python
# SL/TP vs Value Area
if vpoc and stop_loss and take_profit and val and vah:
    sl_in_va = val <= stop_loss <= vah
    tp_beyond_va = (tp > vah if long) or (tp < val if short)
    parts.append(f"SL vs Value Area: {'INSIDE (frequently tested)' if sl_in_va else 'outside (protected)'}.")
    parts.append(f"TP vs Value Area: {'beyond boundary (realistic)' if tp_beyond_va else 'within VA (low R/R zone)'}.")
# Macro events cu zile rămase
if upcoming_events:
    parts.append(f"Macro events in trade window: {' | '.join(events_str)}.")
# Headlines cu timestamp
for headline, age_hours in headlines_with_age[:3]:
    parts.append(f"Headline ({age_hours:.0f}h ago): {headline}")
```

**Câmpuri adiționale FAR horizon (în loc de VPOC):**
```python
# DXY pe termen lung
parts.append(f"DXY trend: {dxy_macro.trend} (5d: {ch5:+.1f}%, 20d: {ch20:+.1f}%).")
# Sentiment pe fereastră extinsă
if sentiment_5d_avg:
    parts.append(f"Sentiment 5-day average: {sentiment_5d_avg:.3f}.")
if sentiment_10d_avg:
    parts.append(f"Sentiment 10-day average: {sentiment_10d_avg:.3f}.")
# Fundamentale extinse
parts.append(f"P/E: {pe_ratio} | 52-week position: {position_52w}%.")
```

### 7.4 Grade rubric corectat pentru `_BACKTEST_INSIGHT_SYSTEM`

```
Grade rubric (strict, apply in order — first match wins):
F = avg_pnl_r < 0 AND win_rate < 35%  [losing strategy]
F = win_rate < 20%                     [random or broken]
D = win_rate 20-34%  OR  (win_rate < 40% AND max_drawdown_r < -4.0)
C = win_rate 35-44%  OR  (win_rate >= 35% AND sharpe < 0.3)
B = win_rate 45-54%  AND avg_pnl_r > 0  AND sharpe >= 0.3
A = win_rate >= 55%  AND avg_pnl_r > 0.15  AND sharpe >= 0.8

Mandatory downgrades (apply after initial grade):
- Downgrade one level if max_drawdown_r < -5.0
- Downgrade one level if sample_size < 10 trades
- Never assign A or B if avg_pnl_r <= 0

Mandatory upgrades:
- Upgrade one level if win_rate > 65% AND sharpe > 1.5

Report the initial grade and any adjustments in grade_reason.
```

---

## 8. Fișiere și migrări

### 8.1 Fișiere noi

| Fișier | Scop |
|---|---|
| `app/models/llm_rationale_log.py` | Model SQLAlchemy |
| `app/services/research/validator.py` | Reguli V1-V8 + retry logic |
| `app/services/research/hallucination.py` | Detecție V4a-V4d + metrics verification + news cross-ref |
| `app/services/research/few_shot.py` | Selecție exemple istorice verificate |
| `app/services/research/bias_detector.py` | Profil halucinații per model (weekly) |
| `migrations/versions/003_llm_rationale_log.py` | Migrare Alembic |

### 8.2 Fișiere modificate

| Fișier | Modificări |
|---|---|
| `app/services/research/llm.py` | SYSTEM_PROMPT_NEAR + SYSTEM_PROMPT_FAR, validator, few-shot, hallucination guard, `max_tokens` 200→350, fix THINKING_BUDGET hardcodat |
| `app/services/research/__init__.py` | `_format_user_content` extins per orizont, DXY/regime, headlines cu timestamp, sentiment trend |
| `app/services/technicals.py` | `estimate_expiry_days` max_days: 30→60 |
| `app/jobs/scheduler.py` | 4 joburi noi: capture_metrics, verify_outcomes, build_hallucination_profile, verify_news |
| `app/api/routes.py` | Endpoint read-only pentru llm_rationale_logs |
| `app/services/briefing.py` | LLM prompt pentru morning brief narativ |

---

## 9. Obiecții și trade-off-uri

### 9.1 „V4b este dificilă — LLM parafrazează, nu citează exact"

**Obiecție validă.** LLM spune "RSI approaching overbought" (RSI=68) — corect contextual, dar nu citează "72" inventat. Regex simplu nu distinge parafrazare legitimă de halucinație.

**Mitigare**: V4b verifică **numai** afirmații cu valoare numerică explicită: `"RSI at 72"`, `"RSI of 72"`, `"RSI ~72"`. Parafrazele calitative (`"RSI approaching overbought"`) sunt excluse. Toleranță ±15%.

### 9.2 „V4c (news cross-ref) are prea multe false positives"

**Obiecție validă.** LLM poate spune "following strong earnings momentum" fără să citeze o știre specifică.

**Mitigare**: V4c verifică numai citări directe cu verb de raportare + substantiv specific. Referințe la narrative generale sunt excluse. V4c este FLAG_ONLY — mai bine fals positive ocazional decât retry costisitor.

### 9.3 „Feedback loop necesită trade-uri reale cu outcome — bootstrap problem"

**Obiecție validă.** Fără outcomes, niciun few-shot, niciun profil de halucinații.

**Mitigare**:
- Activare gradată: 0-10 outcomes → sistem identic cu azi; 10-30 → few-shot parțial; 30+ → complet
- Se poate seed manual cu 5-10 exemple de calitate pentru bootstrap rapid
- Validatorul V1-V8 funcționează de la prima zi (fără date istorice)

### 9.4 „2 prompt-uri separate (NEAR/FAR) se vor diverge și vor fi greu de menținut"

**Obiecție parțial validă.** Regulile de bază se duplică.

**Mitigare**: structura cu `BASE_RULES` shared + secțiuni per orizont. Testare automată pytest cu fixtures near/far.

### 9.5 „Halucinația scade pur și simplu cu un model mai bun"

**Obiecție legitimă.** Claude Opus sau GPT-4o full au rate mult mai mici.

**Contra-argument**: Feedback loop-ul reduce halucinațiile pe ORICE model. Profilul de halucinații este un argument obiectiv pentru alegerea provider-ului — dacă anthropic are 8% vs openai 23%, datele vorbesc.

### 9.6 „Far horizon fără date fundamentale complete — sistemul nu are earnings trend, analyst revisions"

**Obiecție validă.** yfinance oferă P/E, 52-week range — dar nu earnings trend pe 4 trimestre sau analyst consensus.

**Mitigare**: Far horizon rationale este explicit mai structural și mai puțin specific numeric. LLM este instruit să recunoască incertitudinea mai mare. Ce nu avem nu se injectează → nu se halucinează.

### 9.7 „`metrics_at_entry` — dacă jobul rulează cu delay, datele sunt stale"

**Obiecție operațională validă.**

**Mitigare**: `capture_metrics_at_entry` rulează **inline** la POST `/active-trades`, nu ca job de background. Timestamp explicit în `captured_at` pentru trasabilitate.

### 9.8 „Prompts mai lungi (few-shot + guard) = cost API mai mare"

**Estimare**: +480 tokens per apel cu gpt-4o-mini ≈ +$0.000072/apel. 10 recomandări/zi → +$0.26/an. Neglijabil. Pentru Ollama (local): niciun cost, dar few-shot se dezactivează automat pentru modele cu context < 4K tokens.

### 9.9 „Sentiment pe 5-10 zile pentru far horizon nu există în DB azi"

**Obiecție validă.** Aggregarea curentă face window de 72h maxim.

**Mitigare**: Extinderea ferestrei de agregare este un task separat în `aggregate.py`. Far horizon prompts folosesc aceste câmpuri **opțional** — dacă `sentiment_5d_avg is None`, linia nu se adaugă în context. Nu blochează implementarea.

---

## 10. Priorități de implementare

### Ordinea recomandată

| P | Item | Status | Effort | Impact | Dependency |
|---|---|---|---|---|---|
| **1** | Model + migrare Alembic `llm_rationale_log` | ⬜ Pending | Mic | Critic | — |
| **2** | Validator V1-V3, V5, V7 (structural, fără halucinații) | ⬜ Pending | Mic-Mediu | Critic | — |
| **3** | `max_tokens` fix + THINKING_BUDGET hardcodat + ierarhie config | ✅ Done 2026-04-07 | Mic | Mediu | — |
| **4** | Horizon classification + `estimate_expiry_days` max→60 | ⬜ Pending | Mic | Mare | — |
| **5** | `_format_user_content` extins (horizon, confidence, sentiment_trend, regime, events) | ⬜ Pending | Mediu | Mare | P4 |
| **6** | `SYSTEM_PROMPT_NEAR` + `SYSTEM_PROMPT_FAR` | ⬜ Pending | Mediu | Mare | P4, P5 |
| **7** | Logging rationale în `llm_rationale_log` (fără feedback încă) | ⬜ Pending | Mic | Critic | P1 |
| **8** | `capture_metrics_at_entry` (inline la ActiveTrade create) | ⬜ Pending | Mic | Critic | P1, P7 |
| **9** | `job_verify_outcomes` (copiere outcome + quality score) | ⬜ Pending | Mic | Critic | P1, P7, P8 |
| **10** | Validator V4a–V4d (halucinații numerice + news) | ⬜ Pending | Mediu | Mare | P7 |
| **11** | `verify_news_references` (news cross-ref job) | ⬜ Pending | Mediu | Mediu | P1, P7 |
| **12** | `verify_metrics_claims` (post-outcome) | ⬜ Pending | Mediu | Mare | P8, P9 |
| **13** | Hallucination profile builder (weekly job) | ⬜ Pending | Mediu | Mare | P10, P12 |
| **14** | `hallucination_guard` injection în prompts | ⬜ Pending | Mic | Mare | P13 |
| **15** | Few-shot selection + injecție (cu verificare metrică) | ⬜ Pending | Mare | Mare | P7, P12, P13 |
| **16** | Grade rubric corectat în `_BACKTEST_INSIGHT_SYSTEM` | ⬜ Pending | Mic | Mediu | — |
| **17** | Sentiment window extinsă 5-10 zile pentru far horizon | ⬜ Pending | Mediu | Mediu | P4 |
| **18** | Daily briefing LLM prompt (morning brief narativ) | ⬜ Pending | Mediu | Mediu | — |

### Detalii P3 — implementat 2026-04-07

- `max_tokens` rationale: 200 → **400** (OpenAI, OpenRouter, Anthropic fără thinking)
- `_call_anthropic()`: parametru nou `output_buffer` — separă thinking budget de output budget per prompt
  - Rationale: `output_buffer=400`; Backtest insight: `output_buffer=1500`; Gap advice: `output_buffer=150`
- `generate_gap_advice`: `THINKING_BUDGET = 2000` hardcodat → citit din `cfg.get("thinking_budget")`
- `providers.py`: `max_thinking_budget` per provider (Anthropic: 32000, restul: 0); `clamp_thinking_budget()`
- `preferences.py`: `set_research_llm_config()` acceptă `thinking_budget`; `get_research_llm_config()` returnează `thinking_budget_max`
- `routes.py`: `PreferencesUpdate` + `PATCH /preferences` expun `research_llm_thinking_budget`
- Ierarhie completă: `preferences.json` > `.env` > `config.py default (0)`

### Ce NU implementăm (motivat)

- **Fine-tuning model**: necesită GPU, dataset labeled, MLOps — nejustificat
- **RLHF**: overhead operațional neproporsional cu dimensiunea proiectului
- **Embedding-uri + semantic search** pentru few-shot: SQL pe outcome + category este suficient
- **Streaming news real-time** (Twitter/X, Bloomberg Terminal): cost API ridicat, plan separat
- **Far horizon analiză fundamentală completă** (earnings trend, analyst revisions): necesită API premium, plan separat

---

## Appendix: Flux complet

```
[INPUT]
RSS feeds → VADER sentiment → aggregate (72h window) → MarketAnalysis
yfinance → TechnicalSnapshot (RSI, MACD, ATR, VPOC,...) → confidence_score
macro_calendar → upcoming_events
market_context → regime + DXY

          ↓
[HORIZON CLASSIFICATION]
estimate_expiry_days() → 1-60d → classify_horizon() → "near" | "far"

          ↓
[PROMPT BUILDING]
"near" → SYSTEM_PROMPT_NEAR + context_near (VP, events, sentiment 24-72h, SL/TP vs VA)
"far"  → SYSTEM_PROMPT_FAR  + context_far  (DXY 20d, sentiment 5-10d, fundamentals)
+ hallucination_guard (din hallucination_profile.json, per provider/model)
+ few_shot examples (din llm_rationale_logs cu outcome + metrics_accuracy > 0.8)

          ↓
[LLM CALL → provider dispatch]

          ↓
[VALIDATOR V1-V8]
blocking fail → retry (max 2) cu corrective instruction
flag-only → logat în validation_flags

          ↓
[POST-GENERATION]
verify_news_references() → news_verified + news_verification_detail
save → llm_rationale_logs (context_snapshot, rationale_text, flags, horizon)

          ↓
[USER TAKES TRADE]
ActiveTrade created → capture_metrics_at_entry() (inline) → saved to llm_rationale_log

          ↓
[TRADE CLOSES (every 6h job)]
ActiveTrade.outcome set → job_verify_outcomes():
  verify_metrics_claims(rationale, metrics_at_entry) → metrics_accuracy_score
  calculate_quality_score() → quality_score 0-10
  update llm_rationale_log

          ↓
[WEEKLY — Duminică 06:00]
job_build_hallucination_profile():
  agregare din llm_rationale_logs (30 zile)
  → hallucination_profile.json + hallucination_guard.json
  → injectat în prompts viitoare la generare
```

---

*Revizie 2 — 2026-04-07.*
*Schimbări față de rev 1: adăugat near/far horizon distinction (Secțiunea 2), restructurat feedback loop cu focus anti-halucinație (Secțiunea 6), adăugat detecție halucinații V4a-V4d, news cross-reference și metrics verification ca componente centrale, extins obiecțiile la 9 (adăugat 9.7, 9.8, 9.9), adăugat diagrama flux completă în Appendix.*
