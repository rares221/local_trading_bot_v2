# Changelog

All notable changes to this project will be documented here.

---

## [Unreleased] — 2026-04-13

### Branch: `realtime_stream_analize`

---

### Added — Finnhub market data routing layer

#### `app/services/market_data.py` — modul nou de routing

- Layer de routing care direcționează cererile de date de piață către **Finnhub** (dacă `FINNHUB_KEY` e setat în `.env`) sau **Yahoo Finance** (fallback implicit).
- Regula de eligibilitate: simbolurile fără caractere speciale (`=`, `^`, `-`, `:`) sunt eligibile pentru Finnhub — acoperă acțiuni/ETF-uri. Futures (`CL=F`), indici (`^GSPC`), FX (`EURUSD=X`), crypto (`BTC-USD`) și DXY rămân pe Yahoo.
- Funcții expuse: `fetch_ohlcv()`, `fetch_quote()`, `fetch_last_price()`, `fetch_company_profile()`, `fetch_metrics()` (P/E, 52w), `fetch_next_earnings()`, `search_symbol()`.
- Client Finnhub inițializat lazy (singleton) la primul apel.

#### `app/core/config.py`

- `finnhub_user: str` — identificare cont (opțional, logging)
- `finnhub_key: str` — API key Finnhub; dacă e gol, tot traficul merge pe Yahoo

#### `requirements.txt`

- `finnhub-python>=2.4.19` adăugat (**necesită rebuild Docker**: `make build start`)

### Changed — toate sursele de date refactorizate prin `market_data`

| Fișier | Ce s-a schimbat |
|---|---|
| `app/services/technicals.py` | `fetch_ohlcv()` → `market_data.fetch_ohlcv()` (eliminat `import yfinance`, `concurrent.futures` din module level) |
| `app/services/earnings_calendar.py` | `_fetch_next_earnings()` → `market_data.fetch_next_earnings()` |
| `app/live_trading/stream_engine.py` | `_fetch_prices_sync()` → `market_data.fetch_quote()` per ticker |
| `app/api/routes.py` | `_get_short_name()` → `market_data.fetch_company_profile()` |
| `app/live_trading/live_routes.py` | chart `_fetch()` → `market_data.fetch_ohlcv()` |
| `app/services/research/__init__.py` | fundamentals (sector, industry, market cap, P/E, 52w) → `market_data.fetch_company_profile()` + `fetch_metrics()`; live price fallback → `market_data.fetch_last_price()` |
| `app/services/ticker_resolver.py` | `_search_yfinance()` redenumit `_search_live()` → `market_data.search_symbol()` |
| `app/services/backtesting/data_provider.py` | prefetch per simbol → `market_data.fetch_ohlcv()` (eliminat `import yfinance`) |
| `app/services/briefing.py` | `_fetch_price()` / `_fetch_change_pct()` → `market_data.fetch_ohlcv()` (eliminat `import yfinance` la nivel de modul) |

---

## [Unreleased] — 2026-04-07

### Branch: `improvements_trade_builder`

---

### Added — LLM Context Enrichment + Horizon-Aware Prompts (P5, P6, P17 din LLM_IMPROVEMENT_PLAN)

#### `app/services/research/__init__.py` — `build_research_context()` extins

- **Sentiment trend**: citit din `analysis.payload["by_asset_sentiment"][asset]["sentiment_trend"]` (`improving | worsening | neutral`)
- **Headline ages**: headlines extinse de la 2 la 3; fiecare cu vârstă în ore din `FeedItem.fetched_at` (`ctx["headline_ages"]`)
- **Market regime**: `detect_market_regime().regime` adăugat în context (`risk_on | risk_off | cautious`)
- **DXY macro**: `detect_dxy_macro()` → `ctx["dxy_trend"]`, `ctx["dxy_change_5d"]`, `ctx["dxy_change_20d"]`
- **Macro events**: `get_upcoming_events(days_ahead=expiry_days)` → `ctx["upcoming_events"]` (FOMC/CPI/NFP în fereastra trade-ului)
- **P/E ratio**: `yfinance info["trailingPE"]` → `ctx["fundamentals"]["pe_ratio"]` (stocuri/ETF)
- **52-week position**: calcul din `fiftyTwoWeekHigh/Low` + `buy_in_price` → `ctx["fundamentals"]["position_52w"]`
- **Sentiment 5d/10d** (far horizon only): query dedicat pe FeedItem (`.limit(500)`, últimele 10 zile) → `ctx["sentiment_5d_avg"]`, `ctx["sentiment_10d_avg"]`; executat doar când `horizon == "far"` pentru a evita round-trip inutil pe near-horizon

#### `app/services/research/llm.py` — `_format_user_content()` restructurat

- Secțiuni **Common** (near + far): horizon + expiry_days, engine confidence/100, sentiment cu trend + notă contradicție direcție, market regime
- Secțiune **Near**: VPOC, SL/TP vs Value Area (compute inline), macro events cu zile rămase, headlines cu vârstă în ore
- Secțiune **Far**: DXY trend cu 5d/20d%, P/E + 52-week position, sentiment 5d/10d avg, headlines fără vârstă; VPOC omis (irelevant pe horizon lung)
- `snapshot_keys` în `_log_rationale_async` extins cu: `horizon`, `expiry_days`, `sentiment_trend`, `sentiment_5d_avg`, `sentiment_10d_avg`, `dxy_trend`, `dxy_change_5d`, `dxy_change_20d`, `headline_ages`

