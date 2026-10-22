"""
Throwaway diagnostic (read-only): for backtest run 062af0ff-8a44-44ed-bbc6-e691280db486,
reconstruct what sentiment_avg/sentiment_count would ACTUALLY have been at each of the 7124
resolved trades' entry dates, replicating app/services/analysis/aggregate.py's real
decay-half-life + source-weight + entity-weight + domain-correction formula (not a
simplification), then correlate against outcome (hit_tp=1/hit_sl=0):
  1. compute_confidence's two sentiment components as they'd have actually scored
     (sentiment alignment up to 15pts, mention volume up to 10pts).
  2. The raw sentiment_avg / sentiment_count values themselves (uncapped).

Does NOT modify aggregate.py, technicals.py, or scoring.py -- imports the real, pure helper
functions from aggregate.py and domain_correction.py directly (they take no DB/async args),
and duplicates compute_confidence's sentiment-only arithmetic (verbatim, lines ~1490-1500).

Point-in-time correctness: for each trade's entry_date, "now" is fixed at 16:00 UTC that day
(US market close) and only feed_items with that effective published/fetched_at <= now are
used -- no lookahead. Candidate-matching mirrors replay.py._build_signal exactly: try the
symbol as-is, then strip "=", "-", "." suffixes, first asset_tags match wins.
Items older than 60 days are excluded before weighting purely for performance -- at the
24h-default half-life, exp(-0.693*1440/24) = 2^-60, i.e. zero contribution; this does not
change the result, only skips negligible arithmetic. Production's extra `LIMIT 2000 most
recently fetched` cap is not replicated (irrelevant at this decay rate unless a pathological
volume spike occurred, which was not checked) -- flagged as a deliberate simplification.

Run: docker-compose exec -T app python -m scripts.diagnose_confidence_sentiment
"""
from __future__ import annotations

import bisect
import math
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.services.analysis.aggregate import (
    _decay_weight, _source_weight, _entity_weight, _get_sentiment_half_life,
)
from app.services.sentiment.domain_correction import apply_domain_correction

RUN_ID = "062af0ff-8a44-44ed-bbc6-e691280db486"
MAX_AGE_DAYS = 60
SENTIMENT_SIGNAL_SCALE = 1.0  # default (compute_confidence's `ss`), matching prior spike's treatment


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


