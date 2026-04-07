# Improvement Plan — Local Trading Bot v2

> Ultima actualizare: 2026-04-06
> Branch activ: `improvements_trade_builder`

Acest document consolideaza toate imbunatatirile identificate, statusul lor curent, si ordinea de implementare recomandata.

---

## Starea curenta a sistemului

### Pipeline complet functional

```
RSS (44+ feeds, 2h) → VADER/FinBERT sentiment → asset tagging
    ↓
Agregare cu exponential decay (half-life 24h) + source/entity weights
    ↓
Technical analysis: 11 indicatori pe 90 zile daily OHLCV (yfinance)
    ↓
Market regime: SPX trend + VIX level → risk_on / cautious / risk_off
    ↓
Recommendation engine: confidence 0-100, Half-Kelly sizing, SL/TP ATR-based
    ↓
Active trade management: refresh SL/TP la 2h, outcome logging
    ↓
Calibrare: Platt scaling pe trade-uri inchise
```

### Baza de date
- **PostgreSQL + TimescaleDB** (prod via Docker)
- **SQLite** (dev local)
- Migrare automata SQLite → PostgreSQL la pornire cu `MIGRATION_ENABLED=true`
- Schema gestionata prin **Alembic** (migrations/)

---

## Fixuri implementate (2026-04-06)

### Bug fixes programator

| Bug | Fisier | Status |
|-----|--------|--------|
| Platt calibration returna `(0.0, 0.0)` la cold start → `sigmoid(0)=0.5` pentru toate trade-urile | `app/services/calibration.py` | ✅ Fix: returna `None`, caller verifica |
| `routes.py` apela `set_platt_params(0, 0)` neconditional | `app/api/routes.py` | ✅ Fix: guard pe `None` |
| S/R snapping long: `pass` si `sl = sr_sl` inversate — SL corect ignorat, cel gresit aplicat | `app/services/technicals.py:632` | ✅ Fix: logica corectata |
| `yfinance ticker.history()` fara timeout — putea bloca schedulerele indefinit | `app/services/technicals.py:52` | ✅ Fix: `ThreadPoolExecutor` cu timeout 30s |

### Infrastructura

| Task | Status |
|------|--------|
| TimescaleDB ca serviciu separat in `docker-compose.yml` | ✅ Done |
| `asyncpg`, `psycopg2-binary`, `alembic` in `requirements.txt` | ✅ Done |
| Suport dual-engine: SQLite (dev) / PostgreSQL (prod) in `database.py` | ✅ Done |
| Alembic setup complet: `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako` | ✅ Done |
| Migration `001_initial_schema.py`: toate tabelele + TimescaleDB hypertables | ✅ Done |
| `scripts/migrate_sqlite_to_pg.py`: transfer idempotent SQLite → PostgreSQL | ✅ Done |
| `entrypoint.sh`: Alembic + migrare date inainte de uvicorn | ✅ Done |

---

## Imbunatatiri anterioare (din `FUTURE_RECOMMENDATION_IMPROVEMENTS.md`)

### Tehnic & indicatori — DONE
- ✅ Dynamic indicator weights per regime (trend vs mean-reversion balance)
- ✅ Regime-specific RSI/Stochastic thresholds
- ✅ VWAP integration in confidence score
- ✅ Multi-timeframe alignment (daily + weekly + 4H)
- ✅ Volatility regime (expanding/contracting/stable via ATR slope)
- ✅ Indicator conflict penalty (0-25 pts) + skip daca penalty ≥ 25

### Sentiment & news — DONE
- ✅ FinBERT optional blend cu VADER
- ✅ Source reliability weights (Reuters 1.2x, Fed 1.35x)
- ✅ Entity weights (banci centrale 1.25x)
- ✅ Sentiment–price divergence detection
- ✅ Event tagging (earnings, Fed, geopolitics, macro)

