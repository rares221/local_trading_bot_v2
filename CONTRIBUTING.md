# Contributing to local_trading_bot_v2

## Branch Rules

- **Always branch from `dev`**, never from `master`.
- **PR target is `integration`**, never `master` directly.
- One logical change per branch. Don't stack unrelated features.

```bash
git checkout dev
git pull origin dev
git checkout -b your-branch-name
```

---

## Before You Change Anything — Record the Baseline

Start the app and run a full backtest on the current branch. Record these numbers before touching any code:

| Metric | Baseline value |
|---|---|
| Sharpe ratio | |
| Win rate | |
| Avg P&L (R) | |
| Max drawdown (R) | |
| Total trades | |
| Walk-forward degradation ratio | |

**Accepted baseline (locked config as of 2026-04-29):**
Sharpe **2.560**, WR **31%**, Avg P&L **+0.134R**, Max DD **249R**, 5621 trades, walk-forward ratio **0.964** as of 29.04.2026. Values will be updated on better results

If your baseline doesn't match, investigate before proceeding — the config or branch may have diverged.

---

## Contribution Rules

These rules exist because violations have caused measurable regressions. Follow them exactly.

### Signal engine changes
1. **Soft penalties over hard gates.** Any new filter must default to a confidence penalty, not a hard skip. Hard gates are only justified by backtest evidence showing the removed category has ≤ breakeven expectancy with no perverse selection.
2. **Mirror every change.** Any gate or penalty added to `engine.py` (`_build_candidate`) must be mirrored identically in `replay.py` (`_build_signal`). Use the `# --- MIRRORED FROM engine.py ---` marker. The backtest measures nothing if replay diverges from the live engine.
3. **One change per backtest.** Never stack two changes and run a single backtest. Each change gets its own before/after run.
4. **No runtime DB calls in replay.** The replay hot path must be deterministic. Use explicit params, policy objects, or config derived before the replay loop starts.
5. **All soft penalties share a single pool.** Add new penalties to `_new_filter_penalty`, capped at 25 pts. No standalone penalty that can silently combine with others into an invisible hard gate.

### UI changes
6. **Every new button, card, or section gets a tooltip.** Use `data-tooltip="..."` — the tooltip system activates after 3 seconds on hover. No exceptions.
7. **New packages go in `requirements.txt`**, not installed directly in the container.

### Backtesting
8. **No lookahead bias.** Any new signal feature must consume only data available at `signal_date`. Rolling windows must use only bars `<= signal_date`.
9. **Document parity.** Update the Parity Matrix in `.cursor/plans/signal-fidelity-upgrade_cc9feca3.plan.md` for any engine/replay change.

---

## After Your Changes — Verify Improvement

Run a full backtest on the same date range and config used for your baseline. Your change is acceptable only if:

- [ ] Sharpe ratio is equal or higher than baseline
- [ ] Avg P&L (R) is equal or higher than baseline
- [ ] Max drawdown is equal or lower than baseline
- [ ] Win rate has not dropped below breakeven (check the breakeven WR shown in the backtest UI)
- [ ] Trade count has not dropped below 80% of baseline (a large drop signals perverse selection — the filter is removing better trades, not worse ones)

If any metric regresses, revert and investigate root cause before re-attempting.

### Walk-forward validation (required for signal changes)

For any change that affects confidence scoring or signal filtering, run a walk-forward analysis (252 IS / 63 OOS days) and confirm:

- [ ] Degradation ratio ≥ **0.85** (robust) — values below 0.70 indicate overfitting

Walk-forward results are now persisted to the DB and visible in the Backtest page under "Past Runs" — you don't need to keep the tab open.

---

## Rebuild After Code Changes

```bash
make build start
```

Check logs if something looks wrong:

```bash
podman logs -f local-trading-bot
```

---

## Opening a Pull Request

1. Target branch: **`integration`** — not `master`, not `dev`
2. Add both reviewers: **@rares221** and **@bolchisb**
3. Fill in the PR description with:
   - What changed and why
   - Baseline metrics vs post-change metrics (copy the table from above)
   - Walk-forward degradation ratio (if applicable)
   - Parity matrix entry updated (yes/no)

PRs missing baseline/post-change metric comparison will not be reviewed.

---

## Quick Reference

| Command | Purpose |
|---|---|
| `make build start` | Rebuild and restart after code changes |
| `make stop` | Stop containers |
| `podman logs -f local-trading-bot` | App logs |
| `podman logs -f trading-timescaledb` | DB logs |
| `podman exec local-trading-bot alembic upgrade head` | Apply DB migrations |

App runs at **http://localhost:6001** — backtest and walk-forward are on the `/backtest` page.
