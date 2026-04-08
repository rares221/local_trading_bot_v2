# Changelog — Local Trading Bot v2

> Format: data · branch · ce s-a schimbat și de ce

---

## 2026-04-07 — `improvements_trade_builder` (sesiunea 9)

### LLM — P16, P18 implementate și verificate

#### P16 — Grade rubric corectat în `_BACKTEST_INSIGHT_SYSTEM`

**Modificat: `app/services/research/llm.py`**
- `_BACKTEST_INSIGHT_SYSTEM` rescris cu rubric strict cantitativ (first-match wins):
  - **A**: win_rate ≥55% AND avg_pnl >0.3R AND sharpe >1.0 AND max_drawdown <4R
  - **B**: (win_rate ≥45% AND avg_pnl >0.2R AND max_drawdown <5R) OR (≥55% ratează exact un criteriu A)
  - **C**: win_rate 35–44% OR (≥45% AND avg_pnl 0–0.2R) OR max_drawdown 5–8R
  - **D**: win_rate 25–34% OR avg_pnl negativ > -0.2R
  - **F**: win_rate <25% OR avg_pnl ≤ -0.2R
  - Override: max_drawdown >8R forțează minim D indiferent de win_rate
- `{horizon_days}` placeholder injectat înainte de fiecare apel provider: `_system = _BACKTEST_INSIGHT_SYSTEM.replace("{horizon_days}", str(horizon_days))`

#### P18 — Daily briefing LLM prompt (morning brief narativ)

**Modificat: `app/services/research/llm.py`**
- `_BRIEF_NARRATIVE_SYSTEM`: prompt senior trading desk analyst — brief matinal 3–5 propoziții; structură: (1) macro environment VIX+SPX → risk regime, (2) implicații poziții + sizing, (3) biasul direcțional al zilei; numere specifice obligatorii, fără disclaimers
- `generate_brief_narrative(checklist_items, recs_count, recs_long, recs_short, active_trades_count, market_regime, dxy_trend, dxy_change_5d)`: user message construit din checklist + regime + DXY; max_tokens=250; toate cele 4 providere acoperite; returnează `str | None`

**Modificat: `app/services/briefing.py` — `run_briefing()`**
- Numără recs_today per direcție (`recs_long`, `recs_short`)
- Numără `active_trades_count` (ActiveTrade cu outcome IS NULL)
- Fetch market_regime și DXY via `asyncio.to_thread` (non-fatal)
- Apelează `generate_brief_narrative()` non-fatal; stochează în `payload["narrative"]`
- `content_text` nemodificat — backward compatible

**Modificat: `app/static/briefing.html`**
- CSS: `.briefing-narrative` (border-left albastru, background muted), `.briefing-narrative-label`
- `<div id="briefing-narrative" style="display:none;">` adăugat deasupra secțiunii de metrici
- `showBriefing()` actualizat: renderizează `data.payload.narrative` → `#briefing-narrative`; ascunde div-ul dacă narrative lipsește

---

## 2026-04-07 — `improvements_trade_builder` (sesiunea 8-b)

### LLM — P5, P6, P13, P14, P15, P17 implementate

#### P5 + P6 + P17 — Context enrichment + Horizon-aware prompts + Sentiment window extinsă

**Modificat: `app/services/research/__init__.py` — `build_research_context()`**
- `sentiment_trend`: citit din `analysis.payload["by_asset_sentiment"][asset]["sentiment_trend"]`
- `headline_ages: list[float]`: vârstă în ore per headline din `FeedItem.fetched_at`; headlines extinse de la 2 la 3
- `market_regime`: `detect_market_regime().regime` (`risk_on | risk_off | cautious`)
- `dxy_trend`, `dxy_change_5d`, `dxy_change_20d`: `detect_dxy_macro()`
- `upcoming_events`: `get_upcoming_events(days_ahead=expiry_days)` — FOMC/CPI/NFP în fereastra trade-ului
- `pe_ratio`: `yfinance info["trailingPE"]` pentru stocuri/ETF
- `position_52w`: calcul din `fiftyTwoWeekHigh/Low` + `buy_in_price`
- `sentiment_5d_avg`, `sentiment_10d_avg` (far horizon only): query FeedItem ultimele 10 zile; omis pe near pentru eficiență

**Modificat: `app/services/research/llm.py`**
- `SYSTEM_PROMPT` înlocuit cu `SYSTEM_PROMPT_NEAR` + `SYSTEM_PROMPT_FAR` format-strings
  - Near: tactic — direcție în `{horizon_days}` zile, ≥2 indicatori, macro events, SL/TP vs VA; interzis limbaj pe termen lung
  - Far: structural — trend EMA/SMA + ADX, context macro DXY/regime, incertitudine pe `{calendar_weeks}` săptămâni; interzis limbaj intraday
- `_format_user_content()` restructurat cu secțiuni Common / Near / Far
- `generate_rationale()`: selectează promptul bazat pe `context["horizon"]`; persistă `_prompt_version` în logs
- `snapshot_keys` extins cu: horizon, expiry_days, sentiment_trend, dxy_trend, dxy_change_5d/20d, headline_ages, sentiment_5d_avg/10d_avg

#### P13 — Hallucination profile builder (job săptămânal)

**Nou: `app/services/research/bias_detector.py`**
- `build_hallucination_profile(session, days_back=30)`: agregare async per `(provider_id, model)` — v4a_rate, v4b_rate, v4c_rate, v4d_rate, any_v4_rate, retry_rate; scriere atomică `.tmp → rename` în `data/hallucination_profile.json`

**Modificat: `app/jobs/scheduler.py`**
- `job_build_hallucination_profile()`: wrapper async; cron duminică 06:00

#### P14 — `hallucination_guard` injectat în prompts

**Modificat: `app/services/research/bias_detector.py`**
- `build_hallucination_guard(provider_id, model)`: citește profilul; returnează `""` dacă sample_size < 10 sau niciun flag rate ≥ 25%; altfel generează bloc de avertizare personalizat per indicator

