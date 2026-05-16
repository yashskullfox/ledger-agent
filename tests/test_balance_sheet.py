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
        account_number_masked="****1234",  # redaction: allow
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
        # Assets = Liabilities + Equity (within $0.02)  # redaction: allow
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


class TestPlPeriodsAggregation:
    def test_net_income_aggregates_across_pl_periods(self, db):
        """Full-FY P&L = sum of all monthly P&L values."""
        from datetime import date
        from ledger_agent.core.database import (
            init_db, EntityRepo, AccountRepo, SnapshotRepo, TransactionRepo,
            get_conn,
        )
        from ledger_agent.core.models import (
            Entity, Account, AccountType, AccountSnapshot,
            Transaction, TransactionType,
        )
        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        init_db()
        with get_conn() as conn:
            for t in ("realised_trades", "transactions", "account_snapshots",
                      "positions", "imported_statements", "accounts", "entities"):
                conn.execute(f"DELETE FROM {t}")

        entity = Entity(name="TEST LLC", entity_type="LLC", state="MO")
        EntityRepo.upsert(entity)
        acct = Account(
            entity_id=entity.id, name="Checking", institution="Test Bank",
            account_type=AccountType.CHECKING, account_number_masked="****1234",  # redaction: allow
        )
        AccountRepo.upsert(acct)

        # Period 1 snapshot + transactions
        SnapshotRepo.upsert(AccountSnapshot(
            account_id=acct.id, statement_period="2025-01", ending_balance=Decimal("2000.00"),
        ))
        TransactionRepo.bulk_insert([
            Transaction(
                account_id=acct.id, date=date(2025, 1, 15), description="REV JAN",
                raw_description="REV JAN", amount=Decimal("1000.00"),
                transaction_type=TransactionType.CREDIT, statement_period="2025-01",
                coa_code="4000", coa_name="Revenue",
            ),
        ])

        # Period 2 snapshot + transactions
        SnapshotRepo.upsert(AccountSnapshot(
            account_id=acct.id, statement_period="2025-02", ending_balance=Decimal("2900.00"),
        ))
        TransactionRepo.bulk_insert([
            Transaction(
                account_id=acct.id, date=date(2025, 2, 15), description="REV FEB",
                raw_description="REV FEB", amount=Decimal("900.00"),
                transaction_type=TransactionType.CREDIT, statement_period="2025-02",
                coa_code="4000", coa_name="Revenue",
            ),
        ])

        bs = BalanceSheetBuilder(
            entity.id, "2025-02", pl_periods=["2025-01", "2025-02"]
        ).build()
        assert bs.net_income == Decimal("1900.00"), (
            f"Expected 1900 (1000+900), got {bs.net_income}"
        )

    def test_entity_id_set_on_balance_sheet(self, db):
        """BalanceSheet.entity_id is set from BalanceSheetBuilder."""
        from ledger_agent.core.database import init_db, EntityRepo, AccountRepo, SnapshotRepo
        from ledger_agent.core.models import Entity, Account, AccountType, AccountSnapshot
        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        init_db()
        entity = Entity(name="TEST LLC 2", entity_type="LLC", state="MO")
        EntityRepo.upsert(entity)
        acct = Account(
            entity_id=entity.id, name="Checking", institution="Bank",
            account_type=AccountType.CHECKING, account_number_masked="****5678",  # redaction: allow
        )
        AccountRepo.upsert(acct)
        SnapshotRepo.upsert(AccountSnapshot(
            account_id=acct.id, statement_period="2025-03", ending_balance=Decimal("100.00"),
        ))
        bs = BalanceSheetBuilder(entity.id, "2025-03").build()
        assert bs.entity_id == entity.id


class TestComputeNetLtcg:
    def test_5071_excluded_from_ltcg(self):
        """COA 5071 (Legal Fees) must NOT appear in LTCG calculation."""
        from datetime import date
        from ledger_agent.core.models import Transaction, TransactionType
        from ledger_agent.core.api import _compute_net_ltcg
        txns = [
            Transaction(
                account_id="a", date=date(2025, 1, 1),
                description="LEGAL FEE", raw_description="LEGAL FEE",
                amount=Decimal("-500.00"),
                transaction_type=TransactionType.DEBIT,
                statement_period="2025-01",
                coa_code="5071",
            ),
            Transaction(
                account_id="a", date=date(2025, 1, 2),
                description="LTCG GAIN", raw_description="LTCG GAIN",
                amount=Decimal("1000.00"),
                transaction_type=TransactionType.CREDIT,
                statement_period="2025-01",
                coa_code="4011",
            ),
            Transaction(
                account_id="a", date=date(2025, 1, 3),
                description="LTCG LOSS", raw_description="LTCG LOSS",
                amount=Decimal("-200.00"),
                transaction_type=TransactionType.DEBIT,
                statement_period="2025-01",
                coa_code="5075",
            ),
        ]
        result = _compute_net_ltcg(txns)
        # 5071 excluded: only 4011 (1000) + 5075 (-200) = 800
        assert result == Decimal("800.00"), (
            f"Expected 800.00 (4011+5075, 5071 excluded), got {result}"
        )

    def test_5075_included_in_ltcg(self):
        """COA 5075 (LTCG Loss) IS included in LTCG calculation."""
        from datetime import date
        from ledger_agent.core.models import Transaction, TransactionType
        from ledger_agent.core.api import _compute_net_ltcg
        txns = [
            Transaction(
                account_id="a", date=date(2025, 1, 3),
                description="LTCG LOSS", raw_description="LTCG LOSS",
                amount=Decimal("-200.00"),
                transaction_type=TransactionType.DEBIT,
                statement_period="2025-01",
                coa_code="5075",
            ),
        ]
        result = _compute_net_ltcg(txns)
        assert result == Decimal("-200.00")
