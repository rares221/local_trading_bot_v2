"""
Structural short thesis — validation-only research script (Phase A+C of the brainstormed
design, see docs/superpowers/plans/2026-09-23-structural-short-thesis.md).

Question: does a Beneish M-score / Sloan-accruals screen on SEC EDGAR XBRL fundamentals
actually predict 12-month forward underperformance for a broad universe of US equities?
If not, nothing downstream (candidate generation, UI, paid data) is worth building.

Methodology (as brainstormed and approved):
  - Universe: TODAY's S&P 500 constituents (~503 tickers) plus mid/small-cap names sourced
    from IJH/IJR ETF holdings (~936 tickers combined), applied retroactively back to 2009.
    This is SURVIVORSHIP-BIASED (delisted/bankrupt names -- exactly what a distress screen
    should catch -- are excluded). That bias works AGAINST finding an effect, so if one shows
    up anyway, that's a meaningful signal; if nothing shows up, we've saved the much larger
    effort of sourcing point-in-time historical index membership before learning that.
    (Pass 1, S&P 500 only: 66.7% of years correct direction but avg spread -3.19%/yr, below the
    -5%/yr bar. Pass 2, S&P 500 + mid + small: spread flipped to +3.38%/yr and direction fell to
    40% -- small-cap junk rallies in crisis-recovery years (2009, 2020) appear to swamp the
    signal. Pass 3 (this one): controlling for size by dropping small-caps, keeping S&P 500 +
    mid-cap only.)
  - Data: SEC EDGAR XBRL "companyfacts" API (free, no key -- one call per company returns its
    entire historical XBRL fact history) for the 8 Beneish M-score inputs + Sloan accruals.
    yfinance for forward price returns (already depended on elsewhere in this app).
  - Formation date: fiscal-year-end + 90 calendar days, approximating the real-world 10-K
    filing/dissemination lag -- using the raw fiscal-year-end date directly would look ahead
    into information not yet public at that time.
  - Success bar (matches the discipline used for the shorts validation): the worst-scoring
    quintile underperforms the best-scoring quintile by >=5%/year on average over the
    following ~12 months, AND that direction holds in >=60% of individual formation years.

No DB tables, no API endpoints, no UI -- this never touches the live app. Run inside the
container:
    docker-compose exec -T app python -m scripts.validate_structural_short_thesis
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from typing import Optional

import requests

SP500_TICKERS = sorted(set("""
MMM,AOS,ABT,ABBV,ACN,ADBE,AMD,AES,AFL,A,APD,ABNB,AKAM,ALB,ARE,ALGN,ALLE,LNT,ALL,GOOGL,GOOG,MO,
AMZN,AMCR,AEE,AEP,AXP,AIG,AMT,AWK,AMP,AME,AMGN,APH,ADI,AON,APA,APO,AAPL,AMAT,APP,APTV,ACGL,ADM,
ARES,ANET,AJG,AIZ,T,ATO,ADSK,ADP,AZO,AVY,AXON,BKR,BALL,BAC,BAX,BDX,BRK.B,BBY,TECH,BIIB,BLK,BX,
XYZ,BE,BNY,BA,BKNG,BSX,BMY,AVGO,BR,BRO,BF.B,BG,BXP,CHRW,CDNS,CPT,COF,CAH,CCL,CARR,CVNA,CASY,CAT,
CBOE,CBRE,CDW,COR,CNC,CNP,CF,CRL,SCHW,CHTR,CVX,CMG,CB,CHD,CIEN,CI,CINF,CTAS,CSCO,C,CFG,CLX,CME,
CMS,KO,CTSH,COHR,COIN,CL,CMCSA,FIX,COP,ED,STZ,CEG,COO,CPRT,GLW,CPAY,CTVA,CSGP,COST,CRH,CRWD,CCI,
CSX,CMI,CVS,DHR,DRI,DDOG,DVA,DECK,DE,DELL,DAL,DVN,DXCM,FANG,DLR,DG,DLTR,D,DPZ,DASH,DOV,DOW,DHI,
DTE,DUK,DD,ETN,EBAY,ECHO,ECL,EIX,EW,ELV,EME,EMR,ETR,EOG,EQT,EFX,EQIX,ERIE,ESS,EL,EG,EVRG,P,ES,
EXC,EXE,EXPE,EXPD,EXR,XOM,FFIV,FDS,FICO,FAST,FRT,FDX,FDXF,FERG,FIS,FITB,FSLR,FE,FISV,FLEX,F,
FTNT,FTV,FOXA,FOX,BEN,FCX,GRMN,IT,GE,GEHC,GEV,GEN,GNRC,GD,GIS,GM,GPC,GILD,GPN,GL,GDDY,GS,HAL,
HIG,HAS,HCA,DOC,HSIC,HSY,HPE,HLT,HD,HONA,HON,HRL,HST,HWM,HPQ,HUBB,HUM,HBAN,HII,IBM,IEX,IDXX,ITW,
ILMN,INCY,IR,PODD,INTC,IBKR,ICE,IFF,IP,INTU,ISRG,IVZ,INVH,IQV,IRM,JBHT,JBL,JKHY,J,JNJ,JCI,JPM,
KVUE,KDP,KEY,KEYS,KMB,KIM,KMI,KKR,KLAC,KHC,KR,LHX,LH,LRCX,LVS,LDOS,LEN,LII,LLY,LIN,LYV,LMT,L,LOW,
LULU,LITE,LYB,MTB,MPC,MAR,MRSH,MLM,MRVL,MAS,MA,MKC,MCD,MCK,MDT,MRK,META,MET,MTD,MGM,MCHP,MU,MSFT,
MAA,MRNA,MDLZ,MPWR,MNST,MCO,MS,MOS,MSI,MSCI,NDAQ,NTAP,NFLX,NEM,NWSA,NWS,NEE,NKE,NI,NDSN,NSC,NTRS,
NOC,NCLH,NRG,NUE,NVDA,NVR,NXPI,ORLY,OXY,ODFL,OMC,ON,OKE,ORCL,OTIS,PCAR,PKG,PLTR,PANW,PSKY,PH,
PAYX,PYPL,PNR,PEP,PFE,PCG,PM,PSX,PNW,PNC,PPG,PPL,PFG,PG,PGR,PLD,PRU,PEG,PTC,PSA,PHM,PWR,QCOM,DGX,
Q,RL,RJF,RDDT,RTX,O,REG,REGN,RF,RSG,RMD,RVTY,HOOD,ROK,ROL,ROP,ROST,RCL,SPGI,CRM,SNDK,SBAC,SLB,
STX,SRE,NOW,SHW,SPG,SWKS,SJM,SW,SNA,SOLV,SO,LUV,SWK,SBUX,STT,STLD,STE,SYK,SMCI,SYF,SNPS,SYY,TMUS,
TROW,TTWO,TPR,TRGP,TGT,TEL,TDY,TER,TSLA,TXN,TPL,TXT,TMO,TJX,TKO,TSCO,TT,TDG,TRV,TRMB,TFC,TYL,TSN,
USB,UBER,UDR,ULTA,UNP,UAL,UPS,URI,UNH,UHS,VLO,VEEV,VTR,VLTO,VRSN,VRSK,VZ,VRTX,VRT,VTRS,VICI,V,
VST,VMRK,VMC,WRB,GWW,WAB,WMT,DIS,WBD,WM,WAT,WEC,WFC,WELL,WST,WDC,WY,WSM,WMB,WTW,WDAY,WYNN,XEL,
XYL,YUM,ZBRA,ZBH,ZTS
""".replace("\n", "").split(",")))

# Mid-cap (S&P 400 / IJH holdings) constituents, added to widen the universe past large-caps
# only. Sourced from iShares' public holdings CSV (ishares.com/us/products/239763) since
# Wikipedia's table truncates and GitHub/slickcharts mirrors were unavailable (404/403). Same
# survivorship-bias caveat as SP500_TICKERS: today's constituents applied retroactively.
MID_TICKERS = sorted(set("""
AA,AAL,AAON,ACI,ACM,ADC,AEIS,AFG,AGCO,AGNC,AHR,AIT,ALGM,ALK,ALLY,ALSN,ALV,AM,AMG,AMH,AMKR,AN,ANF,
APG,APPF,AR,ARMK,ARW,ARWR,ASB,ASH,ATI,ATR,AVAV,AVNT,AVT,AVTR,AXTA,AYI,BAH,BBWI,BC,BCO,BDC,BHF,BILL,
BIO,BJ,BKH,BMRN,BRKR,BROS,BRX,BSY,BTSG,BURL,BWA,BWXT,BYD,CACI,CAR,CART,CAVA,CBSH,CBT,CCK,CDE,CDP,
CELH,CFR,CG,CGNX,CHDN,CHE,CHH,CHRD,CHWY,CLF,CLH,CMC,CNH,CNM,CNO,CNX,COKE,COLB,COLM,CORT,CR,CRBG,
CROX,CRS,CRUS,CSL,CTRE,CUBE,CUZ,CVLT,CW,CXT,CYTK,DAR,DBX,DCI,DINO,DKS,DLB,DOCN,DOCS,DOCU,DT,DTM,
DUOL,DY,EAT,EEFT,EGP,EHC,ELAN,ELF,ELS,ENS,ENSG,ENTG,EPR,EQH,ESAB,ESNT,EVR,EWBC,EXEL,EXLS,EXP,EXPO,
FAF,FBIN,FCFS,FCN,FFIN,FHI,FHN,FIVE,FLG,FLR,FLS,FN,FNB,FND,FNF,FOUR,FR,FTI,G,GAP,GATX,GBCI,GEF,GGG,
GHC,GLPI,GME,GMED,GNTX,GPK,GWRE,GXO,H,HAE,HALO,HGV,HIMS,HL,HLI,HLNE,HOG,HOMB,HQY,HR,HRB,HUBS,HWC,
HXL,IBOC,IDA,IDCC,IESC,INGR,IPGP,IRT,ITT,JAZZ,JEF,JLL,KBH,KBR,KD,KEX,KNF,KNSL,KNX,KRC,KRG,KRYS,
KTOS,LAD,LAMR,LEA,LECO,LFUS,LIVN,LNTH,LOPE,LPX,LSCC,LSTR,M,MANH,MAT,MEDP,MIDD,MKSI,MLI,MMS,MOG.A,
MOH,MORN,MP,MSA,MSM,MTDR,MTG,MTN,MTSI,MTZ,MUR,MUSA,MZTI,NBIX,NEU,NFG,NJR,NLY,NNN,NOV,NOVT,NTNX,
NVST,NVT,NWE,NXST,NXT,NYT,OC,OGE,OGS,OHI,OKTA,OLED,OLLI,OLN,ONB,ONTO,OPCH,ORA,ORI,OSK,OVV,OZK,PAG,
PATH,PB,PBF,PCTY,PEGA,PEN,PFGC,PII,PINS,PK,PLNT,PNFP,POR,POST,PPC,PR,PRI,PSN,PVH,QLYS,R,RBA,RBC,
REXR,RGA,RGEN,RGLD,RH,RLI,RMBS,RNR,ROIV,ROKU,RPM,RRC,RRX,RS,RYAN,RYN,SAIA,SAIC,SANM,SARO,SBRA,SCI,
SEIC,SF,SFM,SGI,SHC,SIGI,SIRI,SITM,SLAB,SLGN,SLM,SMG,SMTC,SN,SNX,SOLS,SON,SPXC,SR,SSB,SSD,ST,STAG,
STRL,STWD,SUI,SWX,SYNA,TCBI,TEX,THC,THG,THO,TKR,TLN,TNL,TOL,TOST,TREX,TRU,TTC,TTEK,TTMI,TWLO,TXNM,
TXRH,UBSI,UFPI,UGI,ULS,UMBF,UNM,USFD,UTHR,VAL,VC,VFC,VIAV,VICR,VLY,VMI,VNO,VNOM,VNT,VOYA,VVV,WAL,
WCC,WEX,WFRD,WH,WHR,WING,WLK,WMG,WMS,WPC,WSO,WTFC,WTRG,WTS,WWD,XPO,XRAY,YETI,ZION
""".replace("\n", "").split(",")))

# Small-cap (S&P 600 / IJR holdings, ~536 cleaned tickers, cut off before "P" alphabetically --
# see IJR fetch notes) is DELIBERATELY EXCLUDED from UNIVERSE_TICKERS below. The first full-universe
# run (S&P 500 + mid + small, 1439 tickers) flipped the average spread positive (+3.4%/yr vs.
# -3.2%/yr for S&P 500 alone) and dropped direction-consistency to 40% (from 66.7%) -- small-cap
# "junk rallies" in crisis-recovery years (2009, 2020) appear to swamp the fundamental-quality
# signal. This SMALL_TICKERS list is kept for reference / a possible future isolated small-cap
# analysis, but is not part of the active validation universe.
SMALL_TICKERS = sorted(set("""
AAMI,AAP,AAT,ABCB,ABG,ABM,ACA,ACAD,ACHC,ACIW,ACLS,ACMR,ACT,ADEA,ADIG,ADMA,ADNT,ADT,ADUS,AEO,AESI,AGO,AGX,AGYS,AIN,AIR,AKR,ALG,ALGT,ALKS,ALRM,AMN,AMR,AMRX,AMTM,ANDE,ANIP,AORT,APAM,APLE,ARCB,ARLO,AROC,ARQT,ARR,ASO,ASTE,ASTH,ATEN,ATMU,ATRC,AUB,AVA,AWI,AWR,AX,AXTI,AZTA,AZZ,BANC,BANF,BANR,BBT,BCC,BCPC,BFAM,BFH,BGC,BHE,BJRI,BKE,BKU,BL,BLDR,BLFS,BLKB,BMI,BNL,BOH,BOOT,BOX,BRC,BTU,BXMT,CACC,CAG,CAKE,CALM,CALX,CALY,CARG,CATY,CBRL,CBU,CC,CCS,CE,CENTA,CENX,CERT,CFFN,CHCO,CHEF,CLSK,CNK,CNMD,CNR,CNS,CNXC,CNXN,COCO,COHU,CON,COTY,CPB,CPF,CPK,CPRI,CRC,CRGY,CRI,CRK,CRVL,CSW,CTS,CUBI,CURB,CVBF,CVCO,CVI,CVSA,CWEN,CWK,CWST,CWT,CXW,CZR,DAN,DAVE,DBD,DCH,DCOM,DEA,DEI,DFIN,DGII,DIOD,DK,DLX,DMC,DNOW,DORM,DRH,DV,DXPE,EBC,ECG,ECPG,EFC,EFOR,EMN,ENOV,ENPH,ENR,ENVA,EPAC,EPAM,EPC,EPRT,ESE,ESI,ETSY,EVTC,EXTR,EYE,EZPW,FA,FBK,FBNC,FBP,FCF,FCPT,FELE,FFBC,FHB,FIBK,FIVN,FLO,FMC,FORM,FRPT,FSS,FTDR,FTRE,FUL,FULT,FUN,GBX,GEO,GFF,GIII,GKOS,GNL,GNW,GO,GOLF,GPI,GPOR,GRBK,GSHD,GT,GTES,GTY,GVA,HAFC,HASI,HAYW,HCC,HCI,HCSG,HE,HFWA,HIW,HLIT,HMN,HNI,HOPE,HOS,HP,HRI,HRMY,HTH,HTO,HUBG,HWKN,HZO,IART,IBP,ICHR,ICUI,IIPR,INDB,INDV,INSP,INSW,INVA,INVX,IOSP,IPAR,IRDM,ITGR,ITRI,IVT,JBLU,JBTM,JJSF,JOE,JXN,KAI,KALU,KFY,KGS,KLIC,KMPR,KMT,KMX,KN,KNTK,KRMN,KSS,KTB,KWR,LAUR,LAZ,LBRT,LCII,LEU,LFST,LGIH,LGND,LIF,LKFN,LKQ,LMAT,LNC,LNN,LPG,LQDA,LQDT,LRN,LTC,LTH,LUMN,LW,LXP,LYFT,LZB,MAC,MAN,MARA,MATX,MBC,MBGL,MBIN,MC,MCRI,MCY,MD,MDU,MFP,MGEE,MGY,MHK,MHO,MIR,MKTX,MLKN,MMSI,MPT,MRCY,MRP,MSEX,MSGS,MTCH,MTH,MTRN,MTX,MWA,MXL,MYRG,NATL,NBHC,NBTB,NE,NEO,NEOG,NGVT,NHC,NHI,NIC,NMIH,NOG,NPO,NSIT,NSP,NSSC,NTCT,NTST,NWBI,NWL,NWN,OFG,OGN,OI,OII,OMCL,OPLN,OSIS,OSW,OTTR,OUT,PARR,PATK,PAYC,PAYO,PBH,PBI,PCRX,PDFS,PEB,PECO,PENG,PENN,PFBC,PFS,PGNY,PHIN,PI,PIPR,PJT,PLMR,PLUS,PLXS,POOL,POWI,POWL,PPLI,PRDO,PRG,PRGO,PRGS,PRIM,PRK,PRLB,PRSU,PRVA,PSMT,PTCT,PTEN,PTGX,PTON,QRVO,QTWO,RAL,RAMP,RCUS,RDN,RDNT,RELY,REX,REYN,REZI,RHI,RHP,RITM,RNG,RNST,ROAD,ROCK,ROG,RRR,RSI,RUN,RUSHA,RXO,SAFT,SAM,SBCF,SBH,SCL,SCSC,SEDG,SEI,SEZL,SFBS,SFNC,SHAK,SHO,SHOO,SIG,SKT,SKY,SKYW,SLG,SLVM,SM,SNDR,SNEX,SONO,SPHR,SPNT,SPSC,SRPT,STC,STEP,STRA,SUPN,SXI,SXT,TALO,TAP,TBBK,TDC,TDS,TDW,TENB,TFIN,TFX,TGTX,THRM,TILE,TMDX,TMP,TNC,TNDM,TPC,TRIP,TRMK,TRN,TRNO,TRST,TTD,UCB,UCTT,UE,UFCS,UFPT,UNF,UNFI,UNIT,UPWK,URBN,USLM,USPH,UTI,UTL,UVV,VAC,VCEL,VCTR,VCYT,VECO,VGNT,VIR,VIRT,VSAT,VSEC,VSH,VSTS,VTOL,VYX,WABC,WAFD,WAY,WD,WDFC,WEN,WERN,WHD,WKC,WLY,WOR,WRBY,WS,WSBC,WSC,WSFS,WT,WWW,XHR,XNCR,XPEL,YELP,YOU,ZD,ZWS
""".replace("\n", "").split(",")))

# Controlling for size (per user decision): drop small-caps, keep S&P 500 + mid-cap only.
UNIVERSE_TICKERS = sorted(set(SP500_TICKERS) | set(MID_TICKERS))

SEC_USER_AGENT = "LocalTradingBotResearchProject/1.0 (independent hobby research script)"
SEC_MIN_INTERVAL = 0.11  # SEC allows ~10 req/sec; stay comfortably under with margin

START_FY = 2009
END_FY = 2023  # last fiscal year with a full 12-month forward window available before "today"
FORMATION_LAG_DAYS = 90  # approximates real-world 10-K filing/dissemination lag
FORWARD_WINDOW_DAYS = 365  # calendar days forward for the "12-month" return

_TAG_CANDIDATES: dict[str, list[str]] = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet", "SalesRevenueGoodsNet"],
    "cogs": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
    "receivables": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"],
    "total_assets": ["Assets"],
    "current_assets": ["AssetsCurrent"],
    "ppe_net": ["PropertyPlantAndEquipmentNet"],
    "depreciation": ["DepreciationDepletionAndAmortization",
                      "DepreciationAmortizationAndAccretionNet", "Depreciation"],
    "sga": ["SellingGeneralAndAdministrativeExpense"],
    "current_liab": ["LiabilitiesCurrent"],
    "lt_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "net_income": ["NetIncomeLoss"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
}
_DURATION_CONCEPTS = {"revenue", "cogs", "depreciation", "sga", "net_income", "cfo"}
_INSTANT_CONCEPTS = {"receivables", "total_assets", "current_assets", "ppe_net",
                      "current_liab", "lt_debt"}

_last_sec_call = 0.0


def _sec_get(url: str) -> Optional[dict]:
    global _last_sec_call
    elapsed = time.monotonic() - _last_sec_call
    if elapsed < SEC_MIN_INTERVAL:
        time.sleep(SEC_MIN_INTERVAL - elapsed)
    _last_sec_call = time.monotonic()
    try:
        resp = requests.get(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=20)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def fetch_ticker_to_cik() -> dict[str, str]:
    data = _sec_get("https://www.sec.gov/files/company_tickers.json")
    if not data:
        return {}
    out = {}
    for row in data.values():
        out[row["ticker"].upper()] = str(row["cik_str"]).zfill(10)
    return out


def fetch_company_facts(cik: str) -> Optional[dict]:
    return _sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")


def _extract_annual_series(facts: dict, concept_key: str) -> dict[int, tuple[float, date]]:
    """
    Return {fiscal_year: (value, period_end_date)} for the first XBRL tag (among
    _TAG_CANDIDATES[concept_key]) with usable 10-K, full-fiscal-year data.

    IMPORTANT: SEC's own `fy` field on each fact means "which annual filing disclosed this
    value," NOT "which fiscal period the value covers" -- a company's FY2010 10-K re-reports
    FY2009's balance sheet as a comparative column, and that datapoint is tagged fy=2010, not
    fy=2009. Using `fy` directly silently drops/misfiles most historical years. Instead, derive
    the fiscal year from the actual `end` date and dedupe by end-date, keeping the FIRST
    (as-originally-filed) value seen -- avoids both the fy-mislabeling bug and any lookahead
    bias from later restatements.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    is_duration = concept_key in _DURATION_CONCEPTS
    # MERGE across tag candidates (don't short-circuit on the first with any data): companies
    # switch XBRL tags over time (e.g. ASC 606 adoption ~2018 moved revenue from
    # "SalesRevenueNet" to "RevenueFromContractWithCustomerExcludingAssessedTax") -- taking only
    # the first non-empty tag silently loses whichever years used the other one. Earlier entries
    # in _TAG_CANDIDATES win on overlap (assumed more current/preferred tag).
    by_end_date: dict[date, float] = {}
    for tag in reversed(_TAG_CANDIDATES[concept_key]):  # reversed: first-listed applied last, wins
        node = us_gaap.get(tag)
        if not node:
            continue
        usd_list = node.get("units", {}).get("USD", [])
        for item in usd_list:
            if item.get("form") != "10-K" or item.get("fp") != "FY":
                continue
            try:
                end_d = datetime.strptime(item["end"], "%Y-%m-%d").date()
                if is_duration:
                    start_d = datetime.strptime(item["start"], "%Y-%m-%d").date()
                    span = (end_d - start_d).days
                    if not (350 <= span <= 380):  # a full fiscal year, not a stub/quarter
                        continue
                by_end_date[end_d] = float(item["val"])  # later loop iterations may overwrite
            except (KeyError, ValueError):
                continue
    if not by_end_date:
        return {}
    # Bucket by the end-date's calendar year -- consistent across all concepts regardless of
    # the company's actual fiscal-year-end month.
    return {end_d.year: (v, end_d) for end_d, v in by_end_date.items()}