**Modificat: `app/services/research/llm.py` — `generate_rationale()`**
- `build_hallucination_guard(provider_id, model)` apelat înainte de construirea system prompt
- Rezultatul înlocuiește `{hallucination_guard}` în `SYSTEM_PROMPT_NEAR.format()` și `SYSTEM_PROMPT_FAR.format()`

#### P15 — Few-shot selection + injecție

**Nou: `app/services/research/few_shot.py`**
- `get_verified_few_shot_examples(session, provider_id, model, horizon, limit=2)`: bootstrap guard (returnează `""` dacă total logs cu outcome < 10); selecție în 3 pași: exact match provider+model+horizon → same horizon → orice; filtre: outcome IS NOT NULL, metrics_accuracy_score > 0.8, fără V4a/V4b în flags; formatare bloc `--- VERIFIED EXAMPLES ---`

**Modificat: `app/services/research/llm.py` — `generate_rationale()`**
- `get_verified_few_shot_examples()` apelat async (sesiune proprie); bloc injectat la start de user message dacă non-empty

---

## 2026-04-07 — `improvements_trade_builder` (sesiunea 8)

#### Verificare cod (sesiunea 8)

Toate modificările din sesiunile 7–8 au fost verificate în cod înainte de a fi marcate ✅ Done în `LLM_IMPROVEMENT_PLAN.md`:

| Plan | Fișiere verificate | Status |
|---|---|---|
| P1 — model + migrare | `app/models/llm_rationale_log.py`, `migrations/versions/003_llm_rationale_log.py`, `app/models/__init__.py` | ✅ |
| P2 — validator V1–V3, V5, V7 | `app/services/research/validator.py` (toate regulile prezente + V7 wired în gap) | ✅ |
| P3 — max_tokens + thinking budget + ierarhie config | `providers.py` (max_thinking_budget, clamp), `preferences.py` (clamp + thinking_budget_max), `llm.py` (max_tokens=400, output_buffer parametrizat, fără hardcodări) | ✅ |
| P4 — horizon classification | `technicals.py` (max_days=60, classify_horizon), `research/__init__.py`, `scheduler.py`, `routes.py` | ✅ |
| P7 — logging rationale fire-and-forget | `llm.py` (_log_rationale_async + asyncio.create_task în ambele ramuri) | ✅ |
| P8 — capture metrics at entry | `llm.py` (_capture_metrics_at_entry_async), `research/__init__.py`, `routes.py` (mark_took_it) | ✅ |
| P9 — verify outcomes + quality score | `scheduler.py` (_verify_outcomes_for_logs + _calculate_quality_score) | ✅ |
| P10 — validator V4a–V4d + V8 | `validator.py` (reguli noi), `research/__init__.py` (câmpuri rsi/atr_pct/adx/macd_histogram) | ✅ |
| P11 — verify_news_references | `hallucination.py` (funcție nouă), `llm.py` (_verify_news_for_log în same task după log) | ✅ |
| P12 — verify_metrics_claims | `hallucination.py` (funcție nouă), `scheduler.py` (_verify_outcomes_for_logs, inline la outcome) | ✅ |

> ⚠️ **Rebuild necesar** pentru a aplica migrarea 003: `make build start`

#### P1 — LLMRationaleLog + Validator + Retry Loop (complet)

**Nou: `app/models/llm_rationale_log.py`**
- Model SQLAlchemy `LLMRationaleLog` cu 25+ coloane: id, created_at, recommendation_id/active_trade_id (FK SET NULL), asset_id, asset_category, direction, horizon, horizon_days, provider_id, model, prompt_version, context_snapshot (JSON), rationale_text, validation_flags (JSON), retry_count
- Coloane outcome feedback: news_verified, metrics_at_entry, metrics_verification, metrics_accuracy_score, outcome, outcome_at, closing_price, pnl_r, quality_score, hallucination_rate
- 4 indecși compuși: asset+direction+outcome, category+horizon+outcome, provider+model+outcome, created_at
- Helper methods: `get_validation_flags()`, `set_validation_flags()`, `get_context_snapshot()`, `get_metrics_at_entry()`

**Nou: `migrations/versions/003_llm_rationale_log.py`**
- Migrare Alembic revision="003" → creează tabelul `llm_rationale_logs` cu toate coloanele, FK-uri, server_defaults și 6 indecși

**Nou: `app/services/research/validator.py`**
- `ValidationFlag` + `ValidationResult` dataclasses
- V1: length check (< 50 chars) → blocking retry
- V2: truncation heuristic (text > 80 chars, nu se termină cu `.!?"'`) → blocking retry
- V3: direction alignment per-propoziție, cu detecție concession markers ("despite", "although") → blocking retry
- V5: specificity score (< 2 termeni tehnici din set de ~25) → blocking retry
- V7: gap advice word count > 30 → blocking retry
- `validate_rationale(text, context, prompt_type)` — API public; `prompt_type="gap"` activează V7

**Modificat: `app/services/research/llm.py`**
- `generate_rationale()` refăcut cu retry loop (MAX_RETRIES=2, 3 încercări total)
- Fiecare răspuns LLM trece prin `validate_rationale()` înainte de a fi returnat
- La eșec validare: injectează `_corrective_instruction` în context și reîncearcă
- `_log_rationale_async()`: fire-and-forget (asyncio.create_task) care persistă `LLMRationaleLog` în propria sesiune DB; loghează la passed sau după toate retry-urile
- Context snapshot curatizat (exclude OHLCV bulk) — stochează: asset, direction, sentiment, price, levels, technicals, VP fields

**Modificat: `app/models/__init__.py`**
- Import + export `LLMRationaleLog`

#### P11 — `verify_news_references` (cross-ref news imediat după generare)

**Nou: `app/services/research/hallucination.py`** — modul nou

