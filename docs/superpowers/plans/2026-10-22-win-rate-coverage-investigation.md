# Win-Rate & Recommendation-Coverage Investigation — Plan

> **STATUS:** Sub-project A (confidence-score discrimination) **CONCLUDED, no fix found** —
> see `docs/CHANGELOG.md` 2026-10-22 for the full diagnostic detail. Sub-project B (mid/small-cap
> recommendation coverage) **IMPLEMENTED and verified live** the same day — see §3 below.
> **Commit rule (CLAUDE.md):** the user commits; this doc records what was done, not a commit log.

## 1. Trigger

User observation: "the win rate is too low" and "based on market conditions, a lot of
recommendations do not reach us when it comes to mid-cap and small-cap." Decomposed into two
independent sub-projects per the brainstorming skill's guidance (different mechanisms, different
code paths, no shared dependency):

- **A — confidence score discrimination.** Does the existing confidence score (or any other
  already-computed feature) actually separate winners from losers, and can exit/risk-management
  changes improve expectancy if entry-side discrimination can't be fixed?
- **B — mid/small-cap recommendation coverage.** Why do mid/small-cap candidates reach the user so
  much less often than large-cap, and can that be fixed?

## 2. Sub-project A — confidence-score discrimination (CONCLUDED)

Full diagnostic detail (15 features tested on the entry side, the TP-widening/time-exit/SPY-alpha
analysis on the exit side, the out-of-sample check) is in `docs/CHANGELOG.md`, 2026-10-22 entry
"Confidence-score discrimination + exit/risk-management diagnostics — CONCLUDED, no improvement
found." Summary for this plan's purposes:

- Every feature this engine currently computes (9 TA indicators, relative strength, volume-profile
  positioning, OFI, properly-tested sentiment) carries no real discriminative power between
  winners and losers (max |correlation| ~0.07, several sign-wrong) across 7124 resolved trades
  from a 3-year walk-forward.
- Two new data classes tried (insider buying via SEC Forms 3/4/5, Beneish-quality-as-a-long-filter)
  also came up null or noise-level.
- Exit-side: TP-widening initially looked promising, reversed to actively harmful once measured
  correctly (resolved-only, not mixed with mark-to-market expired trades). Time-exit-with-SL looked
  positive in raw R-multiples but showed negative alpha vs. SPY once unit-matched. A narrow
  `stocks_mid` exit-timing lead partially surfaced but weakened (turned slightly negative once
  beta-adjusted) on an out-of-sample window — noted as "inconclusive," not pursued further.
- **Decision: this thread is concluded.** No further confidence-reweighting or exit-policy-tuning
  work is planned. The same "consistent null across many independent, rigorous tests" signature
  that closed the shorts investigation (`2026-09-18-shorts-engine.md`) applies here too.

## 3. Sub-project B — mid/small-cap recommendation coverage (IMPLEMENTED, VERIFIED LIVE)

### 3.1 Diagnosis

- **Root cause confirmed** (not the confidence-gate penalty originally suspected): `direction ==
  "short"` combined with `SHORTS_ENGINE_ENABLED = False` drops the candidate at `engine.py:1266`
  **before** `score_candidate` (the confidence pipeline) ever runs. `asset_class_policy.py`'s
  per-category `min_confidence_offset` and `scoring.py`'s `cap_tier_penalty` are real code but
  rarely get the chance to matter — most mid/small candidates never reach them.
- **Persistent and structural**, not a one-day fluke: measured over 251 trading days, short-rate
  scales inversely with cap size — `stocks_large` 44.3%, `stocks_mid` 54.6%, `stocks_small` 59.9%
  (peaking at 73.1% in bull regimes for small-caps specifically).
