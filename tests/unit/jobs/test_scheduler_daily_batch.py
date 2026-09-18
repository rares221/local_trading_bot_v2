"""Phase 6 (task 4): scheduler smoke test for the daily-batch persistence contract.

`job_daily_batch` loads the latest analysis, calls `build_daily_candidates`, and writes one
`Recommendation` per candidate. The rest of the unit suite deliberately stubs out SQLAlchemy and
`app.core.database` (see tests/conftest.py) so it needs no DB — but the scheduler pulls the real ORM
models, so this module temporarily restores the real sqlalchemy/model stack for the duration of each
test (the engine is created lazily and `database_url` is set in-container, so importing touches no DB)
and restores the suite's stubs afterward. The builder, session, LLM rationale and notifications are all
mocked; the assertions are the persistence contract only.
"""
from __future__ import annotations

import contextlib
import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# sys.modules keys the conftest stubs (or that we rebuild on the real ORM) and must be
# saved/purged/restored around a real scheduler import.
_SWAP_PREFIXES = ("sqlalchemy", "app.core.database", "app.core.base", "app.models", "app.jobs")


def _matches(mod: str) -> bool:
    return any(mod == p or mod.startswith(p + ".") for p in _SWAP_PREFIXES)


@contextlib.contextmanager
def _real_orm_imports():
    saved = {k: v for k, v in sys.modules.items() if _matches(k)}
    for k in list(saved):
        del sys.modules[k]
    try:
        import app.jobs.scheduler as scheduler  # real sqlalchemy + models load here (in-container)
        yield scheduler
    finally:
        for k in list(sys.modules):
            if _matches(k):
                del sys.modules[k]
        sys.modules.update(saved)


class _AsyncCM:
    """Minimal async context manager yielding a fixed session (mimics async_session_factory())."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _candidate(asset_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        asset_category="stocks_large", asset_id=asset_id,
        buy_in_price=100.0, stop_loss=95.0, take_profit=110.0,
        expiry_date=date(2024, 6, 15), risk_tier="moderate", direction="long",
        confidence_score=65, risk_reward=2.0, volatility_notes="vol note",
        market_regime_note="regime note", suggested_risk_pct=1.0,
        min_confidence_gate=40, shadow_b_threshold=None, shadow_b_pass=None,
        vpoc=None, vah=None, val=None, vp_bias=None, vp_overextension=None,
        in_value_area=False, rs_20d=None, rs_60d=None, sector_rs=None,
    )


def _mock_session() -> MagicMock:
    session = MagicMock()
    pending = MagicMock()
    pending.scalars.return_value.first.return_value = None   # no existing recs → don't skip
    taken = MagicMock()
    taken.all.return_value = []                              # no taken assets
    closed = MagicMock()
    closed.scalars.return_value.all.return_value = []        # no closed trades
    session.execute = AsyncMock(side_effect=[pending, taken, closed])
    session.commit = AsyncMock()
    session.add = MagicMock()
    return session


async def test_job_daily_batch_persists_one_rec_per_candidate():
    session = _mock_session()
    analysis = SimpleNamespace(id="analysis-1", payload={"by_asset_sentiment": {}, "active_events": None})
    candidates = [_candidate("AAPL"), _candidate("MSFT")]

    with _real_orm_imports() as scheduler:
        with (
            patch.object(scheduler, "async_session_factory", return_value=_AsyncCM(session)),
            patch.object(scheduler, "get_latest_analysis", AsyncMock(return_value=analysis)),
            patch.object(scheduler, "build_daily_candidates", return_value=candidates),
            patch("app.core.preferences.get_preferred_risk", return_value="moderate"),
            patch("app.core.preferences.get_risk_budget_1r", return_value=100.0),
            patch("app.services.research.build_research_context", AsyncMock(return_value={})),
            patch("app.services.research.generate_rationale", AsyncMock(return_value="rationale")),
            patch("app.services.notifications.send_notification", AsyncMock()),
            patch("app.services.pipeline_progress.update", MagicMock()),
            patch("app.services.pipeline_progress.log_stage", MagicMock()),
            patch("app.services.pipeline_progress.finish", MagicMock()),
        ):
            await scheduler.job_daily_batch()

    assert session.add.call_count == 2       # one Recommendation per candidate
    assert session.commit.await_count == 1   # single commit for the batch


async def test_job_daily_batch_skips_when_no_candidates():
    session = _mock_session()
    analysis = SimpleNamespace(id="analysis-1", payload={})

    with _real_orm_imports() as scheduler:
        with (
            patch.object(scheduler, "async_session_factory", return_value=_AsyncCM(session)),
            patch.object(scheduler, "get_latest_analysis", AsyncMock(return_value=analysis)),
            patch.object(scheduler, "build_daily_candidates", return_value=[]),
            patch("app.core.preferences.get_preferred_risk", return_value="moderate"),
            patch("app.core.preferences.get_risk_budget_1r", return_value=100.0),
            patch("app.services.pipeline_progress.update", MagicMock()),
            patch("app.services.pipeline_progress.log_stage", MagicMock()),
            patch("app.services.pipeline_progress.finish", MagicMock()),
        ):
            await scheduler.job_daily_batch()

    session.add.assert_not_called()
    session.commit.assert_not_awaited()
