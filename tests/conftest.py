"""
tests/conftest.py  –  Shared pytest fixtures for FinancialIntelligence
"""
from __future__ import annotations

import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

# Ensure the project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Early env isolation (before any test module import / collection) ──────────
# pytest_configure fires before collection, so config.py reads FI_DB_PATH
# from this temp dir rather than locking to the production DB path.

_TEST_TMP_DIR: Path | None = None


def pytest_configure(config):
    """Set FI_* env vars before any source module is imported during collection.

    Without this, config.py is imported at collection time with no FI_DB_PATH
    set, causing DB_PATH to default to the production database.  The session-
    scoped fixture below then fires *after* collection and cannot override the
    already-cached module-level DB_PATH.
    """
    global _TEST_TMP_DIR
    tmp = Path(tempfile.mkdtemp(prefix="fi_test_"))
    _TEST_TMP_DIR = tmp
    os.environ.setdefault("FI_DATA_DIR", str(tmp))
    os.environ.setdefault("FI_DB_PATH", str(tmp / "test.db"))
    os.environ.setdefault("FI_MEMORY_FILE", str(tmp / "test_memory.json"))
    os.environ.setdefault("FI_AI_BACKEND", "local")
    os.environ.setdefault("FI_LOG_LEVEL", "WARNING")
    os.environ.setdefault("FI_AUDIT_DISABLED", "1")


# ── Env var setup ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True, scope="session")
def set_test_env(tmp_path_factory):
    """Ensure env vars point to an isolated temp database/memory for the session.

    pytest_configure already set defaults; this fixture reinforces them and
    keeps the same tmp dir alive for the session lifetime.
    """
    tmp = tmp_path_factory.mktemp("fi_test_data")
    os.environ["FI_DATA_DIR"] = str(tmp)
    os.environ["FI_DB_PATH"] = str(tmp / "test.db")
    os.environ["FI_MEMORY_FILE"] = str(tmp / "test_memory.json")
    os.environ["FI_AI_BACKEND"] = "local"
    os.environ["FI_LOG_LEVEL"] = "WARNING"
    os.environ["FI_AUDIT_DISABLED"] = "1"
    yield
    # Teardown: env vars cleaned up by OS


# ── DB fixture ────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    """Fresh per-test database; restores FI_DB_PATH to the session value on teardown."""
    prev_db_path = os.environ.get("FI_DB_PATH")
    db_path = tmp_path / "test.db"
    os.environ["FI_DB_PATH"] = str(db_path)
    from ledger_agent.core.database import init_db
    init_db()
    yield db_path
    # Restore the session-level DB path so subsequent tests are not affected.
    if prev_db_path is not None:
        os.environ["FI_DB_PATH"] = prev_db_path
    else:
        os.environ.pop("FI_DB_PATH", None)
    # DB file is removed by tmp_path teardown at session end.


# ── Model factories ────────────────────────────────────────────────────────────

@pytest.fixture
def make_entity():
    def _make(name="TEST LLC", entity_type="LLC", state="MO"):
        from ledger_agent.core.models import Entity
        return Entity(name=name, entity_type=entity_type, state=state)

    return _make


@pytest.fixture
def make_account(make_entity):
    def _make(entity_id=None, name="Business Checking", institution="Bank X",
              account_type=None):
        from ledger_agent.core.models import Account, AccountType
        return Account(
            entity_id=entity_id or "test-entity-id",
            name=name,
            institution=institution,
            account_type=account_type or AccountType.CHECKING,
            account_number_masked="****1234",  # redaction: allow
        )

    return _make


@pytest.fixture
def make_transaction():
    def _make(
            account_id="test-acct-id",
            date_str="2025-01-15",
            description="QUICKBOOKS ONLINE",
            amount="-30.00",
            coa_code=None,
    ):
        from ledger_agent.core.models import Transaction, TransactionType
        from datetime import date
        y, m, d = date_str.split("-")
        return Transaction(
            account_id=account_id,
            date=date(int(y), int(m), int(d)),
            description=description,
            raw_description=description,
            amount=Decimal(amount),
            transaction_type=TransactionType.DEBIT,
            statement_period=f"{y}-{m}",
            coa_code=coa_code,
        )

    return _make


@pytest.fixture
def make_position():
    def _make(account_id="test-acct-id", symbol="SNAP", quantity="3000",
              price="11.29", period="2025-01"):
        from ledger_agent.core.models import Position
        qty = Decimal(quantity)
        ppu = Decimal(price)
        return Position(
            account_id=account_id,
            symbol=symbol,
            name=f"{symbol} Inc",
            quantity=qty,
            price_per_unit=ppu,
            market_value=qty * ppu,
            statement_period=period,
        )

    return _make


# ── Sample raw text fixtures ──────────────────────────────────────────────────
#
# Real-corpus samples are NEVER committed to the repo (they contain real
# institution names and figures that match private/institutions.py).  At
# runtime we look in this order:
#
#   1. tests/fixtures/private/<name>.txt        — gitignored real samples
#   2. tests/fixtures/<name>.example.txt        — committed pseudonymised
#                                                  samples matching
#                                                  private/institutions.example.py
#
# Tests that strictly require real-corpus accuracy (e.g., balance-extraction
# assertions tied to real figures) use SAMPLE_REAL_AVAILABLE to skip when the
# private file is missing.

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_PRIVATE_FIXTURE_DIR = _FIXTURE_DIR / "private"


def _load_sample(name: str) -> str:
    """Load sample text — prefer private real corpus, fall back to committed example."""
    private = _PRIVATE_FIXTURE_DIR / f"{name}.txt"
    if private.exists():
        return private.read_text(encoding="utf-8")
    example = _FIXTURE_DIR / f"{name}.example.txt"
    if example.exists():
        return example.read_text(encoding="utf-8")
    return ""


def _sample_is_real(name: str) -> bool:
    """True if the private (real-corpus) version of the sample is available."""
    return (_PRIVATE_FIXTURE_DIR / f"{name}.txt").exists()


BANK_X_SAMPLE_TEXT = _load_sample("bank_x_sample")
BROKER_Y_SAMPLE_TEXT = _load_sample("broker_y_sample")
BANK_X3_SAMPLE_TEXT = _load_sample("bank_x3_sample")
BANK_X2_SAMPLE_TEXT = _load_sample("bank_x2_sample")
BROKER_Z_SAMPLE_TEXT = _load_sample("broker_z_sample")
BANK_X4_CHECKING_SAMPLE_TEXT = _load_sample("bank_x4_checking_sample")
BANK_X4_CC_SAMPLE_TEXT = _load_sample("bank_x4_creditcard_sample")

BANK_X_REAL_AVAILABLE = _sample_is_real("bank_x_sample")
BROKER_Y_REAL_AVAILABLE = _sample_is_real("broker_y_sample")
BANK_X3_REAL_AVAILABLE = _sample_is_real("bank_x3_sample")
BANK_X2_REAL_AVAILABLE = _sample_is_real("bank_x2_sample")
BROKER_Z_REAL_AVAILABLE = _sample_is_real("broker_z_sample")
BANK_X4_CHECKING_REAL_AVAILABLE = _sample_is_real("bank_x4_checking_sample")
BANK_X4_CC_REAL_AVAILABLE = _sample_is_real("bank_x4_creditcard_sample")