- **Why**: `determine_direction` (`technicals.py`) is pure per-symbol technicals, not regime-locked
  — a bullish SPX doesn't force individual names long. The curated `stocks_mid`/`stocks_small`
  watchlists (14 and 10 symbols before this fix) were almost entirely speculative/high-beta names
  (PLTR, SMCI, IONQ, RIVN, ENPH, FSLR, CCJ, MP, SOFI, HOOD, RIG, HP, HIMS, RKLB / BBAI, SOUN, LUNR,
  ACHR, ASTS, JOBY, ACMR, ARRY, RXRX, OPEN) — all tagged `"aggressive"`/`"moderate"`, zero
  `"conservative"`, with zero representation in financials, healthcare (non-speculative),
  industrials, consumer staples, utilities, or real estate. These chop bearish on their own
  technicals far more often than `stocks_large`'s balanced, largely profitable mix.
- **Ruled out**: re-enabling shorts for mid/small specifically. The concluded shorts investigation
  never tested a mid/small-specific universe, but given how decisively it failed across 5
  configurations/4 crisis episodes on large-caps/indices, and the follow-up research concluding
  14-day single-name shorts lack edge for any known data class, reopening that investigation for
  one category wasn't judged worth it.

### 3.2 Fix — watchlist diversification

Added 20 new symbols to `ASSETS_BY_CATEGORY` in `engine.py` (10 to `stocks_mid`, 10 to
`stocks_small`), screened for **profitability** (positive net income, SEC EDGAR XBRL-verified,
≥2 of last 3 fiscal years) and **sector balance** (filling the financials/REIT/healthcare/
industrials/utilities/consumer-staples gaps), reusing the structural-short-thesis script's ticker
pool (`MID_TICKERS`/`SMALL_TICKERS`) and SEC EDGAR extraction code. Existing names kept, not
replaced — this dilutes the composition skew and grows the universe rather than removing anything.

**Added to `stocks_mid`** (14 → 24): RNR, EWBC (financials) · NLY (REIT) · TOL, USFD, CSL, CCK
(consumer/industrials) · NJR (utilities) · UTHR, MOH (healthcare).

**Added to `stocks_small`** (10 → 20): CALM, LW (consumer defensive) · ABCB, CATY, RDN (financials)
· TRNO (REIT) · AWI, OTTR, BLDR (industrials) · ACAD (profitable biotech).

Most new entries tagged `"conservative"`/`"moderate"` risk tier (vs. the prior all-aggressive
lists) — first conservative-tier representation either watchlist has had.

**Caveat carried over from the ticker-sourcing work**: a few additions (UTHR, USFD at the large end
of "mid"; several small-cap picks in the $3-7B range) have grown past strict index-rebalance
market-cap bounds — the same cap-drift already present in the source S&P 400/600 ETF-holdings
lists this session sourced earlier. Not disqualifying for this app's purposes.

### 3.3 Verification

- `ASSETS_BY_CATEGORY['stocks_mid']` / `['stocks_small']` load correctly (24 / 20 entries); full
  unit suite (386 tests) green, no hardcoded references to the old list lengths anywhere.
- **Live batch run (2026-10-22)**: 3 of 5 `stocks_mid` candidates and the sole `stocks_small`
  candidate came from the new additions — `EWBC` (conf=66, the best mid-cap confidence that day),
  `MOH` (conf=53), `CCK` (conf=38), `TRNO` (conf=48) — on a day where the prior all-speculative
  watchlist would likely have produced little to no mid/small coverage, per the confirmed
  short-skew pattern. No data errors on any of the 20 new tickers.

### 3.4 Decision

**Implemented and verified.** No further changes planned on this thread unless future monitoring
shows the new names underperforming or the short-skew pattern persists disproportionately even
with the larger, more balanced universe — would need a multi-week live sample to assess, not done
as part of this fix.

## 4. Deferred / open threads

- `stocks_mid`'s exit-timing alpha (§2) — noted as inconclusive, not pursued; could be revisited
  with a larger/different out-of-sample window if sub-project A is ever picked up again.
- Whether the short-skew rate itself changes now that the universe is larger/more balanced — not
  measured; would need a fresh multi-week tally (same method as the original 251-day audit) after
  enough live history accumulates on the new tickers.
- Per-sector risk-tier calibration for the new additions (all assigned by judgment during screening,
  not backtested individually) — acceptable for now, same bar as the original watchlist's risk tiers.