#### `app/services/research/llm.py` — `SYSTEM_PROMPT_NEAR` + `SYSTEM_PROMPT_FAR`

- `SYSTEM_PROMPT` vechi înlocuit cu două constante format-string
- `SYSTEM_PROMPT_NEAR`: tactic — direcție pe `{horizon_days}` zile, ≥2 indicatori specifici, macro events, SL/TP vs VA; interzis limbaj pe termen lung
- `SYSTEM_PROMPT_FAR`: structural — trend EMA/SMA + ADX, context macro DXY/regime, incertitudine explicită pe `{calendar_weeks}` săptămâni; interzis limbaj intraday
- `{hallucination_guard}` placeholder formatat cu `""` până la P14
- `generate_rationale()` selectează promptul bazat pe `context["horizon"]`; `_prompt_version` (`rationale_near_v1` / `rationale_far_v1`) persistat în `llm_rationale_logs`

---

### Added — LLM Rationale Validator (P2 din LLM_IMPROVEMENT_PLAN)

#### `app/services/research/validator.py` — Strat de validare V1–V7

- **V1 — Length check**: rationale < 50 chars → retry cu instrucțiune corectivă
- **V2 — Truncation check**: text > 80 chars fără terminator de propoziție (`.!?"'`) → retry cu `max_tokens=350`
- **V3 — Direction alignment**: detectează concluzie bullish în SHORT sau bearish în LONG; excepție pentru fraze concesive (`despite`, `although`, `even though`, etc.)
- **V5 — Specificity score**: < 2 termeni tehnici din lista de 29 (RSI, MACD, VPOC, ATR, ADX, Bollinger etc.) → retry
- **V7 — Gap advice word count**: > 30 cuvinte → retry (aplicat doar cu `prompt_type="gap"`)
- `ValidationFlag` dataclass: `code`, `message` (log intern), `retry_message` (injectat în LLM)
- `ValidationResult` dataclass: `passed`, `flags: list[str]`, `retry_message: str | None`
- `validate_rationale(text, context, *, prompt_type)`: `prompt_type="gap"` face early-return cu doar V7 — V1/V2/V3/V5 generau false positives pe răspunsuri de 25 cuvinte

#### `app/services/research/llm.py` — Retry loop + wiring

- `generate_rationale()`: loop `MAX_RETRIES=2`; corrective instruction injectată ca `\n\n[CORRECTION REQUIRED]: {message}` în user message pe fiecare attempt; flags acumulate cross-attempt; DB log fire-and-forget la final
- `generate_gap_advice()`: retry loop identic cu V7-only validation; returnează `last_text` chiar dacă nu trece validarea (consistent cu behavior-ul din `generate_rationale`)

### Fixed

#### `app/services/research/llm.py`

- **Bug corrective instruction**: `_corrective_instruction` era setat în `context` dar `_format_user_content()` nu îl citea → fiecare retry trimitea exact același mesaj la LLM; rezolvat prin append explicit la `user` după `_format_user_content()`

---

### Added — Backtest Grade Rubric + Daily Briefing Narrative (P16, P18 din LLM_IMPROVEMENT_PLAN)

#### `app/services/research/llm.py` — `_BACKTEST_INSIGHT_SYSTEM` rubric rescris (P16)

- Grade rubric complet rescris cu criterii cantitative stricte (first-match wins):
  - **A**: `win_rate ≥55%` AND `avg_pnl >0.3R` AND `sharpe >1.0` AND `max_drawdown <4R`
  - **B**: `(win_rate ≥45% AND avg_pnl >0.2R AND max_drawdown <5R)` OR `(≥55% dar ratează un criteriu A)`
  - **C**: `win_rate 35-44%` OR `(≥45% AND avg_pnl 0-0.2R)` OR `max_drawdown 5-8R`
  - **D**: `win_rate 25-34%` OR `avg_pnl negativ > -0.2R`
  - **F**: `win_rate <25%` OR `avg_pnl ≤ -0.2R`
  - Override: `max_drawdown >8R` forțează minim D indiferent de win_rate
- `{horizon_days}` placeholder injectat în system prompt înainte de fiecare apel provider

#### `app/services/research/llm.py` — `generate_brief_narrative()` (P18)

- `_BRIEF_NARRATIVE_SYSTEM`: prompt senior trading desk analyst — brief matinal 3-5 propoziții; focus pe VIX+SPX regime, implicații poziționale, directional bias cu numere specifice; fără disclaimer; perspectivă trader experimentat
- `generate_brief_narrative(checklist_items, recs_count, recs_long, recs_short, active_trades_count, market_regime, dxy_trend, dxy_change_5d)`: construiește user message din checklist + regime; `max_tokens=250`; returnează `str | None`

#### `app/services/briefing.py` — `run_briefing()` actualizat (P18)

- Numără recs_today per direcție (`recs_long`, `recs_short`)
- Numără `active_trades_count` (trades deschise fără outcome)
- Fetches `market_regime` și DXY via `asyncio.to_thread` (non-fatal)
- Apelează `generate_brief_narrative()` (non-fatal)
- Stochează narrativa în `payload["narrative"]`; `content_text` nemodificat (backward compatible)

