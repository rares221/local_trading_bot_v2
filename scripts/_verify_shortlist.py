"""Throwaway: verify sector + liquidity for the sub-project B candidate shortlist via yfinance."""
import yfinance as yf

MID_SHORTLIST = ["RNR", "NLY", "EWBC", "TOL", "UTHR", "USFD", "CSL", "NJR", "CCK", "MOH"]
SMALL_SHORTLIST = ["CALM", "LW", "ABCB", "CATY", "RDN", "TRNO", "AWI", "OTTR", "BLDR", "ACAD"]

for label, tickers in [("MID", MID_SHORTLIST), ("SMALL", SMALL_SHORTLIST)]:
    print(f"\n--- {label} ---")
    for t in tickers:
        try:
            tk = yf.Ticker(t)
            info = tk.info
            name = info.get("longName") or info.get("shortName")
            sector = info.get("sector")
            industry = info.get("industry")
            avg_vol = info.get("averageVolume") or info.get("averageDailyVolume10Day")
            mcap = info.get("marketCap")
            print(f"{t:6s} {name!s:40.40s} sector={sector!s:22.22s} industry={industry!s:28.28s} "
                  f"avgVol={avg_vol!s:>12} mcap={mcap!s:>15}")
        except Exception as e:
            print(f"{t:6s} ERROR: {e}")
