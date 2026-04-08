"""
Global fixtures for the test suite.

We stub out heavy infrastructure (SQLAlchemy, APScheduler, external HTTP clients)
so unit tests run without Docker or a real database.
"""
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

# ── Ensure project root is importable ──────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── Stub sqlalchemy before any app module imports it ───────────────────────
# app/core/__init__.py imports database.py which requires sqlalchemy.
# We mock the entire database module so tests don't need the DB stack.
_db_stub = types.ModuleType("app.core.database")
_db_stub.get_db = AsyncMock()
_db_stub.init_db = AsyncMock()
sys.modules["app.core.database"] = _db_stub

# Also stub asyncpg / sqlalchemy so indirect imports don't fail
for _mod in [
    "sqlalchemy",
    "sqlalchemy.ext",
    "sqlalchemy.ext.asyncio",
    "asyncpg",
    "psycopg2",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import pytest


@pytest.fixture(autouse=True)
def patch_settings_data_dir(tmp_path, monkeypatch):
    """
    Redirect settings.data_dir to a temp directory for every test.
    Prevents tests from touching real data/ files.
    """
    from app.core import config
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    return tmp_path