#### `app/static/briefing.html` — render narrativă (P18)

- CSS: `.briefing-narrative` (border-left albastru, background muted), `.briefing-narrative-label`
- `<div id="briefing-narrative">` adăugat deasupra secțiunii de metrici
- `showBriefing()` actualizat: renderizează `data.payload.narrative` în `#briefing-narrative` dacă present; ascunde div-ul dacă lipsește

---

### Added — Hallucination Bias Profile + Guard + Few-Shot (P13, P14, P15 din LLM_IMPROVEMENT_PLAN)

#### `app/services/research/bias_detector.py` (nou)

- **P13 — `build_hallucination_profile(session, days_back=30)`**: interogare async `llm_rationale_logs`; agregare per `(provider_id, model)` — `v4a_rate`, `v4b_rate`, `v4c_rate`, `v4d_rate`, `any_v4_rate`, `retry_rate`; scriere atomică (`.tmp` → rename) în `data/hallucination_profile.json` (path configurabil via `HALLUCINATION_PROFILE_PATH`)
- **P14 — `build_hallucination_guard(provider_id, model)`**: citește profilul; dacă `sample_size < 10` sau niciun flag rate ≥ 25%, returnează `""`; altfel generează bloc de avertizare personalizat per indicator (preț, valori numerice, news, temporal)

#### `app/services/research/few_shot.py` (nou)

- **P15 — `get_verified_few_shot_examples(session, provider_id, model, horizon, limit=2)`**: bootstrap guard — returnează `""` dacă total logs cu outcome < `MIN_OUTCOMES=10`; selecție în 3 pași: exact match provider+model+horizon → same horizon → orice; filtre: `outcome IS NOT NULL`, `metrics_accuracy_score > 0.8`, fără V4a/V4b în flags; formatare bloc `--- VERIFIED EXAMPLES ---` injectat înainte de contextul curent în user message

#### `app/services/research/llm.py` — `generate_rationale()` actualizat

- **P14**: `build_hallucination_guard(provider_id, model)` apelat sincron înainte de construirea system prompt; rezultatul înlocuiește `hallucination_guard=""` hardcodat în ambele `SYSTEM_PROMPT_NEAR.format()` și `SYSTEM_PROMPT_FAR.format()`
- **P15**: `get_verified_few_shot_examples()` apelat async (sesiune proprie `AsyncSessionLocal`) înainte de loop; bloc injectat la început de user message dacă non-empty

#### `app/jobs/scheduler.py` — job nou

- **`job_build_hallucination_profile()`**: wrapper async ce apelează `build_hallucination_profile()`; înregistrat ca job cron `day_of_week="sun"`, `hour=6`, `minute=0` (duminică 06:00)

---

## [Unreleased] — 2026-04-06

### Branch: `improvements_trade_builder`

---

### Added — Backtesting Framework (PRIORITATE 1)

Implementare completa a engine-ului de backtesting bazat pe historical replay.

#### `app/services/technicals.py` — Backtest Mode
- Adaugat `_backtest_cutoff: Optional[pd.Timestamp]` la nivel de modul
- Adaugat `set_backtest_mode(cutoff)` si `clear_backtest_mode()` pentru a controla taierea datelor
- Adaugat `_apply_backtest_cutoff(df)` — sliceaza DataFrame-ul la data de cutoff
- `fetch_ohlcv()` acum returneaza date taiate cand `_backtest_cutoff` e setat, fara sa altereze cache-ul (full data ramane in cache, slicing e la read)

#### `app/services/backtesting/` — Pachet nou
- **`data_provider.py`**: `prefetch_symbols()` — pre-fetches OHLCV cu period="3y" pentru toti simbolii si injecteaza in `_ohlcv_cache`; `get_trading_days()` — extrage zilele de tranzactionare dintr-o fereastra data; `get_future_bars()` — returneaza bare OHLCV dupa o data (pentru simulare outcome)
- **`simulator.py`**: `simulate_outcome()` — scaneaza barele viitoare si determina `hit_tp` / `hit_sl` / `expired`; gestioneaza edge case-ul in care aceeasi bara atinge ambele niveluri (gap heuristic); calculeaza `pnl_r` (P&L in R-multiples)
- **`replay.py`**: `run_backtest()` — loop principal: pentru fiecare zi de tranzactionare seteaza cutoff, construieste semnale tehnice (fara sentiment), simuleaza outcome-urile si calculeaza metrici; `_build_signal()` — genereaza un semnal pentru un simbol/zi daca confidence >= min_confidence si R:R >= 1.0
- **`metrics.py`**: `compute_metrics()` — calculeaza win rate per confidence bucket (6 bucket-uri: <40, 40-49, ..., 80+), Sharpe ratio anualizat, max drawdown pe curba R cumulativa, avg P&L (R), breakdown pe simbol si directie

#### `app/models/backtest.py` — Modele noi
- `BacktestRun`: id, date range, status (running/completed/failed), metrici agregate (win_rate, sharpe, max_drawdown, avg_rr), `metrics_json` pentru breakdown complet
- `BacktestTrade`: toate campurile unui semnal individual: entry/SL/TP, confidence, outcome, `pnl_r`, `bars_held`, `volatility_regime`, `trend_bias`, `atr_pct`

