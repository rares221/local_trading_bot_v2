"""
Throwaway diagnostic (read-only, no app code touched): test two NEW data classes against the
same 7124-trade outcome set from backtest run 062af0ff that 15 existing engine features already
failed to discriminate on (see docs/CHANGELOG.md / prior diagnose_confidence_*.py scripts):

  1. Beneish M-score / Sloan accruals (reusing scripts/validate_structural_short_thesis.py's
     already-built-and-validated SEC EDGAR XBRL extraction) as a LONG-side quality feature --
     does a company's most-recent-available fiscal-year quality score (as of the trade's entry
     date, respecting the same 90-day formation lag to avoid lookahead) correlate with whether
     THIS engine's actual 14-day long trade won or lost?
  2. Insider buying (Forms 3/4/5, SEC's structured quarterly bulk dataset) -- does net insider
     buying in the N days before entry correlate with outcome?

Run: docker-compose exec -T app python -m scripts.diagnose_confidence_quality_insider
"""
from __future__ import annotations

import io
import math
import zipfile
from datetime import date, datetime, timedelta

import requests
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from scripts.validate_structural_short_thesis import (
    FORMATION_LAG_DAYS,
    SEC_USER_AGENT,
    _extract_annual_series,
    _sec_get,
    compute_scores_for_year,
    fetch_company_facts,
    fetch_ticker_to_cik,
)

RUN_ID = "062af0ff-8a44-44ed-bbc6-e691280db486"


def point_biserial(points: list[float], outcomes: list[int]) -> float:
    n = len(points)
    if n < 10:
        return float("nan")
    mean_p = sum(points) / n
    mean_o = sum(outcomes) / n
    var_p = sum((p - mean_p) ** 2 for p in points)
    var_o = sum((o - mean_o) ** 2 for o in outcomes)
    if var_p == 0 or var_o == 0:
        return float("nan")
    cov = sum((points[i] - mean_p) * (outcomes[i] - mean_o) for i in range(n))
    return cov / math.sqrt(var_p * var_o)


def load_trades():
    sync_url = settings.database_url.replace("+aiosqlite", "").replace("+asyncpg", "")
    engine = sa.create_engine(sync_url)
    Session = sessionmaker(bind=engine)
    s = Session()
    rows = s.execute(sa.text(
        "select symbol, asset_category, entry_date, outcome, pnl_r from backtest_trades "
        "where run_id=:rid and outcome in ('hit_tp','hit_sl') and direction='long' "
        "and asset_category in ('stocks_large','stocks_mid','stocks_small')"
    ), {"rid": RUN_ID}).fetchall()
    s.close()
    return rows


# ---------------------------------------------------------------------------
# 1. Beneish M-score / Sloan accruals as a long-side quality feature
# ---------------------------------------------------------------------------

