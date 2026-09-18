"""Phase 4: OHLCV cache TTL — a live-mode cached frame past its TTL is treated as a miss so a
signal is never emitted off hours-stale bars; backtest mode ignores the TTL (cutoff slicing
governs correctness there)."""
import time

import pandas as pd

import app.services.technicals as tech


def _seed(key: str, age_seconds: float) -> None:
    tech._ohlcv_cache[key] = pd.DataFrame({"Close": [1.0, 2.0]})
    tech._ohlcv_cache_ts[key] = time.monotonic() - age_seconds


def test_live_frame_past_ttl_is_expired():
    tech.clear_cache()
    tech.clear_backtest_mode()
    try:
        _seed("FOO", tech._OHLCV_TTL_SECONDS + 10)
        assert tech._cache_expired("FOO", "1d") is True
    finally:
        tech.clear_cache()


def test_live_frame_within_ttl_is_fresh():
    tech.clear_cache()
    tech.clear_backtest_mode()
    try:
        _seed("FOO", 1.0)
        assert tech._cache_expired("FOO", "1d") is False
    finally:
        tech.clear_cache()


def test_backtest_mode_never_expires():
    tech.clear_cache()
    try:
        _seed("FOO", tech._OHLCV_TTL_SECONDS + 10_000)
        tech.set_backtest_mode(pd.Timestamp("2020-01-01"))
        assert tech._cache_expired("FOO", "1d") is False
    finally:
        tech.clear_backtest_mode()
        tech.clear_cache()


def test_clear_cache_drops_timestamps():
    tech.clear_backtest_mode()
    _seed("FOO", 1.0)
    tech.clear_cache()
    assert "FOO" not in tech._ohlcv_cache_ts
    assert "FOO" not in tech._ohlcv_cache