def compute_scores_for_year(annual: dict[str, dict[int, tuple[float, date]]], fy: int) -> Optional[dict]:
    """Beneish M-score + Sloan accruals for fiscal year `fy` vs fy-1. None if any required
    input is missing (dropped, not guessed -- per the agreed error-handling policy)."""
    prev = fy - 1

    def val(key: str, year: int) -> Optional[float]:
        pair = annual.get(key, {}).get(year)
        return pair[0] if pair else None

    revenue_t, revenue_p = val("revenue", fy), val("revenue", prev)
    cogs_t, cogs_p = val("cogs", fy), val("cogs", prev)
    recv_t, recv_p = val("receivables", fy), val("receivables", prev)
    assets_t, assets_p = val("total_assets", fy), val("total_assets", prev)
    cur_assets_t, cur_assets_p = val("current_assets", fy), val("current_assets", prev)
    ppe_t, ppe_p = val("ppe_net", fy), val("ppe_net", prev)
    dep_t, dep_p = val("depreciation", fy), val("depreciation", prev)
    sga_t, sga_p = val("sga", fy), val("sga", prev)
    cur_liab_t, cur_liab_p = val("current_liab", fy), val("current_liab", prev)
    lt_debt_t = val("lt_debt", fy) or 0.0
    lt_debt_p = val("lt_debt", prev) or 0.0
    net_income_t = val("net_income", fy)
    cfo_t = val("cfo", fy)

    required = [revenue_t, revenue_p, cogs_t, cogs_p, recv_t, recv_p, assets_t, assets_p,
                cur_assets_t, cur_assets_p, ppe_t, ppe_p, sga_t, sga_p,
                cur_liab_t, cur_liab_p, net_income_t, cfo_t, dep_t, dep_p]
    if any(v is None for v in required):
        return None
    if revenue_t == 0 or revenue_p == 0 or assets_t == 0 or assets_p == 0:
        return None
    if (dep_t + ppe_t) == 0 or (dep_p + ppe_p) == 0:
        return None
    if recv_p == 0:  # financials/REITs/insurers often carry zero A/R; ratio-of-ratios undefined
        return None

    dsri = (recv_t / revenue_t) / (recv_p / revenue_p)
    gm_t = (revenue_t - cogs_t) / revenue_t
    gm_p = (revenue_p - cogs_p) / revenue_p
    if gm_t == 0:
        return None
    gmi = gm_p / gm_t
    aqi_p_denom = 1 - (cur_assets_p + ppe_p) / assets_p
    if aqi_p_denom == 0:
        return None
    aqi = (1 - (cur_assets_t + ppe_t) / assets_t) / aqi_p_denom
    sgi = revenue_t / revenue_p
    depi_t = dep_t / (dep_t + ppe_t)
    if depi_t == 0:
        return None
    depi_p = dep_p / (dep_p + ppe_p)
    depi = depi_p / depi_t
    sgai = (sga_t / revenue_t) / (sga_p / revenue_p) if revenue_p != 0 and sga_p != 0 else None
    lvgi_p = (cur_liab_p + lt_debt_p) / assets_p
    if lvgi_p == 0 or sgai is None:
        return None
    lvgi = ((cur_liab_t + lt_debt_t) / assets_t) / lvgi_p
    tata = (net_income_t - cfo_t) / assets_t

    m_score = (-4.84 + 0.920 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi
               + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi)
    sloan_accruals = (net_income_t - cfo_t) / ((assets_t + assets_p) / 2)

    period_end = annual["total_assets"][fy][1]
    return {"m_score": m_score, "sloan_accruals": sloan_accruals, "fiscal_year": fy,
            "period_end": period_end}


