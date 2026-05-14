"""
tests/integration/conftest.py  –  Integration / parity test fixtures
=====================================================================

The root conftest redirects FI_DB_PATH to an isolated temp database so
unit tests never touch production data.  Integration and parity tests
are the exception: they MUST run against the real populated database to
be meaningful.

This conftest overrides FI_DB_PATH back to the production path for the
duration of each integration test module, then restores it on teardown.

If the production DB does not exist (CI without imported statements),
every test in the module skips with a clear message — they never
silently pass against empty data (R-50).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Resolve production DB path relative to the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PROD_DB = _REPO_ROOT / "data" / "db" / "financials.db"


@pytest.fixture(autouse=True, scope="module")
def use_production_db():
    """Point FI_DB_PATH at the production database for this test module.

    Runs after the session-scoped ``set_test_env`` fixture (which redirects
    to a temp DB), so it correctly overrides it.  Restored on module teardown.

    Skips the entire module when the production DB is absent so tests
    never silently pass without real data.
    """
    if not _PROD_DB.exists():
        pytest.skip(
            f"Production database not found at {_PROD_DB}. "
            "Run 'ledger scan' to import statements before running "
            "integration/parity tests."
        )

    prev = os.environ.get("FI_DB_PATH")
    os.environ["FI_DB_PATH"] = str(_PROD_DB)
    yield
    # Restore previous value so the rest of the session stays isolated.
    if prev is not None:
        os.environ["FI_DB_PATH"] = prev
    else:
        os.environ.pop("FI_DB_PATH", None)