### Regime & context — DONE
- ✅ Correlation filter la batch (skip daca corr ≥ 0.75 cu pozitie existenta)
- ✅ Macro calendar overlay: FOMC/CPI/NFP ±1 zi → -10 confidence
- ✅ Macro dates auto-fetch cu 7-day cache TTL

### Calibrare & sizing — DONE
- ✅ Outcome logging: hit_tp / hit_sl / expired / manual
- ✅ Platt scaling calibration (`fit_platt_calibration`)
- ✅ Brier score + ECE tracking
- ✅ Confidence buckets cu hit rate empiric
- ✅ Half-Kelly position sizing (`suggested_risk_pct`)
- ✅ Risk budget per batch (cap suma sizing-ului)
- ✅ Manual trade close

---

## Ce ramane de implementat — perspectiva trader

### ✅ PRIORITATE 1 — Backtesting (foundation pentru tot restul) — DONE 2026-04-06

**De ce e problema:**
Intreaga strategie e construita pe logica "suna bine". Nu stim daca win rate-ul real e 40% sau 60%. Fara backtest nu poti raspunde la nicio intrebare esentiala: functioneaza mai bine pe trend sau pe range? Cate false signals genereaza in risk_off? Cat de des sare SL-ul ATR-based vs unul mai larg? Fara aceste raspunsuri, orice "imbunatatire" adaugata e o schimbare oarba — poti sa faci sistemul mai rau crezand ca il faci mai bun.

In plus, Platt calibration are nevoie de date reale de outcome ca sa fie utila. Momentan are putine inchideri, deci calibrarea e slaba. Backtestingul genereaza sute de "inchideri" simulate pe care le poti folosi pentru calibrare mai rapida.

**Ce trebuie construit:**
- Historical replay engine: re-ruleaza engine-ul pe date istorice cu snapshot-uri de analiza din trecut
- Out-of-sample split: train / validation / test pe date istorice
- Metrici: win rate per confidence bucket, Sharpe ratio, max drawdown, avg R:R realizat vs proiectat
- Benchmark: comparatie vs buy-and-hold SPX si vs strategie random

**Efort:** Mare | **Impact:** Critic — deblocheaza validarea oricarei alte imbunatatiri

---

### ✅ PRIORITATE 2 — Earnings calendar pe actiuni individuale — DONE 2026-04-07

**De ce e problema:**
Avem penalizare pentru FOMC/CPI/NFP dar nu pentru earnings individuale. Aceasta e cea mai mare sursa de gap risk pe actiuni. Un trade deschis pe AAPL cu 2 zile inainte de earnings poate pierde 8-10% intr-o singura noapte daca rezultatele surprind piata negativ. SL-ul setat la 2x ATR (tipic ~3-4% pentru o actiune mare) nu te protejeaza deloc de un astfel de gap. Sistemul nu stie ca AAPL raporteaza maine.

Mai mult: in zilele inainte de earnings, volatilitatea implicita creste (IV crush), pretul se misca mai erratic, si semnalele tehnice devin mult mai putin fiabile. Un RSI de 45 sau un MACD bullish cu o zi inainte de earnings nu inseamna nimic.

**Ce trebuie construit:**
- Integrare earnings calendar via `yfinance get_earnings_dates()` cu cache per ticker
- Penalizare confidence -15 in fereastra ±2 zile de earnings pentru actiuni individuale
- Flag `near_earnings: true` in recommendation payload si UI
- Optiune configurabila sa skipezi complet actiunile cu earnings iminent

**Efort:** Mic-mediu | **Impact:** Mare pentru equity recommendations

---

### ✅ PRIORITATE 3 — Circuit breaker / daily loss limit — DONE 2026-04-07

**De ce e problema:**
Daca sistemul genereaza 5 semnale proaste consecutiv (poate din cauza unui regime shift neasteptat, a unui black swan, sau pur si simplu a unei perioade proaste), continua sa genereze semnale noi ca si cum nimic nu s-a intamplat. Orice sistem de trading profesionist are un mecanism de oprire automata cand lucrurile merg rau — nu din frica, ci pentru ca o serie de pierderi consecutive e un semnal ca ceva s-a schimbat in piata si modelul nu mai e calibrat corect pe contextul curent.

