"""
tests/test_balance_sheet.py  –  Unit tests for balance sheet builder
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture
def seeded_db(db):
    """Create entity, accounts, snapshot and transactions for a balance sheet test."""
    from ledger_agent.core.database import (
        init_db, EntityRepo, AccountRepo, SnapshotRepo, TransactionRepo,
        get_conn,
    )
    from ledger_agent.core.models import (
        Entity, Account, AccountType, AccountSnapshot,
        Transaction, TransactionType,
    )
    init_db()
    # Clear tables in FK-safe order (children before parents)
    with get_conn() as conn:
        for table in ("realised_trades", "transactions", "account_snapshots",
                      "positions", "imported_statements",
                      "accounts", "entities"):
            conn.execute(f"DELETE FROM {table}")

    entity = Entity(name="TEST LLC", entity_type="LLC", state="MO")
    EntityRepo.upsert(entity)

    checking = Account(
        entity_id=entity.id,
        name="Business Checking",
        institution="Test Bank",
        account_type=AccountType.CHECKING,
        account_number_masked="****1234",
    )
    AccountRepo.upsert(checking)

    snap = AccountSnapshot(
        account_id=checking.id,
        statement_period="2025-01",
        ending_balance=Decimal("5000.00"),
        beginning_balance=Decimal("1000.00"),
        total_debits=Decimal("500.00"),
        total_credits=Decimal("4500.00"),
    )
    SnapshotRepo.upsert(snap)

    # Revenue transaction
    rev_txn = Transaction(
        account_id=checking.id,
        date=date(2025, 1, 15),
        description="CLIENT PAYMENT",
        raw_description="CLIENT PAYMENT",
        amount=Decimal("4000.00"),
        transaction_type=TransactionType.CREDIT,
        statement_period="2025-01",
        coa_code="4000",
        coa_name="General Revenue",
    )
    # Expense transaction
    exp_txn = Transaction(
        account_id=checking.id,
        date=date(2025, 1, 9),
        description="QUICKBOOKS ONLINE",
        raw_description="QUICKBOOKS ONLINE",
        amount=Decimal("-30.00"),
        transaction_type=TransactionType.DEBIT,
        statement_period="2025-01",
        coa_code="5010",
        coa_name="Software & SaaS",
    )
    TransactionRepo.bulk_insert([rev_txn, exp_txn])

    return entity, checking, snap


class TestBalanceSheetBuilder:
    def test_builds_balance_sheet(self, seeded_db):
        entity, _, _ = seeded_db
        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()
        assert bs is not None
        assert bs.entity_name == "TEST LLC"
        assert bs.period == "2025-01"

    def test_total_assets_positive(self, seeded_db):
        entity, _, _ = seeded_db
        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()
        assert bs.total_assets > 0

    def test_balance_sheet_lines_not_empty(self, seeded_db):
        entity, _, _ = seeded_db
        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()
        assert len(bs.lines) > 0

    def test_net_income_calculated(self, seeded_db):
        entity, _, _ = seeded_db
        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()
        # Revenue 4000 - Expense 30 = Net income 3970
        assert bs.net_income == Decimal("3970.00")

    def test_is_balanced(self, seeded_db):
        entity, _, _ = seeded_db
        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()
        # Assets = Liabilities + Equity (within $0.02)
        diff = abs((bs.total_liabilities + bs.total_equity) - bs.total_assets)
        assert diff < Decimal("0.02"), f"Sheet not balanced: diff={diff}"

    def test_has_asset_lines(self, seeded_db):
        entity, _, _ = seeded_db
        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()
        assert len(bs.asset_lines()) > 0

    def test_has_equity_lines(self, seeded_db):
        entity, _, _ = seeded_db
        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()
        assert len(bs.equity_lines()) > 0


class TestBuildComparison:
    def test_returns_dict(self, seeded_db):
        entity, _, _ = seeded_db
        from ledger_agent.core.accounting.balance_sheet import build_comparison
        result = build_comparison(entity.id, ["2025-01"])
        assert isinstance(result, dict)
        assert "2025-01" in result

    def test_empty_periods_returns_empty(self, seeded_db):
        entity, _, _ = seeded_db
        from ledger_agent.core.accounting.balance_sheet import build_comparison
        result = build_comparison(entity.id, [])
        assert result == {}