#### `app/api/backtest_routes.py` — Endpoints noi
- `POST /api/backtest/run` — porneste un run in background (async task); returneaza imediat cu run_id
- `GET /api/backtest/runs` — lista toate run-urile (newest first)
- `GET /api/backtest/runs/{id}` — detalii + metrici pentru un run
- `GET /api/backtest/runs/{id}/trades` — trades paginate cu filtre (outcome, symbol)
- `DELETE /api/backtest/runs/{id}` — sterge run + toate trade-urile

#### `app/static/backtest.html` — Pagina UI noua
- Formular de configurare (date, risk_tier, min_confidence, period)
- Lista run-uri cu status (badge running/completed/failed) si polling automat la 5s
- Metrici agregate: win rate, Sharpe, max drawdown, avg P&L (R), total signals/trades, wins/losses
- Win rate by confidence bucket cu progress bar vizual
- Tabel de trades cu filtre (outcome, symbol), paginare
- Breakdown per simbol si per directie

#### `migrations/versions/002_backtest_tables.py` — Migrare noua
- Creeaza `backtest_runs` si `backtest_trades` cu toate coloanele si indexurile necesare

#### `app/main.py`
- Inregistrat `backtest_router` cu prefix `/api`
- Adaugat ruta `/backtest` pentru pagina HTML

### Changed

#### `app/core/database.py` — Eliminare SQLite
- Eliminat complet codul SQLite (`aiosqlite`, `_ensure_sqlite_data_dir`, `_sqlite_compat_migrations`)
- PostgreSQL + TimescaleDB only — schema gestionata exclusiv prin Alembic
- Adaugat `AsyncSessionLocal` alias pentru background tasks

#### `requirements.txt`
- Eliminat `aiosqlite` (SQLite nu mai e suportat)

#### `app/models/__init__.py`
- Adaugat import pentru `BacktestRun`, `BacktestTrade`

---

## [Unreleased] — 2026-04-03

### Branch: `ui/recommendations-table-improvements`

---

### Added

#### `app/static/briefing.html` — Full Page Redesign

- Replaced plain-text `block-summary` dump with a structured **metric cards grid**
- Each known market metric renders as an individual card with icon, large value, sub-label, and color-coded left border:
  - **S&P 500** — shows percentage move; green border if positive, red if negative
  - **VIX** — auto-detects ≥20 threshold; red value + amber alert border when elevated
  - **WTI Crude / Brent Crude** — price formatted with `$` prefix and comma separator, `USD/bbl` sub-label
  - **Gold / Silver** — same formatting with `USD/oz` sub-label
  - **Recommendations** — shows count with green value when >0
- Items marked with `*` in the briefing text receive an amber **"Notable"** badge in the card top-right corner and an alert border
- Unknown/unparsed items rendered in a clean **"Additional signals"** table below the grid (key / value / notable flag)
- **Refresh button** added in the card header — triggers a new run without scrolling to the bottom
- Number formatting: commodity prices use `toLocaleString('en-US')` for comma separators
- All dynamic icons use `window.icon()` directly (not `data-icon` spans which require DOM hydration)
- New CSS classes: `.briefing-header`, `.briefing-timestamp`, `.briefing-grid`, `.bm-card`, `.bm-card--alert`, `.bm-card--positive`, `.bm-card--negative`, `.bm-label`, `.bm-value`, `.bm-value--green`, `.bm-value--red`, `.bm-value--amber`, `.bm-sub`, `.bm-alert-badge`, `.briefing-extras`, `.briefing-extra-row`, `.briefing-extra-key`, `.briefing-extra-val`, `.briefing-extra-badge`

---

#### `app/static/recommendations.html` — Column Header Tooltips

- Added `headerTooltip` to all 13 Tabulator columns — hovering any column header shows a concise description of what that column means:
  - **Asset** — symbol, trend-alignment badge, volatility regime badge explanation
  - **Dir** — Long vs Short meaning
  - **Entry** — suggested open price
  - **Current** — live price with % change from entry
  - **SL** — Stop Loss purpose
  - **TP** — Take Profit purpose
  - **R:R** — Risk-to-Reward explanation with example
  - **Conf** — confidence score + calibrated probability explanation
  - **B** — Shadow-B filter pass/fail explanation
  - **Risk** — tier assignment logic
  - **Size** — portfolio % to risk
  - **Expiry** — stale date + days remaining
  - **Actions** — Took it / Skip / Research button purposes

---

#### `app/static/recommendations.html` — Large Number Formatting

- `fmtNum()` updated from `.toFixed(2)` to `.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })`
- Prices like `73319.55` now display as `73,319.55` across Entry, Current, SL, and TP columns

---

#### `app/static/dashboard.html` — Sector Regimes Redesign

- Replaced the metric-box grid with a full-width row-based layout — each sector occupies one row
- Each row: sector name (fixed 100px left) → gradient bar filling all remaining space → regime label + RS badge (right-aligned 160px)
- Gradient bar (red→yellow→green) reuses the same pattern as the Market Regime card
- Marker dot spring-animates to regime position (`risk_off: 7%`, `cautious: 50%`, `risk_on: 93%`)
- RS badge color-coded: green pill (positive), red pill (negative), gray (neutral), threshold ±0.5%
- Description text moved to ⓘ tooltip on section header (same `.help-btn` pattern)
- New CSS classes: `.sector-grid`, `.sector-row`, `.sector-name`, `.sector-bar-wrap`, `.sector-bar`, `.sector-bar-marker`, `.sector-right`, `.sector-regime-lbl`, `.sector-rs`