Momentan nu exista nicio protectie dincolo de confidence threshold si risk budget per batch. Ambele sunt statice — nu se adapteaza la performanta recenta.

**Ce trebuie construit:**
- `max_daily_loss_pct`: daca suma pierderi realizate in ziua curenta > X%, se opreste generarea de semnale noi
- `max_consecutive_losses`: daca N trade-uri consecutive au outcome `hit_sl`, reduce sizing-ul cu 50% sau opreste batch-ul
- `drawdown_gate`: daca drawdown din peak > Y%, activeaza modul defensiv (confidence threshold +10, sizing -30%)
- Toate configurabile in `preferences.json` cu valori default rezonabile

**Efort:** Mic | **Impact:** Mare pentru risk management

---

### ✅ PRIORITATE 4 — Gap risk modeling — DONE 2026-04-07 (complet)

**De ce e problema:**
ATR zilnic masoara volatilitatea intrazilnica si de la o zi la alta in conditii normale. Nu modeleaza gap-urile — diferente bruste de pret care apar la deschidere cand piata e inchisa. Pe actiuni, un earnings surprise sau o stire macro overnight poate deschide piata cu 5-10% diferenta fata de inchiderea precedenta. Daca SL-ul e la -3%, un gap de -7% inseamna ca iesi din pozitie cu -7%, nu cu -3%. Asta rupe complet calculul R:R si Half-Kelly care presupun ca SL-ul e executat la pretul setat.

**Ce s-a implementat:**
- **Recomandari (sizing)**: `_compute_gap_risk()` cu 5 factori multiplicativi → `gap_risk_level` + `sizing_mult`; cap 2.5% aplicat ÎNAINTE de `gap_mult` (fix bug: altfel gap mult nu aveau efect)
- **Active trades (avertizare)**: gap risk calculat în `job_update_active_trades()` din `snap` deja fetched → stocat în `_gap_risk_by_symbol`; `/api/active-trades` expune `gap_risk_level` + `gap_risk_note`; UI: badge `GAP HIGH/MEDIUM` + strip de avertizare amber
- **Backtesting (slippage realist)**: `simulator.py` — dacă `bar_open <= stop_loss` (gap la deschidere) → exit la `bar_open` nu la `bar_low` (worst case); același fix pentru short

**Efort:** Mediu | **Impact:** Mare pentru equity si crypto

---

### ✅ PRIORITATE 5 — Volume Profile (VPOC, VAH, VAL) — DONE 2026-04-07 (complet)

**De ce e problema:**
Support/Resistance curent e detectat pe swing highs/lows: preturile la care piata s-a intors in trecut. Problema e ca swing highs/lows sunt evident vizibile pe grafic si toata lumea le stie. Pietele tind sa "testeze" aceste niveluri tocmai ca sa ia stop-urile plasate acolo, inainte sa continue directia initiala.

Volume Profile arata ceva diferit si mai util: unde a stat pretul cel mai mult timp, adica unde au tranzactionat cei mai multi participanti. Aceste niveluri sunt mai "adevarate" pentru ca reprezinta pretul de echilibru al pietei, nu doar punctele de intoarcere vizibile. Un VPOC este mult mai greu de spart decat un simplu swing high.

**Ce s-a implementat:**
- VPOC (Volume Point of Control): pretul cu cel mai mare volum tranzactionat in perioada lookback
- VAH/VAL (Value Area High/Low): zona cu 70% din volumul total
- HVN/LVN (High/Low Volume Nodes): niveluri de clustering si goluri de volum
- Integrare in `TechnicalSnapshot`: VPOC ca nivel S/R prioritar fata de swing levels
- Snap SL/TP la VPOC/VAH/VAL: mai intai verifica Volume Profile, abia apoi swing S/R
- Confidence adjustments: `in_value_area` −8, near LVN +5, overextension >2× −10, >1.3× −5, VP confirma directia +4
- VP walk-forward in backtesting: automat corect prin `set_backtest_mode(cutoff)` → OHLCV slice
- VP analytics in metrici backtesting: win rate / avg_pnl_r inside vs outside value area
- UI complet: VP strip in active-trades (VPOC, bias, dist%, IN VA, overextension) + tooltip 3s
- VP badges in recommendations (VA ZONE, VP EXT)
- VP in research pipeline: context dict + LLM prompt + VP strip in Market Context cu tooltip 3s

