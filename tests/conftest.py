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

CHASE_SAMPLE_TEXT = """\
JPMorgan Chase Bank, N.A.
SYNCED LLC
BUSINESS COMPLETE CHECKING  (...1234)
January 1, 2025 through January 31, 2025

DEPOSITS AND ADDITIONS
DATE        DESCRIPTION                                 AMOUNT
01/15       Online Transfer from savings                1,500.00
01/22       Zelle payment from JOHN DOE                   250.00
Total deposits and additions                           $1,750.00

ATM & DEBIT CARD WITHDRAWALS
DATE        DESCRIPTION                                 AMOUNT
01/03       RECURRING DEBIT CARD PURCHASE QUICKBOOKS      30.00
01/10       RECURRING DEBIT CARD PURCHASE GOOGLE LLC       13.00

ELECTRONIC WITHDRAWALS
DATE        DESCRIPTION                                 AMOUNT
01/07       Zelle To JANE DOE                             500.00

Account Summary
Beginning balance                                      $3,000.00
Ending balance                                         $4,207.00
"""

BOFA_SAMPLE_TEXT = """\
Bank of America
SYNCED LLC
Business Checking (...5678)
Statement Period: 01/01/2025 – 01/31/2025

Deposits and other credits
Date   Description                                    Amount
01/06  Online Banking transfer from savings           2,000.00
01/20  Zelle Credit from JOHN DOE                       500.00
Total deposits and other credits                      $2,500.00

Withdrawals and other debits
Date   Description                                    Amount
01/07  RECURRING PAYMENT authorized on 01/07 SLACK       9.00
01/14  USATAXPYMT IRS                                   72.95

Account Summary
Beginning balance  $1,500.00
Ending balance     $3,918.05
"""

IBKR_SAMPLE_TEXT = """\
Interactive Brokers LLC
Activity Statement
Account: U1234567
SYNCED LLC
Period: January 1, 2025 - January 31, 2025

Trades
Symbol  Date/Time             Quantity  Price    Proceeds   P/L
SNAP    2025-01-10 09:30:00   1000      11.50    11500.00   250.00

Cash Report
Starting Cash Balance                          $5,000.00
Deposits                                       $2,000.00
Withdrawals                                      ($500.00)
Commissions                                        ($7.50)
Ending Cash Balance                            $6,492.50

Open Positions
Symbol  Quantity  Price  Market Value  Cost Basis
SNAP    3000      11.29  33870.00      32500.00
"""

USBANK_CHECKING_SAMPLE_TEXT = """\
U.S. Bank
Business Essentials Checking
Account: ****7428
Statement Period: March 1 - March 31, 2026

Other Deposits
DateDescription of TransactionRef NumberAmount
Mar4Ext Tfr DepositTRN #= EB089E3FAD0686B$800.00

Other Withdrawals
DateDescription of TransactionRef NumberAmount
Mar16Internet Banking PaymentTo Acct *************4594$925.44-
Mar20Service ChargeMonthly Maintenance Fee$25.00-

Beginning Balance: $1,200.00
Ending Balance: $1,049.56
"""

USBANK_CC_SAMPLE_TEXT = """\
U.S. Bank
Business Triple Cash Rewards Visa Card
Cardmember: SYNCED LLC
Account: ****4594
Statement Period: 02/01/2026 - 02/28/2026

Purchases and Other Debits
02/1702/135614IN *HACKING LAW 314-9618200 MO$3,000.00
02/2002/185789AMAZON.COM AMZN.COM/BILLWA$89.99

Payments and Other Credits
02/2502/23PAYMENT THANK YOU$500.00CR

New Balance: $3,211.93
Credit Limit: $10,000.00
"""