def main() -> None:
    from app.services import market_data

    print(f"[VALIDATE] Universe: {len(UNIVERSE_TICKERS)} tickers "
          f"(S&P 500 + mid/small-cap, survivorship-biased)")
    print("[VALIDATE] Resolving ticker -> CIK via SEC's official mapping...")
    ticker_to_cik = fetch_ticker_to_cik()
    print(f"[VALIDATE] SEC mapping has {len(ticker_to_cik)} entries")

    observations: list[dict] = []
    n_resolved = n_scored = n_priced = 0

    for i, ticker in enumerate(UNIVERSE_TICKERS):
        yf_ticker = ticker.replace(".", "-")  # yfinance uses BRK-B, SEC uses BRK.B in some feeds
        cik = ticker_to_cik.get(ticker.replace(".", "").replace("-", ""))
        if cik is None:
            cik = ticker_to_cik.get(ticker)
        if cik is None:
            continue
        n_resolved += 1
        facts = fetch_company_facts(cik)
        if not facts:
            continue

        annual: dict[str, dict[int, tuple[float, date]]] = {
            key: _extract_annual_series(facts, key) for key in _TAG_CANDIDATES
        }
        available_years = sorted(annual.get("total_assets", {}).keys())
        if not available_years:
            continue

        company_scores = []
        for fy in range(START_FY, END_FY + 1):
            if fy not in available_years:
                continue
            scored = compute_scores_for_year(annual, fy)
            if scored is not None:
                company_scores.append(scored)
        if not company_scores:
            continue
        n_scored += 1

        # Forward returns: fetch full price history once per ticker.
        px = market_data.fetch_ohlcv(yf_ticker, period="20y")
        if px is None or px.empty:
            continue
        if px.index.tz is not None:  # normalize to tz-naive for plain-date comparisons below
            px = px.tz_localize(None)
        n_priced += 1
        for s in company_scores:
            formation = s["period_end"] + timedelta(days=FORMATION_LAG_DAYS)
            target = formation + timedelta(days=FORWARD_WINDOW_DAYS)
            entry_slice = px[px.index >= pd_ts(formation)]
            exit_slice = px[px.index >= pd_ts(target)]
            if entry_slice.empty or exit_slice.empty:
                continue
            entry_px = float(entry_slice["Close"].iloc[0])
            exit_px = float(exit_slice["Close"].iloc[0])
            if entry_px <= 0:
                continue
            fwd_return = exit_px / entry_px - 1
            observations.append({
                "ticker": ticker, "fiscal_year": s["fiscal_year"],
                "m_score": s["m_score"], "sloan_accruals": s["sloan_accruals"],
                "formation_date": formation.isoformat(), "fwd_return_12m": fwd_return,
            })

        if (i + 1) % 50 == 0:
            print(f"[VALIDATE] {i + 1}/{len(UNIVERSE_TICKERS)} tickers processed "
                  f"(resolved={n_resolved} scored={n_scored} priced={n_priced} "
                  f"obs={len(observations)})")

    print(f"\n[VALIDATE] DONE. resolved={n_resolved} scored={n_scored} priced={n_priced} "
          f"total observations={len(observations)}")

    out_path = "/tmp/structural_short_thesis_raw.json"
    with open(out_path, "w") as f:
        json.dump(observations, f, default=str)
    print(f"[VALIDATE] Raw observations dumped to {out_path}")

    analyze(observations)