def run_quality_test(rows):
    print(f"\n{'=' * 90}\n1. BENEISH M-SCORE / SLOAN ACCRUALS AS LONG-SIDE QUALITY FEATURE\n{'=' * 90}")
    symbols = sorted({r.symbol for r in rows})
    print(f"[QUALITY] {len(rows)} long trades, {len(symbols)} distinct stock symbols")

    ticker_to_cik = fetch_ticker_to_cik()
    print(f"[QUALITY] SEC ticker->CIK mapping: {len(ticker_to_cik)} entries")

    annual_by_symbol: dict[str, dict] = {}
    for i, ticker in enumerate(symbols):
        cik = ticker_to_cik.get(ticker.replace(".", "").replace("-", "")) or ticker_to_cik.get(ticker)
        if cik is None:
            print(f"[QUALITY]   {ticker}: no CIK match, skipping")
            continue
        facts = fetch_company_facts(cik)
        if not facts:
            print(f"[QUALITY]   {ticker}: no companyfacts data, skipping")
            continue
        from scripts.validate_structural_short_thesis import _TAG_CANDIDATES
        annual = {key: _extract_annual_series(facts, key) for key in _TAG_CANDIDATES}
        available_years = sorted(annual.get("total_assets", {}).keys())
        if not available_years:
            print(f"[QUALITY]   {ticker}: no usable fiscal years, skipping")
            continue
        annual_by_symbol[ticker] = {"annual": annual, "years": available_years}
        print(f"[QUALITY]   {ticker}: {len(available_years)} fiscal years available "
              f"({available_years[0]}-{available_years[-1]})")

    records = []
    skipped_no_company = 0
    skipped_no_fy = 0
    skipped_no_score = 0
    score_cache: dict[tuple[str, int], dict | None] = {}

    for r in rows:
        entry = r.entry_date
        company = annual_by_symbol.get(r.symbol)
        if company is None:
            skipped_no_company += 1
            continue
        # Most recent fiscal year whose data would actually have been public by entry date
        # (period_end + 90-day formation lag <= entry_date), same lookahead guard as the
        # original short-thesis script.
        eligible_fy = None
        for fy in sorted(company["years"], reverse=True):
            pair = company["annual"].get("total_assets", {}).get(fy)
            if pair is None:
                continue
            period_end = pair[1]
            if period_end + timedelta(days=FORMATION_LAG_DAYS) <= entry:
                eligible_fy = fy
                break
        if eligible_fy is None:
            skipped_no_fy += 1
            continue
        cache_key = (r.symbol, eligible_fy)
        if cache_key not in score_cache:
            score_cache[cache_key] = compute_scores_for_year(company["annual"], eligible_fy)
        scored = score_cache[cache_key]
        if scored is None:
            skipped_no_score += 1
            continue
        win = 1 if r.outcome == "hit_tp" else 0
        records.append({
            "symbol": r.symbol, "category": r.asset_category, "win": win, "pnl_r": r.pnl_r,
            "m_score": scored["m_score"], "sloan_accruals": scored["sloan_accruals"],
            "fiscal_year": eligible_fy,
        })

    print(f"\n[QUALITY] matched={len(records)}  skipped_no_company={skipped_no_company}  "
          f"skipped_no_eligible_fy={skipped_no_fy}  skipped_no_score={skipped_no_score}")

    if len(records) < 10:
        print("[QUALITY] Not enough matched records to correlate.")
        return

    outcomes = [rec["win"] for rec in records]
    m_scores = [rec["m_score"] for rec in records]
    sloan = [rec["sloan_accruals"] for rec in records]

    corr_m = point_biserial(m_scores, outcomes)
    corr_sloan = point_biserial(sloan, outcomes)
    overall_wr = sum(outcomes) / len(outcomes)

    print(f"\nOverall WR (matched quality-scored long trades): {overall_wr:.1%}  n={len(records)}")
    print(f"  m_score         corr={corr_m:+.3f}  (negative expected if 'good quality'/low "
          f"M-score predicts winning)")
    print(f"  sloan_accruals  corr={corr_sloan:+.3f}")

    cats = sorted({rec["category"] for rec in records})
    print("\n-- Category breakdown --")
    for cat in cats:
        sub = [rec for rec in records if rec["category"] == cat]
        if len(sub) < 20:
            print(f"  {cat:<14} n={len(sub):<6} (too small to report)")
            continue
        pts_m = [rec["m_score"] for rec in sub]
        pts_s = [rec["sloan_accruals"] for rec in sub]
        outs = [rec["win"] for rec in sub]
        print(f"  {cat:<14} n={len(sub):<6} m_score_corr={point_biserial(pts_m, outs):+.3f}  "
              f"sloan_corr={point_biserial(pts_s, outs):+.3f}")


# ---------------------------------------------------------------------------
# 2. Insider buying (SEC Forms 3/4/5 structured bulk dataset)
# ---------------------------------------------------------------------------

def _quarter_of(d: date) -> tuple[int, int]:
    return d.year, (d.month - 1) // 3 + 1


def _fetch_insider_quarter(year: int, q: int) -> list[dict] | None:
    """Download+parse one quarter's Forms 3/4/5 structured dataset
    (NONDERIV_TRANS.tsv: transaction-level, non-derivative). Returns list of
    {issuer_ticker, trans_date, trans_code, shares, price} dicts, or None on failure."""
    url = f"https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{year}q{q}_form345.zip"
    try:
        resp = requests.get(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=60)
    except Exception as e:
        print(f"[INSIDER]   {year}Q{q}: request failed ({e})")
        return None
    if resp.status_code != 200:
        print(f"[INSIDER]   {year}Q{q}: HTTP {resp.status_code} at {url}")
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
    except Exception as e:
        print(f"[INSIDER]   {year}Q{q}: not a valid zip ({e})")
        return None
    names = zf.namelist()
    nonderiv_name = next((n for n in names if "NONDERIV_TRANS" in n.upper()), None)
    if nonderiv_name is None:
        print(f"[INSIDER]   {year}Q{q}: no NONDERIV_TRANS file in {names[:10]}")
        return None
    with zf.open(nonderiv_name) as f:
        header = f.readline().decode("latin-1").rstrip("\n").split("\t")
        try:
            idx_date = header.index("TRANS_DATE")
            idx_code = header.index("TRANS_CODE")
            idx_shares = header.index("TRANS_SHARES")
            idx_acc = header.index("ACCESSION_NUMBER")
        except ValueError as e:
            print(f"[INSIDER]   {year}Q{q}: unexpected header {header[:15]} ({e})")
            return None
        out = []
        for line in f:
            parts = line.decode("latin-1").rstrip("\n").split("\t")
            if len(parts) <= max(idx_date, idx_code, idx_shares, idx_acc):
                continue
            out.append({
                "accession": parts[idx_acc], "trans_date": parts[idx_date],
                "trans_code": parts[idx_code], "trans_shares": parts[idx_shares],
            })
    return out