---

#### `app/static/dashboard.html` — Cross-Asset Correlations: Human-Friendly Warnings

- Replaced raw ticker symbols (`GC=F`, `^GSPC`) with human names via `TICKER_NAME` map
- Replaced `(up)`/`(down)` direction codes with `rising`/`falling`
- Each divergence now renders as a plain-English sentence: *"Gold is rising while S&P 500 is also rising — they normally move in opposite directions"*
- Multiple divergences render as a bullet list
- `expected_relationship` field from API mapped to readable explanations (`inverse` → "they normally move in opposite directions")

---

#### `app/services/research/llm.py` + `app/api/routes.py` + `app/static/dashboard.html` — LLM Correlation Analysis

- New `generate_correlation_analysis(diverging_signals)` function in `llm.py`:
  - Dedicated system prompt: concise market analyst, 2-3 sentences, plain text only (no markdown)
  - Uses same multi-provider dispatch as `generate_rationale()` (OpenAI / Anthropic / Ollama / OpenRouter)
  - Builds a compact prompt listing diverging pairs with asset names and directions
- New `GET /api/market/correlations/analysis` endpoint:
  - Fetches live signals via `asyncio.to_thread(check_cross_asset_signals)`
  - Filters diverging signals, calls `generate_correlation_analysis()`
  - Returns `{analysis, signal_count}` or `{analysis: null, reason: "no_divergences"}`
  - Fully async — does not block event loop or other requests
- Frontend renders LLM result below the rule-based warning:
  - Left blue accent border + primary-tinted background
  - "LLM ANALYSIS" label with info icon
  - Fires as a separate `fetch()` after matrix renders — non-blocking
  - Shows *"Analysing…"* placeholder while waiting
  - JS strips any markdown (`##` headers, `**bold**`, `*italic*`) the LLM returns
  - Box hidden silently if LLM unavailable or no divergences
- Analysis is generated live per request (not cached from pipeline) — reflects current market data

---

#### `app/static/active-trades.html` — SL/TP Level History Chart (TradingView Lightweight Charts)

- Added TradingView Lightweight Charts v4.2.0 (CDN, pinned to avoid v5 breaking API changes) to the Active Trades page
- Each trade's level history panel now shows an interactive dark-mode chart with:
  - Green line — Take Profit over time
  - Red line — Stop Loss over time
  - Dashed white line — Entry price (constant reference)
- Chart initializes lazily on first panel open (avoids zero-width issue when panel is hidden)
- `ResizeObserver` per chart instance handles container width changes
- All chart instances are destroyed and re-created on data reload to prevent stale state
- `.history-layout` CSS splits the panel into left (table) and right (chart) at 55%/45%

---

#### `app/static/dashboard.html` — Renamed to Configuration + Tabbed Layout

- Page title and nav link renamed from "Dashboard" to "Configuration" across all 8 HTML pages
- Nav icon changed from `layout-dashboard` to `sliders-horizontal`
- All sections reorganized into 5 tabs with segmented control UI:
  - **Settings** — Risk preference, LLM provider, Webhook notifications
  - **Pipeline** — Pipeline actions, Market regime status
  - **Market** — Sector regimes, Correlations
  - **Calibration** — Confidence calibration stats
  - **Docs** — Metric explanations, Quick navigation
- Tab state persisted to URL hash (`#settings`, `#pipeline`, etc.) and restored on page load

---

#### `app/static/dashboard.html` — Market Regime Status Card Redesign

- Full visual redesign of the Market Regime card to a trading-style layout:
  - Gradient progress bar (red → yellow → green) spanning Risk-Off to Risk-On
  - Animated spring-easing marker (`cubic-bezier(.34,1.4,.64,1)`) that moves to current regime position
  - Large uppercase regime badge with color-coded background
  - Description line below badge summarizing what the regime means
- `REGIME_META` object maps regime names to marker position, color, and description text
- Replaced static "LIVE" badge with a 60-second countdown timer:
  - Starts green, transitions to yellow at 30s, red + blink at 10s
  - Resets and re-fetches regime data at 0
- Verbose regime description text moved to a CSS-only tooltip on an info (ⓘ) icon
- Tooltip positioned below-right of icon with upward-pointing arrow; does not overflow viewport
- Duration and Flagged trade count displayed as stat chips below the badge

---

#### `app/services/regime_monitor.py` — Persist Regime State Across Restarts

- Added `_STATE_FILE = Path("./data/regime_state.json")` (same Docker volume as SQLite DB)
- `_load_state()` — reads JSON once per process on first call (guarded by `_state_loaded` flag)
- `_save_state()` — writes `{regime, since}` JSON after every state change
- `detect_regime_transition()` now calls `_load_state()` on entry and `_save_state()` after updates
- `reset_regime_tracking()` now also deletes the JSON file and resets `_state_loaded`
- Regime duration (`since` timestamp) now survives container restarts

---

#### `app/api/routes.py` — Fixed Duration Reset on `/api/regime/status`