`verify_news_references(rationale_text, context_headlines, session, asset_id, hours_back=72)`:
- Extrage propoziții cu verbe de raportare (`announced/reported/said/following/after X said`)
- Verifică față de context headlines (fuzzy threshold 0.35) și față de `feed_items` din DB (72h, threshold 0.40)
- Clasificare per citare: `verified` / `found_in_db_only` (context leak) / `hallucinated` (invenție pură)
- Returnează: `verified` (bool), `citations_found`, `missing_from_context`, `found_in_db_only`, `pure_hallucinations`, `detail` (per-sentence)

**Modificat: `app/services/research/llm.py`**
- `_log_rationale_async` returnează acum `log_id` intern după `session.refresh(entry)`
- Funcție nouă `_verify_news_for_log(log_id, rationale_text, headlines, asset_id)` — deschide sesiune proprie, rulează `verify_news_references`, persistă `news_verified` + `news_verification_detail` (JSON)
- `_log_rationale_async` apelează `_verify_news_for_log` în același task, după commit — fără impact pe latența generate_rationale

#### P12 — `verify_metrics_claims` (post-outcome, inline la job_update_active_trades)

**Nou în `app/services/research/hallucination.py`**

`verify_metrics_claims(rationale_text, metrics_at_entry)`:
- Pattern-uri dedicate: `_RSI_CLAIM`, `_ADX_CLAIM`, `_ATR_CLAIM`, `_MACD_BULL`/`_MACD_BEAR`
- Compară valori citate cu actuale din `metrics_at_entry` (±15% toleranță)
- MACD: verificare direcție calitativă (bullish/bearish vs histogram pozitiv/negativ)
- Returnează: `claims` (per indicator: cited, actual, delta_pct, is_hallucination), `accuracy_score` (0-1), `hallucinated_indicators`
- No-op dacă `metrics_at_entry` e gol sau nu există claims detectabile

**Modificat: `app/jobs/scheduler.py` — `_verify_outcomes_for_logs`**
- Rulează `verify_metrics_claims` inline la outcome (dacă `metrics_at_entry` e populat și `metrics_accuracy_score` e NULL)
- Persistă `metrics_accuracy_score` și `metrics_verification` (JSON detaliat)
- `quality_score` calculat cu `metrics_accuracy_score` real (nu mai e None ca în P9)

**Fix post-verificare: `app/services/research/llm.py` — `_capture_metrics_at_entry_async`**
- Bug: `atr_pct` nu era stocat în `metrics_at_entry` (se stoca doar `atr` absolut)
- Fix: adăugat `metrics["atr_pct"] = round(snap.atr_pct * 100, 2)` — P12 caută `atr_pct` (ca %) în `metrics_at_entry`, fără fix ATR claims erau întotdeauna skipped

#### P10 — Validator V4a–V4d + V8

**Modificat: `app/services/research/validator.py`** — rescris complet

- `BLOCKING_RULES` extins: `{"V1","V2","V3","V4a","V4b","V5","V7","V8"}`
- `FLAG_ONLY_RULES = {"V4c","V4d"}` (nu blochează retry, doar loghează)
- Helper `_within_tolerance(actual, cited, tol=0.15)` — comparație relativă ±15%
- Helper `_fuzzy_match(text, candidates, threshold=0.35)` — `difflib.SequenceMatcher`, fără dependențe extra

**V4a — price hallucination** (blocking)
- Regex `_PRICE_CONTEXT`: extrage numere din context de preț (support/resistance/target/entry/$)
- Ignoră valori < 5% din prețul minim cunoscut (evită false positives pe RSI/ADX/%)
- Fail: număr citat > 15% față de TOATE prețurile din context (buy_in, SL, TP, VPOC, VAH, VAL)

**V4b — indicator value hallucination** (blocking)
- Regex-uri separate: `_RSI_PATTERN`, `_ADX_PATTERN`, `_ATR_PATTERN`
- Verifică doar dacă context conține valoarea actuală (`rsi`, `adx`, `atr_pct`)
- Fail: valoare citată > 15% față de actual; retry message include valoarea corectă explicit

**V4c — news hallucination** (flag-only)
- Regex `_NEWS_REPORTING`: propoziții cu verbe de raportare (announced/reported/said/following)
- Fuzzy match față de `context["headlines"]` (threshold 0.35 — lax, flag-only)
- No-op dacă nu există headlines în context

**V4d — temporal hallucination** (flag-only)
- Regex `_TEMPORAL_RECENT`: yesterday/last week/this morning/just announced/hours ago
- Cross-ref cu `context["headline_ages"]` (introdus în sesiunea curentă)
- Threshold: "yesterday/this morning" → min headline < 48h; "last week" → < 168h

**V8 — horizon consistency** (blocking)
- Near (≤14d): blochează `long-term / over the months / structural / fundamental`
- Far (>14d): blochează `this week / by Friday / intraday / today's session`
- Retry message include `horizon_days` din context

**Modificat: `app/services/research/__init__.py`**
- Adăugat în context din snapshot: `rsi`, `atr_pct` (ca %, ex. 2.3), `adx`, `macd_histogram` — folosite de V4b

#### P8 — `capture_metrics_at_entry` (fire-and-forget la took-it)

**Nou: `_capture_metrics_at_entry_async()` în `app/services/research/llm.py`**
- Capturează `TechnicalSnapshot` live via `compute_sl_tp_for_symbol` (async via `asyncio.to_thread`)
- Salvează în `metrics_at_entry` (JSON): price, RSI, MACD histogram, ATR, ADX, VPOC, VAH, VAL, vp_bias, sentiment_avg
- Setează `active_trade_id` pe toate `LLMRationaleLog` cu `recommendation_id` potrivit și `metrics_at_entry IS NULL`

**Modificat: `app/services/research/__init__.py`**
- Expune `capture_metrics_at_entry()` wrapper în `__all__`