def pd_ts(d: date):
    import pandas as pd
    return pd.Timestamp(d)


def analyze(observations: list[dict]) -> None:
    if not observations:
        print("[VALIDATE] No observations -- cannot analyze.")
        return
    import pandas as pd
    df = pd.DataFrame(observations)
    print(f"\n{'=' * 90}\nSTRUCTURAL SHORT THESIS VALIDATION — Beneish M-score quintiles\n{'=' * 90}")
    print(f"Total observations (company-fiscal-years): {len(df)}")
    print(f"Years covered: {sorted(df['fiscal_year'].unique())}")

    year_results = []
    for fy, group in df.groupby("fiscal_year"):
        if len(group) < 20:  # need enough names to form quintiles meaningfully
            continue
        try:
            group = group.copy()
            group["quintile"] = pd.qcut(group["m_score"], 5, labels=False, duplicates="drop")
        except ValueError:
            continue
        if group["quintile"].nunique() < 5:
            continue
        worst = group[group["quintile"] == 4]["fwd_return_12m"].mean()  # highest M-score = worst
        best = group[group["quintile"] == 0]["fwd_return_12m"].mean()   # lowest M-score = best
        spread = worst - best  # negative = worst underperforms best, as the thesis predicts
        year_results.append({"fiscal_year": int(fy), "n": len(group),
                              "worst_quintile_fwd_return": round(worst, 4),
                              "best_quintile_fwd_return": round(best, 4),
                              "spread_worst_minus_best": round(spread, 4)})

    print("\n--- Per fiscal year (worst quintile = highest M-score = distress/manipulation risk) ---")
    for r in year_results:
        print(json.dumps(r))

    if not year_results:
        print("\n[VALIDATE] Not enough per-year data to form quintiles. Cannot evaluate the bar.")
        return

    spreads = [r["spread_worst_minus_best"] for r in year_results]
    avg_spread = sum(spreads) / len(spreads)
    n_favorable = sum(1 for s in spreads if s < 0)  # worst underperforms best
    pct_favorable = n_favorable / len(spreads)

    print(f"\n{'=' * 90}\nSUCCESS BAR CHECK: avg spread <= -5%/yr, direction holds >=60% of years\n{'=' * 90}")
    print(f"  Years evaluated: {len(spreads)}")
    print(f"  Average spread (worst - best fwd return): {avg_spread:.4f} (bar: <= -0.05)")
    print(f"  Years where worst-quintile underperformed best-quintile: {n_favorable}/{len(spreads)} "
          f"({pct_favorable:.1%}) (bar: >= 60%)")
    passes = avg_spread <= -0.05 and pct_favorable >= 0.60
    print(f"  OVERALL BAR MET: {passes}")


if __name__ == "__main__":
    main()