- Root cause: endpoint was calling `build_market_context()` + `detect_regime_transition()` on every 60-second UI poll; yfinance data oscillating near VIX thresholds caused regime to flip each call, resetting `_last_regime_at` to `now()`
- Fix: endpoint now only reads persisted state via `get_current_regime_state()` — no live market fetch
- One-time bootstrap added: if `current_regime` is `None` (fresh install, no state file), the endpoint fetches live market context once to initialize state

---

#### `app/static/dashboard.html` — Risk Preference Card Redesign

- Replaced verbose inline `<p class="meta">` explanations with a clean row-based layout
- Each setting is a horizontal row: label (left) + input (right), separated by a bottom border
- All explanatory text moved into CSS-only tooltips on ⓘ info icon buttons (reuses `.help-btn` / `.help-tooltip` pattern)
- **Risk stance** — select dropdown, right-aligned
- **Risk budget per batch** — number input + "%" unit label
- **Sentiment decay half-life** — number input + "hours" unit label
- **Min. confidence thresholds** — colored regime tags (green/yellow/red pill) above each input in a flex row
- **A/B shadow mode** — replaced bare checkbox with an animated toggle switch (CSS-only, no JS)
- **B delta sub-row** — indented with left accent border, only visible when shadow mode is enabled; collapses when toggled off
- New CSS classes: `.pref-row`, `.pref-label-wrap`, `.pref-label`, `.pref-input-wrap`, `.pref-unit`, `.pref-regime-grid`, `.pref-regime-cell`, `.regime-tag`, `.toggle-switch`, `.toggle-track`, `.toggle-thumb`, `.badge-b`, `.pref-sub-row`

---

#### `app/services/pipeline_progress.py` — New: In-Memory Pipeline Progress Tracking

- New singleton module providing shared pipeline state readable by the API
- `start()` — resets all state, records `started_at`
- `update(pct, stage, message)` — updates progress mid-run (clamped 0–99)
- `log_stage(name, status, detail, warnings)` — records a completed stage result with status `ok | warn | error | skipped`
- `finish(error=None)` — marks run complete; sets `pct=100` on success or records error string
- `get()` — returns JSON-serialisable dict including `running`, `pct`, `stage`, `message`, `error`, `elapsed_seconds`, and `stages[]` list

---

#### `app/jobs/scheduler.py` — Pipeline Progress Instrumentation

- `job_ingest_and_sentiment()`:
  - `fetch_all_feeds()` wrapped in `asyncio.to_thread()` (was blocking the event loop during RSS network I/O)
  - VADER scoring loop wrapped in `asyncio.to_thread()` (CPU-bound sync work)
  - 6 progress checkpoints: 2% → 8% → 12% → 12–25% (per-item) → 25%
  - Logs stage result: feed count, scored item count
- `job_run_analysis()`:
  - 3 progress checkpoints: 27% → 32% → 50%
  - Logs stage result on completion
- `job_daily_batch()`:
  - 8 progress checkpoints: 52% → 55% → 60% → 60–96% (per LLM call, shows asset name) → 97% → 100%
  - Tracks `llm_failures[]` list — assets where rationale generation failed
  - Logs stage result: candidate count, category breakdown (e.g. "3 stocks, 2 commodities"), rationale success rate, warnings list for each LLM failure

---

#### `app/api/routes.py` — Pipeline Progress API + Background Execution

- `GET /api/pipeline/progress` — returns current `pipeline_progress.get()` state for polling
- `POST /api/actions/run-pipeline-once` — refactored to:
  - Return `202` immediately (fire-and-forget via `asyncio.create_task()`)
  - Guard against double-run: returns `{"ok": false}` if already running
  - Pipeline runs as background task; HTTP server remains responsive for progress polls

---

#### `app/static/dashboard.html` — Pipeline Progress Bar

- **Run full pipeline** button replaced by animated progress bar during execution:
  - White background, blue fill sweeping left → right (`transition: width .6s cubic-bezier`)
  - Amber text (`#d97706`) centered over bar: stage message + percentage
  - Flashes green briefly on completion, then fades back to normal button
- Polls `GET /api/pipeline/progress` every 1.5 seconds while running
- On error: toast shown, bar dismissed, button restored
- Guard: result panel hidden and bar reset on each new run start

---

#### `app/static/dashboard.html` — Pipeline Result Status Panel

- After each pipeline run a result panel appears below the button showing per-stage outcomes:
  - ✅ Green check — stage completed successfully
  - ⚠️ Yellow warning — stage completed with non-fatal issues (e.g. some LLM rationales failed)
  - ❌ Red X — stage errored out
  - Inline warning list for each LLM failure: `{asset_id}: {error}`
- Run elapsed time shown in panel header (e.g. `1m 43s`)
- Panel hidden when a new run starts
- Inline SVG icons (no external dependency)

---

## [Unreleased] — 2026-03-27

### Branch: `ui_improvements_1`

---

### Changed

#### `app/static/recommendations.html` — Research Modal Redesign + Performance Fix

**Research modal — complete visual overhaul**

The research modal was previously a plain text dump inside a single `<p>` tag. It has been replaced with a structured, data-rich layout:

- **Wider modal** (780px) to accommodate the chart and metrics bar.
- **Structured header** — shows direction badge (LONG/SHORT), asset ID, and a meta line with company short name, sector, industry, and market cap (fetched live from Yahoo Finance via the existing `/api/research/summary?generate=false` endpoint).
- **Metrics bar** — 6 inline chips rendered immediately from the recommendation row data (no extra API call needed):
  - Entry price
  - Stop Loss (with % downside from entry)
  - Take Profit (with % upside from entry)
  - Risk:Reward ratio
  - Confidence score (color-coded: green ≥70%, yellow ≥40%, red <40%)
  - Sentiment score (color-coded positive/neutral/negative)
- **Illustrative trade forecast chart** (Chart.js 4) — 14-day projected price paths:
  - Bull case path (green) trending toward Take Profit
  - Base case path (indigo dashed) with moderate drift
  - Bear case path (red) trending toward Stop Loss
  - Shaded zones: green fill between TP reference and entry, red fill between SL reference and entry
  - Tooltip shows all three scenarios + reference levels on hover
  - Seeded pseudo-random per asset (same chart every time for the same asset)
  - "Illustrative simulation only" disclaimer shown below chart
- **Research summary text** — same LLM-generated content but rendered as readable paragraphs (sentence grouping: ~3 sentences per paragraph, with breaks before risk/technical sections).
- **Latest Headlines section** — shows up to 2 recent news headlines from `context.headlines` fetched alongside context.
- **Two-phase load**: metrics and chart render immediately from row data; fundamentals/headlines load via fast `generate=false` context call; LLM summary only generated if no `rationale_text` already exists on the recommendation.

**Performance fix — lazy Chart.js loading**

The eager `<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js">` tag was blocking the entire page from calling `load()` until Chart.js (~240KB) finished downloading. On cold cache or slow CDN connections this caused 1–3 seconds of "Loading recommendations…" before any data appeared.

Fix: removed the blocking script tag. Chart.js is now loaded on-demand via a `loadChartJs(callback)` helper that dynamically injects the script only when the Research modal is opened for the first time. On subsequent opens it is already cached in the browser and renders instantly. The recommendations table now populates as fast as the API responds (~15–70ms).

**Other cleanup**

- Replaced emoji icon (`💡`) in the "Understanding Recommendations" summary with plain text.
- `showResearch()` signature extended from `(assetId, category, text)` to `(assetId, category, text, rec)` — the full recommendation object is passed so metrics and chart can be rendered without an extra API round-trip.
- SVG close button replaces the `&times;` character in the modal header.

---

#### Research Modal — UI polish + chart overhaul (2026-03-27)

**Modal width fix**

- Increased research modal max-width from `780px` to `min(1100px, 95vw)` so it fills most of the browser window and the chart has room to breathe.

**Chart background**

- Chart wrapper background set to `#0f172a` (dark slate) with top/bottom borders, giving a TradingView-style dark panel regardless of page theme.
- Grid lines and axis labels forced to dark-theme colors (`rgba(255,255,255,.06)` / `#94a3b8`).
- Chart.js `beforeDraw` plugin used to paint the canvas background dark (ensures dark bg even if canvas is screenshot/exported).

**Chart height fix**

- Canvas was previously a bare `<canvas height="180">` which Chart.js ignored when `maintainAspectRatio: false`, causing the chart to expand unbounded downward.
- Fixed by wrapping the canvas in `<div style="position:relative;height:220px;">` — Chart.js requires a fixed-height parent to correctly constrain the canvas.

**Monte Carlo GBM simulation replaces fake paths**

The previous "forecast" was three nearly-straight lines (bull/base/bear) generated by a seeded linear interpolation with minimal noise — not based on any statistical model.

Replaced with a proper **Geometric Brownian Motion (GBM) Monte Carlo simulation** — the industry standard used in Bloomberg, institutional research, and options pricing:

- **500 independent paths** simulated using the exact GBM discrete formula:
  `S(t+1) = S(t) × exp((μ − σ²/2) + σ × Z)` where Z ~ N(0,1)
- **Itô correction** (`−σ²/2`) applied to ensure E[S(t)] = S₀·e^(μt) (mathematically correct log-normal drift)
- **Box-Muller transform** used to generate standard normal variables from `Math.random()` (no external math library needed)
- **σ (daily volatility)** derived from TP–SL log-range: `σ = |ln(TP/SL)| / (2 × √T)` — the SL distance is treated as approximately a 2σ move over the holding period
- **μ (daily drift)** derived from confidence score and expected log-return: `μ = (conf × ln(TP/entry) + (1−conf) × ln(SL/entry)) / T` — at 50% confidence the drift is neutral; higher confidence tilts it toward TP
- **Fan chart with percentile bands** replacing the three-path display:
  - Outer band (80% CI): 10th–25th and 75th–90th percentile fills (light indigo)
  - Inner band (50% CI): 25th–75th percentile fill (darker indigo)
  - Median line: 50th percentile path
- **P(TP) probability** displayed in the chart header — percentage of the 500 simulations that reached Take Profit before Stop Loss
- Chart title updated to "Monte Carlo GBM · 500 paths · 14-day horizon"
- Disclaimer updated to reflect the actual model used

---

#### Icon System — Lucide SVG icons across all pages (2026-03-27)

**New file: `app/static/icons.js`**