**Modificat: `app/api/routes.py` — `mark_took_it`**
- Reține `first_active_id` după `db.flush()`
- `asyncio.create_task(capture_metrics_at_entry(...))` după `db.commit()` — nu blochează răspunsul

#### P9 — `_verify_outcomes_for_logs` (job periodic la finalul update_trades)

**Nou: `_calculate_quality_score()` în `app/jobs/scheduler.py`**
- Formula din plan: baseline 5 ±outcome (±3) ±metrics_accuracy ±news_verified ±flags −retry_count +pnl_r bonus
- Degradeaza graceful când `metrics_accuracy_score` și `news_verified` sunt `None` (P10/P11 le completează ulterior)

**Nou: `_verify_outcomes_for_logs(session)` în `app/jobs/scheduler.py`**
- JOIN `active_trades` → `recommendations` → filtrare `llm_rationale_logs` unde `outcome IS NULL`
- Copiază: `outcome`, `outcome_at`, `closing_price`
- Calculează `pnl_r`: `(closing_price − entry) / risk` (ajustat long/short)
- Calculează și setează `quality_score`
- Actualizează toate log-urile cu `recommendation_id` potrivit și `outcome IS NULL`

**Modificat: `app/jobs/scheduler.py` — `job_update_active_trades`**
- Apel `await _verify_outcomes_for_logs(session)` + `await session.commit()` după commit-ul principal

#### P4 — Horizon classification + estimate_expiry_days max→60

**Modificat: `app/services/technicals.py`**
- `estimate_expiry_days()`: parametru `max_days` schimbat din 30 → **60** — suportă acum far horizon (>14 zile)
- Funcție nouă: `classify_horizon(expiry_days: int) -> str` — returnează `"near"` (≤14) sau `"far"` (>14)

**Modificat: `app/services/research/__init__.py`**
- `build_research_context()`: dacă recomandarea are `expiry_date`, calculează `expiry_days` și `horizon` și le adaugă în ctx
- Dacă SL/TP sunt calculate fresh din snapshot, apelează `estimate_expiry_days()` + `classify_horizon()` pentru același efect

**Modificat: `app/jobs/scheduler.py`**
- Batch loop: adaugă `expiry_days` + `horizon` din `c.expiry_date` în ctx (dacă nu există deja din `build_research_context`)

**Modificat: `app/api/routes.py`**
- `trade_builder_from_asset`: adaugă `expiry_days` + `horizon` din `cand.expiry_date` în ctx

---

## 2026-04-07 — `improvements_trade_builder` (sesiunea 7)

### LLM — Analiză completă + fix-uri thinking budget + plan îmbunătățiri

#### Analiză și planificare

**Nou: `docs/LLM_IMPROVEMENT_PLAN.md`** (rev 2)
- Analiză completă a celor 5 prompts existente din perspectivă developer senior + trader profesionist
- Gap-uri identificate: orizont de predicție absent, sentiment fără trend/context, confidence score netrimis la LLM, macro context gol, Volume Profile fără context de execuție, backtest grade rubric slab calibrat, lipsă streaming news, daily briefing fără LLM
- Erori de logică: `max_tokens=200` trunchiază, `THINKING_BUDGET` hardcodat, zero validare rationale, context dict fără schemă, halucinații numerice și de news nedetectate, lipsă versionare prompts
- **Distincție near/far horizon**: near ≤ 14 trading days (tehnice + news + events iminente), far > 14 zile (trend structural + fundamentale + macro cycle)
- **Plan validator V1–V8**: length, truncation, direction alignment, halucinații de preț/indicatori/news/temporale, specificity score, grade consistency, horizon consistency
- **Plan feedback loop anti-halucinație**: tabel `llm_rationale_log`, news cross-reference vs `feed_items`, metrics snapshot la entry, metrics verification la outcome, hallucination profile per model (weekly), hallucination guard injectat în prompts viitoare, few-shot cu exemple verificate
- 9 obiecții documentate cu mitigări: cost API few-shot, false positives V4b/V4c, bootstrap problem, divergență prompts NEAR/FAR, model mai bun vs feedback loop, far horizon fără fundamentale complete, delay metrics capture, prompts mai lungi, sentiment window extinsă inexistentă

#### Fix-uri implementate

**Modificat: `app/services/research/llm.py`**
- `THINKING_BUDGET = 2000` hardcodat în `generate_gap_advice` → eliminat; citește acum `thinking_budget = int(cfg.get("thinking_budget") or 0)` din config (identic cu toate celelalte funcții)
- `_call_anthropic()`: adăugat parametru `output_buffer: int = 1024` — separă explicit bugetul de thinking de tokens-ii rezervați pentru răspuns; formula: `max_tokens = thinking_budget + output_buffer`
- `max_tokens` rationale: 200 → **400** pentru OpenAI, Anthropic (output_buffer=400), OpenRouter; previne trunchierile frecvente pe modele verbose
- `generate_backtest_insight`: output buffer ridicat la **1500** (JSON complex poate depăși 1024 tokens)
- `generate_gap_advice`: output buffer **150** tokens (25 cuvinte + marjă), `max_tokens` non-thinking: 80 → 100

**Modificat: `app/services/research/providers.py`**
- `ProviderDef` dataclass: câmp nou `max_thinking_budget: int = 0`
- Anthropic: `max_thinking_budget=32000` (claude-opus-4-6/sonnet-4-6 suportă extended thinking până la 32K tokens)
- OpenAI / Ollama / OpenRouter: `max_thinking_budget=0` (nu suportă thinking tokens)
- Funcție nouă: `get_max_thinking_budget(provider_id)` — returnează plafonul provider-ului
- Funcție nouă: `clamp_thinking_budget(provider_id, requested)` — capsează automat un budget configurat la maximul provider-ului; returnează 0 dacă provider-ul nu suportă thinking

