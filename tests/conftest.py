"""
tests/conftest.py  –  Shared pytest fixtures for FinancialIntelligence
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

# Ensure the project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Env var setup ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True, scope="session")
def set_test_env(tmp_path_factory):
    """Set env vars so tests use an isolated temp database/memory."""
    tmp = tmp_path_factory.mktemp("fi_test_data")
    os.environ["FI_DATA_DIR"] = str(tmp)
    os.environ["FI_DB_PATH"] = str(tmp / "test.db")
    os.environ["FI_MEMORY_FILE"] = str(tmp / "test_memory.json")
    os.environ["FI_AI_BACKEND"] = "local"
    os.environ["FI_LOG_LEVEL"] = "WARNING"  # quiet during tests
    yield
    # Teardown: env vars cleaned up by OS


# ── DB fixture ────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    """Fresh in-memory-style database for each test."""
    db_path = tmp_path / "test.db"
    os.environ["FI_DB_PATH"] = str(db_path)
    from core.database import init_db
    init_db()
    yield db_path
    # File is removed by tmp_path teardown


# ── Model factories ────────────────────────────────────────────────────────────

@pytest.fixture
def make_entity():
    def _make(name="TEST LLC", entity_type="LLC", state="MO"):
        from core.models import Entity
        return Entity(name=name, entity_type=entity_type, state=state)

    return _make


@pytest.fixture
def make_account(make_entity):
    def _make(entity_id=None, name="Business Checking", institution="Truist Bank",
              account_type=None):
        from core.models import Account, AccountType
        return Account(
            entity_id=entity_id or "test-entity-id",
            name=name,
            institution=institution,
            account_type=account_type or AccountType.CHECKING,
            account_number_masked="****1234",
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
        from core.models import Transaction, TransactionType
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
        from core.models import Position
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

TRUIST_SAMPLE_TEXT = """\
TRUIST BANK
SYNCED LLC
SIMPLE BUSINESS CHECKING  1470018610272
For 01/31/2025

Account Summary
Previous balance as of 12/31/2024 $572.15
New balance as of 01/31/2025 = $4,031.20

Other withdrawals, debits and service charges
DATE  DESCRIPTION                     AMOUNT($)
01/09 DEBIT CARD RECURRING PYMT INCFILE LLC 0047  29.00
01/09 DEBIT CARD RECURRING PYMT QUICKBOOKS  30.00
01/14 DEBIT CARD RECURRING PYMT GOOGLE LLC  13.00
01/14 USATAXPYMT IRS                         72.95
01/28 DEBIT CARD PYMT ADOBE INC              54.99
01/28 DEBIT CARD PYMT GOOGLE WORKSPACE       14.00
Total other withdrawals $213.94

Deposits, credits and interest
DATE  DESCRIPTION                     AMOUNT($)
01/21 MONEYLINE FID BKG SVC LLC       2,250.00
01/24 MONEYLINE FID BKG SVC LLC       1,701.00
Total deposits, credits $3,951.00
"""

FIDELITY_SAMPLE_TEXT = """\
FIDELITY BROKERAGE SERVICES LLC
Fidelity Account Z23-123456
INVESTMENT REPORT
For Period January 1, 2025 – January 31, 2025
SYNCED LLC

Account Value (NAV):  $35,438.80
Margin Balance:       $(24,061.20)

Holdings
SNAP INC       SNAP   3,000  $11.29  $33,870.00
CASH                                  $1,630.00
"""