A lightweight, zero-dependency icon system (~3KB) based on the [Lucide](https://lucide.dev/) icon set — stroke-based SVG icons optimized for dashboards and data-heavy UIs.

- `window.icon(name, size)` — returns an inline SVG string for use in JS-generated HTML
- `data-icon="name"` / `data-icon-size="px"` — HTML placeholder pattern hydrated automatically on `DOMContentLoaded` via `hydrateIcons()`
- All icons are 24×24 viewBox, `stroke="currentColor"`, `fill="none"` — inherit color from parent text/button

**Icons included (30 total) and their assignments:**

| Icon name | Used on |
|---|---|
| `layout-dashboard` | Nav — Dashboard link |
| `list-checks` | Nav — Recommendations link |
| `search` | Nav — Research link; Research page Resolve button |
| `bar-chart-2` | Nav — Analysis link |
| `activity` | Nav — Active Trades link |
| `book-open` | Nav — Journal link |
| `newspaper` | Nav — Briefing link |
| `play-circle` | Briefing page — Run briefing now button |
| `sparkles` | Research page — Get research button |
| `filter-x` | Journal page — Reset Filters button |
| `info` | Journal page — Understanding Outcomes collapsible header |
| `gauge` | Analysis page — About Market Regime info banner |
| `check` | Recommendations — Took it action button |
| `x` | Recommendations — Skip action button |
| `refresh-cw` | Active Trades — Refresh button |
| `square-x` | Active Trades — Close manually button |
| `history` | Active Trades — Level history toggle button |
| `trending-up` | Dashboard — Run pipeline button; stat headers |
| `zap` | Dashboard — LLM provider section title |
| `settings` | Dashboard — Configuration section title |
| `database` | Dashboard — Data section title |
| `plus-circle` | Recommendations — Add recommendation button |
| `trash-2` | Recommendations — Delete action |
| `check-circle` | Recommendations — Confirm/approve action |
| `alert-triangle` | Alert banners (warning) |
| `wifi-off` | Error/offline states |
| `clock` | Expiry fields |
| `arrow-up` | Long direction indicator |
| `arrow-down` | Short direction indicator |
| `bar-chart` | Confidence/stats labels |

**CSS additions to `app/static/css/common.css`:**

```css
.btn-icon { display: inline-block; flex-shrink: 0; vertical-align: -2px; }
.nav a .btn-icon { opacity: .65; transition: opacity .15s; }
.nav a:hover .btn-icon, .nav a.active .btn-icon { opacity: 1; }
.section-title .btn-icon { opacity: .55; margin-right: 5px; vertical-align: -2px; }
.btn { gap: 6px; }
```

**Pages updated:**

- `dashboard.html` — nav icons + all buttons + section title icons
- `recommendations.html` — nav icons + row action buttons (Took it / Skip / Research)
- `active-trades.html` — nav icons + Refresh, Close manually, Level history buttons
- `journal.html` — nav icons + Reset Filters + info collapsible icon (replaces 📊 emoji)
- `research.html` — nav icons + Resolve + Get research buttons
- `analysis.html` — nav icons + gauge icon in info banner (replaces 💡 emoji)
- `briefing.html` — nav icons + Run briefing now button

> **Docker rebuild required** — `icons.js` is a new static file baked into the image. Run `make build && make start` to apply.

---

#### `app/static/active-trades.html` — Card UI Redesign (2026-03-27)

Complete visual overhaul of the Active Trades page for better readability and scannability.

**Card status indicator**

- Added colored left border to each trade card communicating health at a glance:
  - Green — open, signal aligned
  - Yellow — signal weakening (1–2 consecutive flips)
  - Red — signal flipped (3+ consecutive flips)
  - Gray/faded — closed trade

**Header redesign**

- Asset name enlarged (1.25rem, bold)
- All status badges (Direction, Confidence, Signal, Phase) moved to a single inline row with `|` separators — previously stacked vertically in separate `badge-group` columns which were hard to scan
- Category shown as a small inline chip next to the asset name

**Levels layout — 3-column grid replaces 6 flat cells**

- Previous layout: 6 equal cells (Entry, Orig SL, Orig TP, Sug SL, Sug TP, Expiry) with no visual relationship between related values
- New layout: 3 columns — **Entry** | **Stop Loss** | **Take Profit**
- When a suggested level differs from the original, it renders as: `~~$204.67~~ → $204.91` with the new value colored green (improvement) or red (worse) based on trade direction:
  - Long SL: green if suggested > original (tighter protection), red otherwise
  - Long TP: green if suggested ≥ original (higher target), red otherwise
  - Short directions inverted
- `% from entry` hint shown under each level (e.g. `-1.5%` under SL, `+3.0%` under TP)
- R:R ratio displayed as a hint under the Entry price

**Range bar**

- Added a visual gradient bar (red → green) below the levels grid showing where the entry price sits between Stop Loss and Take Profit
- White marker pin indicates entry position within the SL–TP range
- Labels: SL on the left (red), TP on the right (green), Entry centered (muted)
- Uses suggested levels if available, falls back to originals

**Footer improvements**

- Expiry date moved to the right side of the footer with urgency coloring: red if expiring today/tomorrow, amber if within 3 days
- "Close manually" restyled as `btn--danger` to visually communicate it is a destructive action
- "Level history" button label simplified