**Modificat: `app/core/preferences.py`**
- `get_research_llm_config()`: aplică `clamp_thinking_budget()` după citirea valorii — budgetele peste maximul provider-ului sunt silențios capsate
- `get_research_llm_config()`: returnează câmp nou `thinking_budget_max` — plafonul provider-ului curent; folosit pentru validare UI (slider max)
- `set_research_llm_config()`: parametru nou `thinking_budget: int | None` — permite salvarea budgetului în `preferences.json`; aplică `clamp_thinking_budget()` înainte de persistare

**Modificat: `app/api/routes.py`**
- `PreferencesUpdate`: câmp nou `research_llm_thinking_budget: int | None` (0 = disabled, max depinde de provider)
- `PATCH /preferences`: pasează `thinking_budget` la `set_research_llm_config()` când e specificat

#### Ierarhia de prioritate pentru `thinking_budget` (de la maxim la minim)

```
preferences.json["research_llm_thinking_budget"]   ← PATCH /preferences sau editare manuală
    ↓ fallback dacă absent
.env RESEARCH_LLM_THINKING_BUDGET=16000             ← override la restart container
    ↓ fallback dacă .env nu setează
config.py default: research_llm_thinking_budget = 0 ← thinking dezactivat
```

Bugetul configurat este capsat automat la `max_thinking_budget` al provider-ului (32000 pentru Anthropic, 0 pentru restul).

---

## 2026-04-07 — `improvements_trade_builder` (sesiunea 6)

### Analiza impact PRIORITATE 7 și 8 — decizie pendintă

**Modificat: `docs/IMPROVEMENT_PLAN.md`**
- PRIORITATE 7 (News latență < 15 minute): adăugată analiză de impact per zonă — valoarea reală e în research on-demand, nu în batch; zero impact pe backtesting; cost recurent obligatoriu (~$29/lună); marcată `⚠️ Decizie pendintă`
- PRIORITATE 8 (Corelație dinamică): adăugată analiză de impact per zonă — valoarea reală e doar în filtrul de batch (mai puține semnale corelate în stress); zero impact pe research și backtesting actual; imbunatatire marginala fata de VP si RS deja implementate; marcată `⚠️ Decizie pendintă`
- Ambele rămân nedecise până la validarea prin backtesting că VP (PRIORITATE 5) și RS (PRIORITATE 6) adaugă valoare reală

---

## 2026-04-07 — `improvements_trade_builder` (sesiunea 5)

### Relative Strength vs Benchmark (PRIORITATE 6 — complet)

**Nou: `compute_relative_strength()` în `app/services/technicals.py`**
- Calculează RS ratio `(1 + ret_asset) / (1 + ret_benchmark)` pe N zile trading
- >1.0 = asset outperformează benchmark; <1.0 = underperformează
- Reutilizează `fetch_ohlcv()` cu cache existent — zero API calls extra
- Walk-forward automat: respectă `_backtest_cutoff` prin `_apply_backtest_cutoff()`
- Output clampat `[0.1, 10.0]` pentru date sparse/proaste; returnează `None` dacă bare insuficiente

**Nou: `SECTOR_ETF_DIRECT` + `get_sector_etf_for_asset()` în `app/services/market_context.py`**
- Dict direct asset_id → sector ETF Yahoo symbol (30+ mappings: AAPL→XLK, XOM→XLE, JPM→XLF, JNJ→XLV etc.)
- Fallback prin `get_sector_for_asset()` → `SECTOR_ETFS` pentru assets nemapați direct

**Modificat: `app/services/recommendations/engine.py`**
- `Candidate` dataclass: câmpuri noi `rs_20d`, `rs_60d`, `sector_rs`
- Import nou: `compute_relative_strength`, `get_sector_etf_for_asset`
- `_build_candidate()`: calculează RS doar pentru `category == "stocks"`; confidence direction-aware:
  - RS > 1.2 + long → +5; RS > 1.2 + short → −5
  - RS < 0.8 + long → −8; RS < 0.8 + short → +5
- `sector_aligned` extins: revocat dacă `sector_rs < 0.9` (stock slab față de sector), acordat dacă `sector_rs > 1.15` (stock puternic chiar dacă sectorul e neutru)
- Constructor `Candidate(...)`: populat cu `rs_20d`, `rs_60d`, `sector_rs`

**Modificat: `app/jobs/scheduler.py`**
- Dict nou `_rs_by_symbol: dict[str, dict]` la nivel de modul
- `job_daily_batch()`: populează `_rs_by_symbol[sym]` din câmpurile RS ale fiecărui candidat

**Modificat: `app/api/routes.py`**
- `_rec_to_item()`: importă `_rs_by_symbol`, expune `rs_20d`, `rs_60d`, `sector_rs` în JSON per recomandare

**Modificat: `app/services/backtesting/replay.py`**
- Import nou: `compute_relative_strength`
- `run_backtest()`: pre-fetch garantat pentru `^GSPC` și sector ETFs (XLK/XLE/XLF/XLV/XLY/XLI/XLB)
- `_build_signal()`: calculează `rs_20d` walk-forward pentru stocks; aplică aceleași ajustări confidence ca engine-ul live; adaugă `rs_20d` în signal dict

**Modificat: `app/services/backtesting/metrics.py`**
- `compute_metrics()` returnează `relative_strength`: stats RS strong (>1.2) / neutral (0.8–1.2) / weak (<0.8) / no_data + `pct_strong`, `pct_weak`

**Modificat: `app/static/recommendations.html`**
- CSS nou: `.badge--rs-strong`, `.badge--rs-weak`, `#rec-rs-tooltip` (fixed global)
- IIFE tooltip global cu 3s delay, `initRsBadgeTooltips()` apelat la `renderComplete`
- `rsBadge(rec)`: badge `RS+` (verde) dacă `rs_20d > 1.2`, `RS−` (roșu) dacă `rs_20d < 0.8`
- Tooltip: explică cât a deviat față de SPX, dacă confirmă sau contrazice direcția, și impactul asupra confidence
- Asset column formatter: badge RS adăugat după VP badge

