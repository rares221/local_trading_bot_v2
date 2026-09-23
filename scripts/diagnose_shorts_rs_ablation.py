"""
RS-gate ablation — isolates whether the relative-strength gate specifically is responsible
for the near-total activation collapse seen in the Phase 1 checkpoint, or whether the other
Phase 1 fixes (horizon-matched sizing, volume veto, flattened offsets) are the meaningful
ones. Runs on the SAME already-spent 2021-2024 checkpoint window (not the reserved blind
holdout) — a variable-isolation diagnostic, not a re-validation.

Monkeypatches BREAKDOWN_RS_MAX / FADE_TOP_RS_MIN to non-restrictive values so the RS gate
still requires a real (non-None) RS value (fail-closed on missing data preserved) but never
blocks on the threshold itself.

Run inside the container:
    docker-compose exec -T app python -m scripts.diagnose_shorts_rs_ablation
"""
from __future__ import annotations

import app.services.recommendations.shorts_signals as shorts_signals_mod

# Structurally present (still requires a real RS value) but never restrictive.
shorts_signals_mod.BREAKDOWN_RS_MAX = 999.0
shorts_signals_mod.FADE_TOP_RS_MIN = -999.0

from scripts.diagnose_shorts_tier1 import main  # noqa: E402 (must patch before import runs it)

if __name__ == "__main__":
    print("[ABLATION] BREAKDOWN_RS_MAX ->", shorts_signals_mod.BREAKDOWN_RS_MAX)
    print("[ABLATION] FADE_TOP_RS_MIN  ->", shorts_signals_mod.FADE_TOP_RS_MIN)
    main()