**Efort:** Mediu | **Impact:** Mare — imbunatateste fundamental calitatea nivelurilor S/R

---

### PRIORITATE 6 — Relative Strength vs benchmark

**De ce e problema:**
Sistemul judeca un asset in mod absolut: RSI de 55, MACD bullish, ADX de 30 → long. Dar nu conteaza doar daca un asset e bullish in absolut — conteaza daca e mai puternic sau mai slab decat piata. Intr-o piata bullish generala (SPX +1.5% pe zi), o actiune care face +0.3% e de fapt slaba relativ la piata. Sa mergi long pe ea inseamna sa cumperi cel mai slab asset intr-o zi in care restul pietei e puternic — exact ceea ce fac traderii care pierd.

Sector rotation e un fenomen real: banii se muta din tech in energy, din growth in value, in cicluri clare. Un sistem care ignora acest lucru va recomanda long pe tech chiar cand banii ies din tech spre alta parte.

**Ce trebuie construit:**
- `relative_strength(symbol, benchmark, period)`: return cumulativ al asset-ului vs SPX/sector ETF pe 20 si 60 zile
- RS ratio integrat in confidence score: RS > 1.2 → +5 pts, RS < 0.8 → -8 pts
- Sector ETF mapping: AAPL/MSFT → XLK, XOM/CVX → XLE, JPM/GS → XLF, JNJ → XLV etc.
- `sector_aligned` flag extins: nu doar directie, ci si RS relativ la sector

**Efort:** Mediu | **Impact:** Mediu-mare — filtreaza candidate slabe in sectoare out-of-favor

---

### PRIORITATE 7 — News latenta < 15 minute

**De ce e problema:**
RSS feeds cu refresh la 2 ore inseamna ca o stire aparuta acum e procesata cel mai devreme peste 2 ore. O stire importanta (earnings surprise, decizie Fed neasteptata, geopolitica brusca) misca piata in primele 5-15 minute. Pana procesam semnalul, miscarea e complet facuta, si daca intrarea recomandata era bazata pe acea stire, acum entry-ul e la un pret mult mai prost sau directia s-a si corectat.

Practic, sentiment-ul din RSS e retrospectiv prin design — nu masori ce se intampla acum, masori ce s-a intamplat cu 2+ ore in urma.

**Ce trebuie construit:**
- Integrare cu provider real-time: Polygon.io News API (WebSocket) sau Benzinga News API
- Interval ingestie redus la 10-15 minute pentru feeds prioritare (finance, earnings)
- Deduplicare agresiva pe hash de continut (nu doar ID — acelasi articol apare pe mai multe RSS-uri)
- Rate limiting inteligent per sursa ca sa nu depasim limitele API

**Nota:** Polygon.io ~$29/luna plan basic include news. Benzinga similar. Cost recurent necesar.

**Efort:** Mediu | **Impact:** Mare — dar necesita cost recurent

---

### PRIORITATE 8 — Corelatie dinamica (nu statica 60 zile)

**De ce e problema:**
Filtrul de corelatie curent (skip daca corr ≥ 0.75 pe 60 zile) e menit sa evite over-concentration. Dar 60 zile e o fereastra structurala — masoara corelatia in conditii normale. In momente de stress (crash 2020, rate hike panic 2022), corelatiile intre aproape toate asset-urile converc spre 1: actiunile cad toate impreuna, commodity-urile cad, crypto cade. Diversificarea dispare exact cand ai nevoie de ea.