**Modificat: `app/services/research/__init__.py`**
- `build_research_context()`: calculează `rs_20d`, `rs_60d`, `sector_rs` pentru stocks după `compute_sl_tp_for_symbol`; adaugă în `ctx`

**Modificat: `app/services/research/llm.py`**
- `_format_user_content()`: include linie RS în promptul LLM — ratio 20d/60d vs SPX, ratio vs sector ETF, label outperforming/underperforming

**Modificat: `app/static/research.html`**
- CSS nou: `.rs-rs-strip`, `.rs-rs-badge-strong/weak/neut`
- `renderRsStrip(ctx)`: strip RS în panoul "Market Context" cu badge STRONG/WEAK/NEUTRAL, valori 20d/60d/sector, tooltip 3s (reutilizează `rs-vp-tooltip-anchor` + `initRsTooltips()`)
- `renderContext()`: inserează RS strip înaintea VP strip

---

## 2026-04-07 — `improvements_trade_builder` (sesiunea 4)

### Volume Profile — VPOC, VAH, VAL, HVN/LVN (PRIORITATE 5 — complet)

**Nou: `compute_volume_profile()` în `app/services/technicals.py`**
- Distribuie volumul zilnic uniform pe range-ul High–Low în 120 de bin-uri (lookback 60 zile)
- VPOC = bin-ul cu volumul maxim (pretul de echilibru al pietei)
- Value Area (VAH/VAL): expansiune de la VPOC pana la 70% din volumul total
- HVN: bin-uri > percentila 65 (clustere de volum — magnet de pret); LVN: bin-uri < percentila 25 (goluri — miscare rapida)
- `_cluster()`: mergeaza niveluri aflate la < 0.5% distanta intre ele

**Modificat: `TechnicalSnapshot` în `app/services/technicals.py`**
- Câmpuri noi: `vpoc`, `vah`, `val`, `hvn_levels`, `lvn_levels`, `vp_bias`, `vp_overextension`
- Metode noi: `in_value_area()`, `nearest_hvn_below()`, `nearest_hvn_above()`, `nearest_lvn_below()`, `nearest_lvn_above()`
- `build_snapshot()` apeleaza `compute_volume_profile(df)` si populeaza toate campurile VP

**Modificat: `compute_atr_levels()` în `app/services/technicals.py`**
- VP prioritar fata de swing S/R pentru plasarea SL si TP
- Long SL: `nearest_hvn_below()` → `snap.val` → 0.2% sub nivel; fallback la swing S/R
- Long TP: capsat la HVN deasupra pretului sau la VAH daca e in cale (`vah * 0.998`)
- Short: logica simetrica cu HVN/VAL

**Modificat: `app/services/recommendations/engine.py`**
- `Candidate` dataclass: câmpuri noi `vpoc`, `vah`, `val`, `vp_bias`, `vp_overextension`, `in_value_area`
- `_build_candidate()`: ajustari confidence VP — `in_value_area` −8, near LVN +5, overextension >2× −10, >1.3× −5, VP confirma directia +4
- Return: câmpuri VP populati din `snap`

**Modificat: `app/services/backtesting/replay.py`**
- `_build_signal()`: aceleasi ajustari confidence VP ca engine-ul live (walk-forward corect automat)
- Signal dict include: `vpoc`, `vah`, `val`, `vp_bias`, `vp_flag`, `in_value_area`

**Modificat: `app/services/backtesting/metrics.py`**
- `compute_metrics()` returneaza `volume_profile`: `in_value_area` / `outside_value_area` stats (total, win_rate, avg_pnl_r) + `pct_signals_in_va`

**Modificat: `app/jobs/scheduler.py`**
- Dict nou `_vp_by_symbol: dict[str, dict]` la nivel de modul
- `job_update_active_trades()` — populeaza `_vp_by_symbol[symbol]` din `snap` deja fetched
- `job_daily_batch()` — populeaza `_vp_by_symbol` din câmpurile VP ale fiecarui candidat

**Modificat: `app/api/routes.py`**
- `/api/active-trades` — câmpuri noi per trade: `vpoc`, `vah`, `val`, `vp_bias`, `vp_overextension`, `in_value_area`, `vp_distance_pct`
- `/api/recommendations` — câmpuri noi per recomandare: `vpoc`, `vah`, `val`, `vp_bias`, `vp_overextension`, `in_value_area`

**Modificat: `app/static/active-trades.html`**
- `vpStrip(t)`: bara VP cu VPOC, bias (↑/↓ fair value), VA range, distanta % vs VPOC, badge IN VA, indicator overextension
- Tooltip global fix (3s hover delay): explica VPOC, Value Area, fair value, dist% unui newbie
- CSS: `.vp-strip`, `.vp-badge-va`, `.vp-dist-pos/.neg`

**Modificat: `app/static/recommendations.html`**
- `vpBadge(rec)`: badge `VA ZONE` (gri) daca `in_value_area`; badge `VP EXT N×` (mov) daca overextension > 1.3×
- Coloana Asset: badge VP afisat langa gap badge
- Coloana Size: VPOC price + bias ca subtext sub pozitionare

**Modificat: `app/services/research/__init__.py`**
- `build_research_context()`: extrage `vpoc`, `vah`, `val`, `vp_bias`, `vp_overextension`, `in_value_area` din `snap` returnat de `compute_sl_tp_for_symbol`

**Modificat: `app/services/research/llm.py`**
- `_format_user_content()`: include linie VP in promptul LLM — VPOC, bias, value area range, in_value_area, overextension
- Modelul refera VP in rationale-ul generat

**Modificat: `app/static/research.html`**
- `renderVpStrip(ctx)`: bara VP in panoul "Market Context" (aceeasi informatie ca active-trades)
- Tooltip global fix (3s hover delay, independent de gap tooltip)
- `renderContext()`: insereaza VP strip + apeleaza `initRsTooltips()` dupa render
- CSS: `.rs-vp-strip`, `.rs-vp-badge-va`, `#rs-tooltip-global`

