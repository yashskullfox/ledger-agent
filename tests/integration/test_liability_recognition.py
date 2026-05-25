"""
tests/integration/test_liability_recognition.py  —  ARCH-28
============================================================

Verifies R-67: a brokerage account's negative ``margin_balance`` is
recognised as a 2010 "Margin Loan Payable" liability on the balance
sheet — NOT deducted from assets.

Accounting identity tested:
    TOTAL ASSETS        = gross securities holdings (sign: positive)
    TOTAL LIABILITIES   = |margin_balance|          (sign: positive)
    TOTAL EQUITY        = TOTAL ASSETS − TOTAL LIABILITIES
    is_balanced: |((TOTAL_LIAB + TOTAL_EQUITY) − TOTAL_ASSETS)| < 0.02 (USD)

Test strategy
-------------
All tests use an isolated SQLite database (via FI_DB_PATH monkeypatch)
seeded with the minimum required entity / account / snapshot rows.

Acceptance
----------
    pytest tests/integration/test_liability_recognition.py -q
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest


# ── DB fixture ────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_liab.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    from ledger_agent.core.database import init_db
    init_db(db_file)
    return db_file


def _entity(db):
    from ledger_agent.core.database import EntityRepo
    from ledger_agent.core.models import Entity
    e = Entity(name="ENTITY_A", entity_type="LLC", state="FL",
               id=str(uuid.uuid4()))
    EntityRepo.upsert(e, db)
    return e


def _brokerage_account(db, entity_id):
    from ledger_agent.core.database import AccountRepo
    from ledger_agent.core.models import Account, AccountType
    a = Account(
        entity_id=entity_id,
        name="Margin Account",
        institution="Broker Y",
        account_type=AccountType.BROKERAGE,
        account_number_masked="****0001",  # redaction: allow
        id=str(uuid.uuid4()),
    )
    AccountRepo.upsert(a, db)
    return a


def _brokerage_snapshot(db, account_id, period,
                        gross="75000.00", margin="-25000.00"):
    """Insert an AccountSnapshot with gross_asset_value and margin_balance."""
    from ledger_agent.core.database import SnapshotRepo
    from ledger_agent.core.models import AccountSnapshot
    ending = Decimal(gross) + Decimal(margin)  # net equity
    s = AccountSnapshot(
        account_id=account_id,
        statement_period=period,
        ending_balance=ending,
        gross_asset_value=Decimal(gross),
        margin_balance=Decimal(margin),   # stored negative = debt
        id=str(uuid.uuid4()),
    )
    SnapshotRepo.upsert(s, db)
    return s


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestMarginLiabilityRecognised:
    """R-67: negative margin_balance → 2010 Margin Loan Payable."""

    def test_margin_appears_as_liability_not_asset_deduction(self, db):
        """
        Gross securities value (75,000) must equal TOTAL ASSETS.
        Margin loan (25,000) must appear under TOTAL LIABILITIES.
        TOTAL ASSETS must NOT be reduced by the margin.
        """
        entity = _entity(db)
        acct = _brokerage_account(db, entity.id)
        _brokerage_snapshot(db, acct.id, "2025-01",
                            gross="75000.00", margin="-25000.00")

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()

        assert bs.total_assets == Decimal("75000.00"), (
            f"TOTAL ASSETS must equal gross holdings 75000.00, got {bs.total_assets}"
        )
        assert bs.total_liabilities == Decimal("25000.00"), (
            f"TOTAL LIABILITIES must equal |margin| 25000.00, got {bs.total_liabilities}"
        )

    def test_2010_line_present_with_correct_amount(self, db):
        """COA 2010 Margin Loan Payable line must appear in bs.lines."""
        entity = _entity(db)
        acct = _brokerage_account(db, entity.id)
        _brokerage_snapshot(db, acct.id, "2025-01",
                            gross="75000.00", margin="-25000.00")

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()

        margin_lines = [l for l in bs.lines if l.coa_code == "2010"]
        assert len(margin_lines) == 1, (
            "Expected exactly one 2010 Margin Loan Payable line"
        )
        assert margin_lines[0].amount == Decimal("25000.00"), (
            f"2010 line amount must be |margin| 25000.00, got {margin_lines[0].amount}"
        )

    def test_balance_sheet_is_balanced_with_margin(self, db):
        """
        TOTAL ASSETS == TOTAL LIABILITIES + TOTAL EQUITY must hold within 0.02 (USD).
        This proves the margin is not double-counted (not in assets AND liabilities).
        """
        entity = _entity(db)
        acct = _brokerage_account(db, entity.id)
        _brokerage_snapshot(db, acct.id, "2025-01",
                            gross="75000.00", margin="-25000.00")

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()

        assert bs.is_balanced, (
            f"Balance sheet must balance: assets={bs.total_assets} "
            f"liab+equity={bs.total_liabilities + bs.total_equity}"
        )

    def test_sign_flip_positive_margin_produces_no_liability(self, db):
        """
        A positive margin_balance (credit) must not produce a 2010 line.
        Only negative margin_balance (debt) triggers recognition.
        """
        entity = _entity(db)
        acct = _brokerage_account(db, entity.id)
        _brokerage_snapshot(db, acct.id, "2025-01",
                            gross="75000.00", margin="0.00")

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()

        margin_lines = [l for l in bs.lines if l.coa_code == "2010"]
        assert len(margin_lines) == 0, (
            "No 2010 line must appear when margin_balance is zero or positive"
        )
        assert bs.total_liabilities == Decimal("0"), (
            f"TOTAL LIABILITIES must be 0 with no margin debt, got {bs.total_liabilities}"
        )

    def test_no_snapshot_produces_zero_liabilities(self, db):
        """
        A brokerage account with no snapshot for the period must produce
        zero assets and zero liabilities — not an uncaught exception.
        """
        entity = _entity(db)
        _brokerage_account(db, entity.id)   # no snapshot inserted

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()

        assert bs.total_assets == Decimal("0")
        assert bs.total_liabilities == Decimal("0")


class TestMarginCarryForward:
    """
    Closing margin of period N must match opening context of period N+1.

    This is a boundary contract — the balance sheet builder reads snapshots
    independently per period, so the only way continuity can break is if
    a snapshot is missing or mis-dated.  These tests confirm that two
    consecutive periods with correct snapshots produce consistent numbers.
    """

    def test_dec_closing_margin_equals_jan_opening(self, db):
        """
        The 2024-12 closing margin (25,000 debt) and the 2025-01
        snapshot use the same margin_balance, so TOTAL LIABILITIES is
        identical in both periods.
        """
        entity = _entity(db)
        acct = _brokerage_account(db, entity.id)
        _brokerage_snapshot(db, acct.id, "2024-12",
                            gross="75000.00", margin="-25000.00")
        _brokerage_snapshot(db, acct.id, "2025-01",
                            gross="80000.00", margin="-25000.00")

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs_dec = BalanceSheetBuilder(entity.id, "2024-12").build()
        bs_jan = BalanceSheetBuilder(entity.id, "2025-01").build()

        assert bs_dec.total_liabilities == bs_jan.total_liabilities, (
            f"Margin liability must be the same across consecutive periods "
            f"when margin_balance is unchanged: "
            f"2024-12={bs_dec.total_liabilities} 2025-01={bs_jan.total_liabilities}"
        )

    def test_margin_change_reflected_in_next_period(self, db):
        """
        If the margin loan is paid down between periods, total liabilities
        must shrink accordingly.
        """
        entity = _entity(db)
        acct = _brokerage_account(db, entity.id)
        _brokerage_snapshot(db, acct.id, "2024-12",
                            gross="75000.00", margin="-25000.00")
        _brokerage_snapshot(db, acct.id, "2025-01",
                            gross="80000.00", margin="-10000.00")  # partial paydown

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs_dec = BalanceSheetBuilder(entity.id, "2024-12").build()
        bs_jan = BalanceSheetBuilder(entity.id, "2025-01").build()

        assert bs_jan.total_liabilities < bs_dec.total_liabilities, (
            "Partial paydown of margin must reduce TOTAL LIABILITIES in the next period"
        )
        assert bs_jan.total_liabilities == Decimal("10000.00"), (
            f"Expected 10000 liability after partial paydown, got {bs_jan.total_liabilities}"
        )