def main():
    sync_url = settings.database_url.replace("+aiosqlite", "").replace("+asyncpg", "")
    engine = sa.create_engine(sync_url)
    Session = sessionmaker(bind=engine)
    s = Session()

    trades = s.execute(sa.text(
        "select symbol, asset_category, direction, entry_date, outcome, pnl_r, market_regime "
        "from backtest_trades where run_id=:rid and outcome in ('hit_tp','hit_sl')"
    ), {"rid": RUN_ID}).fetchall()
    print(f"[DIAG] {len(trades)} resolved trades loaded")

    feed = s.execute(sa.text(
        "select published, fetched_at, source, title, description, sentiment_score, asset_tags "
        "from feed_items where sentiment_score is not null and asset_tags is not null"
    )).fetchall()
    s.close()
    print(f"[DIAG] {len(feed)} feed_items with sentiment+tags loaded")

    def item_time(row):
        t = row.published or row.fetched_at
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t

    feed_sorted = sorted(feed, key=item_time)
    feed_times = [item_time(r) for r in feed_sorted]

    half_life = _get_sentiment_half_life()
    print(f"[DIAG] sentiment half-life (prefs or default) = {half_life}h")

    # Group trades by entry_date so the (expensive-ish) per-date weighted aggregation runs once
    # per unique date, not once per trade.
    by_date: dict = {}
    for r in trades:
        by_date.setdefault(r.entry_date, []).append(r)
    print(f"[DIAG] {len(by_date)} distinct entry dates across {len(trades)} trades")

    results = []
    for di, (edate, rows) in enumerate(sorted(by_date.items())):
        now = datetime(edate.year, edate.month, edate.day, 16, 0, 0, tzinfo=timezone.utc)
        lo_time = now - timedelta(days=MAX_AGE_DAYS)
        lo_idx = bisect.bisect_right(feed_times, lo_time)
        hi_idx = bisect.bisect_right(feed_times, now)
        window = feed_sorted[lo_idx:hi_idx]

        by_asset_weighted: dict[str, list[tuple[float, float]]] = {}
        tag_titles: dict[str, list[str]] = {}
        for it in window:
            base_w = _decay_weight(it.published or it.fetched_at, now)
            src_w = _source_weight(it.source)
            text = f"{it.title or ''} {it.description or ''}".strip()
            for tag in (it.asset_tags or []):
                w = base_w * src_w * _entity_weight(tag)
                by_asset_weighted.setdefault(tag, []).append((it.sentiment_score, w))
                if text:
                    tag_titles.setdefault(tag, []).append(text)

        by_asset_sentiment: dict[str, dict] = {}
        for tag, pairs in by_asset_weighted.items():
            total_weight = sum(w for _, w in pairs)
            if total_weight > 0:
                weighted_avg = sum(sc * w for sc, w in pairs) / total_weight
            else:
                weighted_avg = sum(sc for sc, _ in pairs) / len(pairs) if pairs else 0
            corrected_avg, _ = apply_domain_correction(tag, weighted_avg, tag_titles.get(tag, []))
            by_asset_sentiment[tag] = {"avg": round(corrected_avg, 4), "count": len(pairs)}

        for r in rows:
            candidates = [r.symbol, r.symbol.split("=")[0], r.symbol.split("-")[0], r.symbol.split(".")[0]]
            hist_sentiment = None
            hist_count = 0
            for cand in candidates:
                entry = by_asset_sentiment.get(cand)
                if entry:
                    hist_sentiment = entry["avg"]
                    hist_count = entry["count"]
                    break

            # compute_confidence's sentiment (15pt) + mentions (10pt) components, verbatim.
            is_long = r.direction == "long"
            ss = max(0.5, min(1.6, SENTIMENT_SIGNAL_SCALE))
            sent_pts = 0.0
            if hist_sentiment is not None:
                if (is_long and hist_sentiment > 0) or (not is_long and hist_sentiment < 0):
                    sent_pts = min(15.0, abs(hist_sentiment) * 15.0 * ss)
            mention_pts = 0.0
            if hist_count > 0:
                mention_pts = min(10.0, math.log1p(hist_count) * 3.0 * ss)

            win = 1 if r.outcome == "hit_tp" else 0
            results.append({
                "symbol": r.symbol, "category": r.asset_category, "direction": r.direction,
                "win": win,
                "raw_sentiment_avg": hist_sentiment, "raw_sentiment_count": hist_count,
                "sent_pts": sent_pts, "mention_pts": mention_pts,
            })

        if (di + 1) % 100 == 0:
            print(f"[DIAG] {di + 1}/{len(by_date)} dates processed")

    print(f"[DIAG] DONE: {len(results)} trade-records built\n")

    import json
    with open("/tmp/confidence_sentiment_raw.json", "w") as f:
        json.dump(results, f, default=str)

    overall_wr = sum(rec["win"] for rec in results) / len(results)
    n_with_sentiment = sum(1 for rec in results if rec["raw_sentiment_avg"] is not None)
    print(f"Overall WR: {overall_wr:.1%}  n={len(results)}")
    print(f"Trades with ANY matched historical sentiment: {n_with_sentiment}/{len(results)} "
          f"({n_with_sentiment / len(results):.1%})\n")

    def corr_report(key, label):
        pairs = [(rec[key], rec["win"]) for rec in results if rec[key] is not None]
        if len(pairs) < 10:
            print(f"{label:<22} n/a (n={len(pairs)})")
            return None
        pts = [p for p, _ in pairs]
        outs = [o for _, o in pairs]
        corr = point_biserial(pts, outs)
        print(f"{label:<22} corr={corr:+.3f}  n={len(pairs)}")
        return (label, corr, len(pairs))

    print("-- Formula-based (as compute_confidence would actually score it) --")
    r1 = corr_report("sent_pts", "sentiment_pts (0-15)")
    r2 = corr_report("mention_pts", "mention_pts (0-10)")

    print("\n-- Raw, uncapped values (dose-response check) --")
    r3 = corr_report("raw_sentiment_avg", "raw_sentiment_avg")
    r4 = corr_report("raw_sentiment_count", "raw_sentiment_count")

    print("\n-- Category breakdown (raw_sentiment_avg, where matched) --")
    cats = sorted({rec["category"] for rec in results})
    for cat in cats:
        sub = [rec for rec in results if rec["category"] == cat and rec["raw_sentiment_avg"] is not None]
        if len(sub) < 20:
            print(f"  {cat:<14} n={len(sub)} (too few matched)")
            continue
        pts = [rec["raw_sentiment_avg"] for rec in sub]
        outs = [rec["win"] for rec in sub]
        corr = point_biserial(pts, outs)
        print(f"  {cat:<14} n={len(sub):<6} corr={corr:+.3f}")


if __name__ == "__main__":
    main()