In aceste momente, doua asset-uri cu corr 0.50 structural (sub threshold, deci amandoua recomandate) pot deveni corr 0.90 real-time. Sistemul nu detecteaza aceasta schimbare si da batch-uri care par diversificate dar in realitate sunt toate corelate.

**Ce trebuie construit:**
- Corelatie pe doua ferestre: 20 zile (recent) si 60 zile (structural)
- Logica: skip daca corr recenta (20z) > 0.85 SAU corr structurala (60z) > 0.75
- `correlation_regime`: normal / elevated / stress, calculat ca media corelatiei cross-asset in portofoliu
- In `stress` regime: threshold redus la 0.60, sizing redus cu 20%

**Efort:** Mic | **Impact:** Mediu — mai important in perioade de volatilitate ridicata

---

## Metrici tehnice cu magic numbers nejustificate

Urmatoarele constante trebuie validate sau calibrate prin backtesting (PRIORITATE 1):

| Constanta | Valoare curenta | Fisier | Justificare |
|-----------|-----------------|--------|-------------|
| ATR multiplier conservative SL | 1.5x | `technicals.py` | Niciuna — standard "suna rezonabil" |
| ATR multiplier conservative TP | 2.5x | `technicals.py` | Niciuna |
| ATR multiplier moderate SL | 2.0x | `technicals.py` | Niciuna |
| ATR multiplier moderate TP | 3.5x | `technicals.py` | Niciuna |
| ATR multiplier aggressive SL | 3.0x | `technicals.py` | Niciuna |
| ATR multiplier aggressive TP | 5.0x | `technicals.py` | Niciuna |
| Correlation skip threshold | 0.75 | `engine.py` | Niciuna |
| Trend alignment bonus | +20% sizing | `engine.py` | Niciuna — nevalidat pe backtest |
| Sector alignment bonus | +10% sizing | `engine.py` | Niciuna — nevalidat pe backtest |
| Volatility contraction mult | 1.1x ATR | `technicals.py` | Niciuna |
| Volatility expansion mult | 0.95x ATR | `technicals.py` | Niciuna |
| Conflict skip threshold | 25 pts | `engine.py` | Arbitrar |
| Min Platt calibration samples | 10 | `calibration.py` | Prea mic — 30-50 recomandat |
| Sentiment half-life | 24h | `aggregate.py` | Niciuna |

---

## Ordinea recomandata de implementare

```
[DONE] Bug fixes programator + TimescaleDB
    ↓
[NEXT] Earnings calendar (rapid, impact imediat pe equity recs)
    ↓
[NEXT] Circuit breaker / daily loss limit (rapid, risk management de baza)
    ↓
[THEN] Gap risk modeling (depinde partial de earnings calendar)
    ↓
[THEN] Backtesting framework (efort mare, dar deblocheaza validarea tuturor constantelor)
    ↓
[THEN] Volume Profile (necesita backtesting ca sa validezi imbunatatirea)
    ↓
[THEN] Relative Strength (necesita backtesting ca sa validezi)
    ↓
[LAST] News latenta / Corelatie dinamica (nice to have, depinde de budget si date)
```

---

## Referinte

- `app/services/technicals.py` — indicatori, snapshot, SL/TP, confidence score
- `app/services/recommendations/engine.py` — pipeline candidati, filtre, sizing
- `app/services/calibration.py` — Platt scaling
- `app/services/analysis/aggregate.py` — agregare sentiment cu decay
- `app/services/market_context.py` — regime, corelatie, multi-TF
- `app/services/macro_calendar.py` — calendar macro, fetch automat
- `migrations/versions/001_initial_schema.py` — schema PostgreSQL + hypertables
- `scripts/migrate_sqlite_to_pg.py` — transfer date SQLite → PostgreSQL
- `docs/FUTURE_RECOMMENDATION_IMPROVEMENTS.md` — document detaliat versiune anterioara