---

## 2026-04-07 — `improvements_trade_builder`

### Earnings Calendar (PRIORITATE 2)

**Nou: `app/services/earnings_calendar.py`**
- Fetch data de earnings per ticker via `yfinance.Ticker.calendar`
- Cache în memorie cu TTL 24h — nu bate Yahoo la fiecare batch
- `get_next_earnings(symbol)` → `Optional[date]`
- `is_near_earnings(symbol, window_days)` → `(bool, Optional[int])`

**Modificat: `app/services/recommendations/engine.py`**
- `Candidate` dataclass: câmpuri noi `near_earnings: bool`, `days_to_earnings: Optional[int]`
- `_build_candidate()`: penalizare `-15` confidence pentru stocks în fereastra ±2 zile de earnings
- Dacă `earnings_skip_enabled=True`: candidatul e eliminat complet din batch (skip)
- Note în `market_regime_note`: `"earnings ±Nd"`

**Modificat: `app/core/preferences.py`**
- `get_earnings_calendar_config()` / `set_earnings_calendar_config()`
- Chei noi: `earnings_window_days` (default 2), `earnings_confidence_penalty` (default 15), `earnings_skip_enabled` (default False)

**Modificat: `app/api/routes.py`**
- `/api/recommendations` — câmpuri noi `near_earnings`, `days_to_earnings` per recomandare
- `/api/active-trades` — câmpuri noi `near_earnings`, `days_to_earnings` per trade activ (stocks only, folosind cache-ul existent)

**Modificat: `app/static/recommendations.html`**
- Badge auriu `EARNINGS ±Nd` pe simbolul acțiunii în tabelul de recomandări

**Modificat: `app/static/active-trades.html`**
- Badge pulsant roșu `EARNINGS +Nd` în header-ul cardului de trade
- Strip de avertizare sub header cu mesaj explicit de gap risk când `near_earnings=true`
- CSS: `.badge--earnings-warn` (animație pulse), `.earnings-strip`

---

### Circuit Breaker (PRIORITATE 3)

**Nou: `app/services/circuit_breaker.py`**
- `CircuitBreakerState` dataclass: `mode`, `rolling_loss_r`, `consecutive_losses`, `drawdown_r`, `triggered_by`
- `compute_state(db_trades, ...)` — evaluează 3 gate-uri independente:
  1. **rolling_loss_gate** — R-multipli pierduți în ultimele 24h (nu calendar day)
  2. **consecutive_gate** — streak de `hit_sl`; `expired`/`manual` sunt neutre; doar `hit_tp` resetează
  3. **drawdown_gate** — peak-to-trough pe curba cumulativă R (tot istoricul)
- Moduri: `normal` / `defensive` (conf -10, sizing ×0.7) / `paused` (batch gol)

**Modificat: `app/services/recommendations/engine.py`**
- `build_daily_candidates()` — parametru nou `closed_trades: Optional[list]`
- Dacă CB e `paused` → returnează `[]` imediat
- Dacă CB e `defensive` → aplică penalizare confidence și sizing după ce batch-ul e construit

**Modificat: `app/jobs/scheduler.py`**
- `job_run_batch()` — fetchează closed trades și le pasează la `build_daily_candidates(closed_trades=...)`
- `job_update_active_trades()` — evaluează CB la fiecare run (2h); loghează și notifică dacă mode != normal

**Modificat: `app/core/preferences.py`**
- `get_circuit_breaker_config()` / `set_circuit_breaker_config()`
- Chei noi: `circuit_breaker_enabled` (default True), `max_consecutive_losses` (default 5), `max_daily_loss_r` (default 3.0), `drawdown_gate_r` (default 8.0)

**Modificat: `app/api/routes.py`**
- `GET /api/circuit-breaker` — endpoint nou: mode curent, stats, config, triggered_by

**Modificat: `app/static/recommendations.html`**
- Banner `⛔ PAUSED` (roșu) sau `⚠️ DEFENSIVE` (galben) deasupra tabelului când CB e activ
- CSS: `.alert--cb-defensive`, `.alert--cb-paused`

---

### Fix-uri circuit breaker (sesiunea 2 — 2026-04-07)

**Bug fix: `app/services/circuit_breaker.py`**
- `expired` și `manual` nu mai rup streak-ul de pierderi consecutive — sunt tratate ca neutre
- Înainte: orice outcome non-`hit_sl` reseta streak-ul → un trade expirat masca o serie de pierderi
- Acum: doar `hit_tp` (victorie confirmată) resetează streak-ul

**Bug fix: rolling 24h window în loc de calendar day**
- Înainte: `oa_date == today` → pierderile de la 23:55 dispăreau la miezul nopții
- Acum: `outcome_at >= now - 24h` → fereastra glisantă corectă

**Nou: CB evaluat la fiecare update de active trades (2h)**
- Înainte: CB era evaluat doar la generarea batch-ului zilnic
- Acum: `job_update_active_trades()` calculează starea CB și loghează/notifică dacă e activ

**Nou: earnings warning pe active trades**
- `/api/active-trades` returnează `near_earnings` și `days_to_earnings` pentru fiecare trade
- UI: badge pulsant + strip de avertizare cu mesaj de gap risk explicit

---

### Gap Risk — Active Trades + Backtesting (PRIORITATE 4 — sesiunea 3 — 2026-04-07)

**Bug fix: `app/services/backtesting/simulator.py` — gap slippage realist**
- Înainte: la `sl_hit` fără `tp_hit`, `closing = min(bar_low, stop_loss)` → folosea bar_low (worst case, prea pesimist)
- Acum: dacă `bar_open <= stop_loss` (gap down la deschidere) → `closing = bar_open` (fill realist la open)
- Dacă `bar_open > stop_loss` (stop atins intraday) → `closing = stop_loss` (fill normal)
- Același fix pentru short: `bar_open >= stop_loss` → `closing = bar_open`
- Rezultat: P&L în R-multipli mai realist; nu mai penalizează cu worst intraday price când gap-ul e la open

