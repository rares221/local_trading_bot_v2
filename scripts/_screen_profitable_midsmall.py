"""
Throwaway screening script for sub-project B (mid/small-cap watchlist diversification).
Not wired into the app. Run via: docker-compose exec -T app python -m scripts._screen_profitable_midsmall
"""
from __future__ import annotations

import sys
import time
sys.path.insert(0, "/app")

from scripts.validate_structural_short_thesis import (
    MID_TICKERS, SMALL_TICKERS, fetch_ticker_to_cik, fetch_company_facts,
    _extract_annual_series,
)

CURRENT_MID = {"PLTR", "SMCI", "IONQ", "RIVN", "ENPH", "FSLR", "CCJ", "MP", "SOFI", "HOOD", "RIG", "HP", "HIMS", "RKLB"}
CURRENT_SMALL = {"BBAI", "SOUN", "LUNR", "ACHR", "ASTS", "JOBY", "ACMR", "ARRY", "RXRX", "OPEN"}

mid_candidates = sorted(set(MID_TICKERS) - CURRENT_MID)
small_candidates = sorted(set(SMALL_TICKERS) - CURRENT_SMALL)

print(f"[SCREEN] mid candidates: {len(mid_candidates)}  small candidates: {len(small_candidates)}")

ticker_to_cik = fetch_ticker_to_cik()
print(f"[SCREEN] CIK map: {len(ticker_to_cik)} entries")


def screen(tickers: list[str], label: str) -> list[dict]:
    results = []
    for i, ticker in enumerate(tickers):
        cik = ticker_to_cik.get(ticker.replace(".", "").replace("-", ""))
        if cik is None:
            cik = ticker_to_cik.get(ticker)
        if cik is None:
            continue
        facts = fetch_company_facts(cik)
        if not facts:
            continue
        ni_series = _extract_annual_series(facts, "net_income")
        if not ni_series:
            continue
        years = sorted(ni_series.keys())
        recent_years = years[-3:]
        recent_vals = [(y, ni_series[y][0]) for y in recent_years]
        most_recent_year, most_recent_val = recent_vals[-1]
        positive_count = sum(1 for _, v in recent_vals if v > 0)
        if most_recent_val > 0 and positive_count >= 2:
            results.append({
                "ticker": ticker, "cik": cik,
                "net_income_recent": recent_vals,
                "most_recent_year": most_recent_year,
                "most_recent_val": most_recent_val,
                "positive_count": positive_count,
                "n_years": len(recent_vals),
            })
        if (i + 1) % 50 == 0:
            print(f"[SCREEN][{label}] {i + 1}/{len(tickers)} processed, {len(results)} passed so far")
    return results


mid_profitable = screen(mid_candidates, "mid")
print(f"[SCREEN] mid profitable: {len(mid_profitable)}")
small_profitable = screen(small_candidates, "small")
print(f"[SCREEN] small profitable: {len(small_profitable)}")

import json
with open("/tmp/midsmall_profitable.json", "w") as f:
    json.dump({"mid": mid_profitable, "small": small_profitable}, f, default=str)
print("[SCREEN] dumped to /tmp/midsmall_profitable.json")

print("\n--- MID profitable (ticker, most_recent_year, most_recent_val, positive_count/n_years) ---")
for r in sorted(mid_profitable, key=lambda r: -r["most_recent_val"]):
    print(f"{r['ticker']:8s} {r['most_recent_year']} {r['most_recent_val']:>18,.0f}  {r['positive_count']}/{r['n_years']}")

print("\n--- SMALL profitable (ticker, most_recent_year, most_recent_val, positive_count/n_years) ---")
for r in sorted(small_profitable, key=lambda r: -r["most_recent_val"]):
    print(f"{r['ticker']:8s} {r['most_recent_year']} {r['most_recent_val']:>18,.0f}  {r['positive_count']}/{r['n_years']}")
