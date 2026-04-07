# Changelog — Local Trading Bot v2

> Format: data · branch · ce s-a schimbat și de ce

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