**Modificat: `app/jobs/scheduler.py`**
- Dict nou la nivel de modul: `_gap_risk_by_symbol: dict[str, dict]`
- `job_update_active_trades()` — calculează gap risk per simbol (folosind `snap` deja fetched, zero API calls extra)
- Rezultatul e stocat în `_gap_risk_by_symbol[symbol] = {"level": ..., "note": ...}`
- Populat o dată per simbol per run (cache în memorie, refresh la fiecare run de 2h)

**Modificat: `app/api/routes.py`**
- `/api/active-trades` — câmpuri noi: `gap_risk_level` ("low"/"medium"/"high") și `gap_risk_note` per trade
- Import `_gap_risk_by_symbol` din scheduler; citit din cache (nu face apeluri blocking în route async)
- `get_yf_symbol()` acum chemat o singură dată per trade (eliminată duplicarea din blocul earnings)

**Modificat: `app/static/active-trades.html`**
- Badge `GAP MEDIUM` (amber) / `GAP HIGH` (roșu) în row-ul de badge-uri din header-ul cardului
- Strip de avertizare `gap-strip` (galben) sub earnings strip, cu mesaj specific din engine
- CSS: `.badge--gap-high`, `.badge--gap-medium`, `.gap-strip`
- JS: `gapBadge(level)`, `gapStrip(level, note)`

---

## 2026-04-06 — `improvements_trade_builder`

### Backtesting — Quick Backtest (PRIORITATE 1)

**Modificat: `app/services/backtesting/replay.py`**
- `run_backtest()` — parametru nou `max_outcome_bars: int`
- `_simulate_signal_outcome()` — acceptă `max_bars` în loc de constanta globală

**Modificat: `app/api/backtest_routes.py`**
- `/api/backtest/quick` — redesenat complet:
  - Fereastra fixă: ultimele 90 de zile de semnale, outcomes pe 14 bare (nu mai e 1 an)
  - Adaugă `_compute_current_signal()` — snapshot live al motorului pentru asset-ul curent
  - Apelează `generate_backtest_insight()` cu context curent + orizont 14 zile
  - Returnează: `window_days=90`, `outcome_bars=14`, `current_signal`, `llm_analysis`

**Modificat: `app/services/research/llm.py`**
- `generate_backtest_insight()` — returnează `dict` structurat (nu text liber):
  - Câmpuri: `grade` (A–F), `grade_reason`, `key_insights[]` (3 bullets), `recommendation`
- `_BACKTEST_INSIGHT_SYSTEM` — prompt nou cu rubrică de notare și orizont explicit 14 zile
- `_parse_backtest_insight_json()` — parser tolerant la markdown fences
- Fix Anthropic: `max_tokens = thinking_budget + 1024` când extended thinking e activ

**Modificat: `app/static/active-trades.html`**
- Modal backtest redesenat complet:
  - Card "ENGINE SIGNAL — TODAY": Confidence, Price, SL, TP în grid 4 coloane
  - Grade badge colorat (A=verde, B=teal, C=amber, D=portocaliu, F=roșu) cu `grade_reason`
  - Grid 4 metrici: Win Rate, Avg Profit/Loss, Sharpe Ratio, Max Drawdown
  - Stacked bar W/L/Expired cu legendă
  - AI Key Insights ca bullet list cu chevron icons
  - Confidence buckets cu bare colorate
  - Recommendation box cu border amber
- Fix overflow: `max-height: 90vh` + flexbox pe modal + `overflow-y: auto` pe body
- Fix SL/TP bug: `rangeBar()` și live price cell folosesc `origTP` în loc de `sugTP || origTP`

**Modificat: `app/jobs/scheduler.py`**
- `trade.suggested_tp = trade.current_tp` — TP-ul per-leg nu mai e suprascris de ATR recalculat
- Același fix în `ActiveTradeLevel` logging

**Modificat: `docs/IMPROVEMENT_PLAN.md`**
- PRIORITATE 1 marcată ✅ DONE 2026-04-06
- PRIORITATE 2 marcată ✅ DONE 2026-04-07
- PRIORITATE 3 marcată ✅ DONE 2026-04-07

---

## 2026-04-06 — `improvements_trade_builder` (sesiunea 1)

### Infrastructură

**Nou: TimescaleDB**
- `docker-compose.yml` — serviciu separat `trading-timescaledb` (PostgreSQL 16 + TimescaleDB)
- `requirements.txt` — adăugat `asyncpg`, `psycopg2-binary`, `alembic`
- `app/core/database.py` — suport dual-engine SQLite (dev) / PostgreSQL (prod)

**Nou: Alembic migrations**
- `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`
- `migrations/versions/001_initial_schema.py` — toate tabelele + TimescaleDB hypertables
- `scripts/migrate_sqlite_to_pg.py` — transfer idempotent SQLite → PostgreSQL

**Modificat: `entrypoint.sh`**
- Rulează Alembic + migrare date înainte de uvicorn

### Bug fixes

| Bug | Fișier | Fix |
|-----|--------|-----|
| Platt calibration returna `(0.0, 0.0)` la cold start → `sigmoid(0)=0.5` pentru toate | `calibration.py` | Returnează `None`; caller verifică |
| `routes.py` apela `set_platt_params(0, 0)` necondiționat | `routes.py` | Guard pe `None` |
| S/R snapping long: `pass` și `sl = sr_sl` inversate — SL corect ignorat | `technicals.py:632` | Logică corectată |
| `yfinance ticker.history()` fără timeout — bloca schedulerele indefinit | `technicals.py:52` | `ThreadPoolExecutor` cu timeout 30s |