def run_insider_test(rows):
    print(f"\n{'=' * 90}\n2. INSIDER BUYING (SEC FORMS 3/4/5 BULK DATASET)\n{'=' * 90}")
    dates = [r.entry_date for r in rows]
    min_d, max_d = min(dates), max(dates)
    print(f"[INSIDER] trade window: {min_d} .. {max_d}")

    # Probe the dataset's actual URL/format before committing to downloading the full range.
    probe_year, probe_q = _quarter_of(min_d)
    probe = _fetch_insider_quarter(probe_year, probe_q)
    if probe is None:
        print("[INSIDER] Could not fetch/parse the structured bulk dataset for the probe quarter "
              f"({probe_year}Q{probe_q}). Insider-buying test ABORTED -- see note in final report.")
        return

    print(f"[INSIDER] Probe quarter {probe_year}Q{probe_q}: {len(probe)} non-derivative "
          f"transaction rows. Format confirmed usable. Proceeding to fetch all quarters.")

    # ACCESSION_NUMBER alone doesn't carry the issuer ticker in NONDERIV_TRANS -- need
    # SUBMISSION.tsv (issuer CIK/ticker per accession) joined in. Check once on the probe
    # quarter before committing to the full multi-quarter join.
    url = f"https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{probe_year}q{probe_q}_form345.zip"
    resp = requests.get(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=60)
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = zf.namelist()
    print(f"[INSIDER] Files in dataset zip: {names}")
    sub_name = next((n for n in names if "SUBMISSION" in n.upper()), None)
    if sub_name is None:
        print("[INSIDER] No SUBMISSION file found to join issuer ticker -- ABORTED.")
        return
    with zf.open(sub_name) as f:
        header = f.readline().decode("latin-1").rstrip("\n").split("\t")
        print(f"[INSIDER] SUBMISSION header: {header}")
        idx_acc = header.index("ACCESSION_NUMBER") if "ACCESSION_NUMBER" in header else None
        idx_ticker = None
        for cand in ("ISSUERTRADINGSYMBOL", "ISSUER_TRADING_SYMBOL", "TICKER"):
            if cand in header:
                idx_ticker = header.index(cand)
                break
        if idx_acc is None or idx_ticker is None:
            print(f"[INSIDER] Could not locate ticker/accession columns in SUBMISSION header "
                  f"-- ABORTED. idx_acc={idx_acc} idx_ticker={idx_ticker}")
            return
        acc_to_ticker: dict[str, str] = {}
        for line in f:
            parts = line.decode("latin-1").rstrip("\n").split("\t")
            if len(parts) <= max(idx_acc, idx_ticker):
                continue
            tkr = parts[idx_ticker].strip().upper()
            if tkr:
                acc_to_ticker[parts[idx_acc]] = tkr
    print(f"[INSIDER] Probe quarter: {len(acc_to_ticker)} accession->ticker mappings resolved.")

    # Full fetch across all quarters spanned by the trade window.
    quarters = []
    y, q = _quarter_of(min_d)
    end_y, end_q = _quarter_of(max_d)
    while (y, q) <= (end_y, end_q):
        quarters.append((y, q))
        q += 1
        if q > 4:
            q = 1
            y += 1

    symbols_needed = {r.symbol for r in rows}
    buy_sell_by_ticker_date: dict[str, list[tuple[date, int, float]]] = {}  # ticker -> [(date, net_shares, 0)]

    for (yy, qq) in quarters:
        print(f"[INSIDER] Fetching {yy}Q{qq}...")
        url = f"https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{yy}q{qq}_form345.zip"
        try:
            resp = requests.get(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=90)
        except Exception as e:
            print(f"[INSIDER]   {yy}Q{qq}: request failed ({e}), skipping quarter")
            continue
        if resp.status_code != 200:
            print(f"[INSIDER]   {yy}Q{qq}: HTTP {resp.status_code}, skipping quarter")
            continue
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = zf.namelist()
        sub_name = next((n for n in names if "SUBMISSION" in n.upper()), None)
        nd_name = next((n for n in names if "NONDERIV_TRANS" in n.upper()), None)
        if sub_name is None or nd_name is None:
            print(f"[INSIDER]   {yy}Q{qq}: missing SUBMISSION or NONDERIV_TRANS, skipping")
            continue
        acc_to_ticker = {}
        with zf.open(sub_name) as f:
            header = f.readline().decode("latin-1").rstrip("\n").split("\t")
            if "ACCESSION_NUMBER" not in header:
                continue
            idx_acc = header.index("ACCESSION_NUMBER")
            idx_ticker = None
            for cand in ("ISSUERTRADINGSYMBOL", "ISSUER_TRADING_SYMBOL", "TICKER"):
                if cand in header:
                    idx_ticker = header.index(cand)
                    break
            if idx_ticker is None:
                continue
            for line in f:
                parts = line.decode("latin-1").rstrip("\n").split("\t")
                if len(parts) <= max(idx_acc, idx_ticker):
                    continue
                tkr = parts[idx_ticker].strip().upper()
                if tkr in symbols_needed:
                    acc_to_ticker[parts[idx_acc]] = tkr
        n_rows_kept = 0
        with zf.open(nd_name) as f:
            header = f.readline().decode("latin-1").rstrip("\n").split("\t")
            try:
                idx_acc2 = header.index("ACCESSION_NUMBER")
                idx_date = header.index("TRANS_DATE")
                idx_code = header.index("TRANS_CODE")
                idx_shares = header.index("TRANS_SHARES")
            except ValueError:
                continue
            for line in f:
                parts = line.decode("latin-1").rstrip("\n").split("\t")
                if len(parts) <= max(idx_acc2, idx_date, idx_code, idx_shares):
                    continue
                acc = parts[idx_acc2]
                tkr = acc_to_ticker.get(acc)
                if tkr is None:
                    continue
                code = parts[idx_code].strip().upper()
                if code not in ("P", "S"):  # P=open-market purchase, S=open-market sale
                    continue
                try:
                    tdate = datetime.strptime(parts[idx_date], "%d-%b-%Y").date()
                except ValueError:
                    try:
                        tdate = datetime.strptime(parts[idx_date], "%Y-%m-%d").date()
                    except ValueError:
                        continue
                try:
                    shares = float(parts[idx_shares])
                except ValueError:
                    continue
                signed = shares if code == "P" else -shares
                buy_sell_by_ticker_date.setdefault(tkr, []).append((tdate, signed, 0.0))
                n_rows_kept += 1
        print(f"[INSIDER]   {yy}Q{qq}: {n_rows_kept} matching P/S transaction rows kept "
              f"for our {len(symbols_needed)} symbols")

    total_tx = sum(len(v) for v in buy_sell_by_ticker_date.values())
    print(f"\n[INSIDER] Total insider P/S transactions collected across all symbols: {total_tx}")
    if total_tx == 0:
        print("[INSIDER] No insider transaction data collected for any of our 46 symbols -- "
              "cannot test this signal. Reporting as ABORTED/no-coverage, not as a null result.")
        return

    for window_days in (30, 60, 90):
        records = []
        for r in rows:
            txs = buy_sell_by_ticker_date.get(r.symbol)
            if not txs:
                continue
            cutoff_start = r.entry_date - timedelta(days=window_days)
            net = sum(signed for (d, signed, _) in txs if cutoff_start <= d < r.entry_date)
            n_tx = sum(1 for (d, signed, _) in txs if cutoff_start <= d < r.entry_date)
            if n_tx == 0:
                continue
            win = 1 if r.outcome == "hit_tp" else 0
            records.append({"symbol": r.symbol, "category": r.asset_category, "win": win,
                             "net_shares": net, "n_tx": n_tx})
        if len(records) < 10:
            print(f"\n[INSIDER] window={window_days}d: only {len(records)} trades with any "
                  f"insider activity in-window -- too few to correlate.")
            continue
        outs = [rec["win"] for rec in records]
        nets = [rec["net_shares"] for rec in records]
        corr = point_biserial(nets, outs)
        overall_wr = sum(outs) / len(outs)
        print(f"\n[INSIDER] window={window_days}d: n={len(records)}  overall_WR={overall_wr:.1%}  "
              f"corr(net_insider_shares, outcome)={corr:+.3f}")
        cats = sorted({rec["category"] for rec in records})
        for cat in cats:
            sub = [rec for rec in records if rec["category"] == cat]
            if len(sub) < 15:
                continue
            print(f"    {cat:<14} n={len(sub):<6} corr={point_biserial([x['net_shares'] for x in sub], [x['win'] for x in sub]):+.3f}")


def main():
    rows = load_trades()
    print(f"[DIAG] Loaded {len(rows)} long stock trades (stocks_large/mid/small) from run {RUN_ID}")
    run_quality_test(rows)
    run_insider_test(rows)


if __name__ == "__main__":
    main()
