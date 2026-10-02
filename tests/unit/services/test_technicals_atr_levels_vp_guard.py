"""
Regression test for the 2026-09-18 production incident: batch_id 2026-09-18 produced
zero recommendations for stocks_mid, stocks_small, indices, and other, while
stocks_large/commodities were unaffected.

Root cause: compute_atr_levels()'s Volume-Profile-aware TP snap (HVN/VAH for longs,
HVN/VAL for shorts) unconditionally snapped the take-profit to that level whenever it
sat between price and the ATR-based target, with NO check that the resulting R:R stayed
>= 1.0 -- unlike the swing-S/R fallback immediately below it, which already has that guard.

When price sits just below its own recent Value Area High (a common, unremarkable
technical state -- not itself a bug), TP collapsed to within ~1-5% of entry while the
ATR/support-based SL stayed several ATR-widths away, producing R:R as low as ~0.09-0.5.
That failed even the loosened per-category min-R:R gates (~1.35), so
`build_daily_candidates` silently rejected the candidate via the `risk_reward` skip
reason -- verified live for RXRX (R:R 0.09), ^IXIC/NDX (R:R 0.41-0.48) and SMCI
(R:R ~1.07, still short of the 1.35 floor) on 2026-09-18. stocks_large/commodities names
inspected the same day (AAPL, MSFT, CL=F, GC=F) were trading AT/ABOVE their own VAH, so
the unguarded snap never fired for them -- purely a function of where each symbol's price
sat relative to its own VAH, not a category-specific code path.

This test pins the fix: the VAH/HVN (and VAL/HVN) snap must be skipped when it would push
R:R below 1.0, exactly like the pre-existing swing-S/R guard.
"""
from app.services.technicals import TechnicalSnapshot, compute_atr_levels, calc_risk_reward


def _snap(**kw) -> TechnicalSnapshot:
    return TechnicalSnapshot(symbol="TEST", price=kw.pop("price", 100.0), **kw)


def test_long_vah_snap_does_not_collapse_rr_below_one():
    """
    Reproduces the RXRX shape from the 2026-09-18 batch logs: price sits just below its
    VAH/HVN (a real, unremarkable technical state), while support (SL anchor) is far
    below. Before the fix, the unguarded VAH snap pulled TP to within ~1.5% of price
    while SL stayed ~16% away (observed live R:R == 0.09). The fix must leave TP at (or
    beyond) the ATR-based target instead of snapping into a losing R:R.
    """
    price = 3.685
    snap = _snap(
        price=price,
        atr=0.2035,
        volatility_regime="stable",
        adx=None,
        supports=[3.10],
        resistances=[3.70],
        vpoc=3.314,
        vah=3.7463,   # sits just above price -> in the danger zone for the unguarded snap
        val=3.0587,
        hvn_levels=[3.5015, 3.6994],  # hvn_below < price < hvn_above, both close to price
    )
    sl, tp = compute_atr_levels(price, "aggressive", snapshot=snap, direction="long")
    rr = calc_risk_reward(price, sl, tp)

    assert rr is not None
    assert rr >= 1.0, f"VAH/HVN snap collapsed R:R to {rr} (sl={sl}, tp={tp})"


def test_long_vah_snap_still_applies_when_it_keeps_rr_healthy():
    """Sanity check: the VP-aware TP snap should still fire (and tighten TP) when the
    resulting R:R comfortably clears 1.0 -- the fix must not disable the feature outright,
    only guard the case where snapping would invert risk:reward."""
    price = 100.0
    snap = _snap(
        price=price,
        atr=1.0,
        volatility_regime="stable",
        adx=None,
        supports=[95.0],
        resistances=[130.0],
        vpoc=101.0,
        vah=103.2,   # between price and the ATR tp_base (103.5); snapping keeps R:R >= 1
        val=90.0,
        hvn_levels=[],
    )
    sl, tp = compute_atr_levels(price, "moderate", snapshot=snap, direction="long")
    rr = calc_risk_reward(price, sl, tp)

    assert tp == round(103.2 * 0.998, 2)  # snap still applied (tighter than the 103.5 ATR target)
    assert rr is not None and rr >= 1.0


def test_short_val_snap_does_not_collapse_rr_below_one():
    """Mirror of the long case on the short side: price just above its VAL, no swing S/R
    levels present to interfere, isolating the VP-aware TP guard."""
    price = 100.0
    snap = _snap(
        price=price,
        atr=2.0,
        volatility_regime="stable",
        adx=None,
        supports=[],
        resistances=[],
        vpoc=99.0,
        vah=None,
        val=99.5,   # just below price -> danger zone for the unguarded short TP snap
        hvn_levels=[],
    )
    sl, tp = compute_atr_levels(price, "aggressive", snapshot=snap, direction="short")
    rr = calc_risk_reward(price, sl, tp)

    assert rr is not None
    assert rr >= 1.0, f"VAL snap collapsed short R:R to {rr} (sl={sl}, tp={tp})"
