"""
tests/unit/test_continuity.py  –  Tests for fiscal-year carry-forward continuity (ARCH-31)
"""
from __future__ import annotations

from decimal import Decimal

import pytest


class TestCheckPeriodContinuity:
    def test_returns_none_when_no_data(self, db):
        from ledger_agent.core.accounting.continuity import check_period_continuity
        result = check_period_continuity("nonexistent-entity", "2025-01", "2025-02")
        assert result is None

    def test_zero_delta_when_balances_equal(self, db):
        from ledger_agent.core.database import init_db, EntityRepo, AccountRepo, SnapshotRepo
        from ledger_agent.core.models import Entity, Account, AccountType, AccountSnapshot
        from ledger_agent.core.accounting.continuity import check_period_continuity
        init_db()
        entity = Entity(name="CONT TEST LLC", entity_type="LLC", state="MO")
        EntityRepo.upsert(entity)
        acct = Account(
            entity_id=entity.id, name="Checking", institution="Test Bank",
            account_type=AccountType.CHECKING, account_number_masked="****9999",  # redaction: allow
        )
        AccountRepo.upsert(acct)
        SnapshotRepo.upsert(AccountSnapshot(
            account_id=acct.id, statement_period="2025-01", ending_balance=Decimal("1000.00"),
        ))
        SnapshotRepo.upsert(AccountSnapshot(
            account_id=acct.id, statement_period="2025-02", ending_balance=Decimal("1000.00"),
        ))
        delta = check_period_continuity(entity.id, "2025-01", "2025-02")
        assert delta == Decimal("0")

    def test_nonzero_delta_detected(self, db):
        from ledger_agent.core.database import init_db, EntityRepo, AccountRepo, SnapshotRepo
        from ledger_agent.core.models import Entity, Account, AccountType, AccountSnapshot
        from ledger_agent.core.accounting.continuity import check_period_continuity
        init_db()
        entity = Entity(name="GAP TEST LLC", entity_type="LLC", state="MO")
        EntityRepo.upsert(entity)
        acct = Account(
            entity_id=entity.id, name="Checking", institution="Test Bank",
            account_type=AccountType.CHECKING, account_number_masked="****8888",  # redaction: allow
        )
        AccountRepo.upsert(acct)
        SnapshotRepo.upsert(AccountSnapshot(
            account_id=acct.id, statement_period="2025-03", ending_balance=Decimal("5000.00"),
        ))
        SnapshotRepo.upsert(AccountSnapshot(
            account_id=acct.id, statement_period="2025-04", ending_balance=Decimal("5500.00"),
        ))
        delta = check_period_continuity(entity.id, "2025-03", "2025-04")
        assert delta == Decimal("500.00")


class TestPriorPeriodAdjustmentEnum:
    def test_prior_period_adjustment_in_transaction_type(self):
        from ledger_agent.core.models import TransactionType
        assert TransactionType.PRIOR_PERIOD_ADJUSTMENT.value == "prior_period_adjustment"

    def test_prior_period_adjustment_is_valid_transaction_type(self):
        from ledger_agent.core.models import TransactionType
        all_values = [t.value for t in TransactionType]
        assert "prior_period_adjustment" in all_values


class TestListDiscontinuities:
    def test_empty_periods_returns_empty(self, db):
        from ledger_agent.core.accounting.continuity import list_discontinuities
        result = list_discontinuities("any-entity", [])
        assert result == []

    def test_single_period_returns_empty(self, db):
        from ledger_agent.core.accounting.continuity import list_discontinuities
        result = list_discontinuities("any-entity", ["2025-01"])
        assert result == []


class TestMaterialisePriorPeriodAdjustments:
    """Tests for ARCH-31 carry-forward write-back."""

    def _setup_entity_with_gap(self, db):
        from ledger_agent.core.database import init_db, EntityRepo, AccountRepo, SnapshotRepo
        from ledger_agent.core.models import Entity, Account, AccountType, AccountSnapshot
        init_db()
        entity = Entity(name="WRITEBACK TEST LLC", entity_type="LLC", state="MO")
        EntityRepo.upsert(entity)
        acct = Account(
            entity_id=entity.id, name="Checking", institution="Test Bank",
            account_type=AccountType.CHECKING, account_number_masked="****7777",  # redaction: allow
        )
        AccountRepo.upsert(acct)
        SnapshotRepo.upsert(AccountSnapshot(
            account_id=acct.id, statement_period="2024-12", ending_balance=Decimal("4000.00"),
        ))
        SnapshotRepo.upsert(AccountSnapshot(
            account_id=acct.id, statement_period="2025-01", ending_balance=Decimal("4600.00"),
        ))
        return entity

    def test_dry_run_returns_delta_without_writing(self, db):
        from ledger_agent.core.accounting.continuity import materialise_prior_period_adjustments
        from ledger_agent.core.database import TransactionRepo
        entity = self._setup_entity_with_gap(db)
        delta = materialise_prior_period_adjustments(
            entity.id, "2024-12", "2025-01", dry_run=True
        )
        assert delta == Decimal("600.00")
        # Nothing written to DB
        txns = TransactionRepo.list_for_period("2025-01")
        ppa_txns = [t for t in txns if t.coa_code == "9999"]
        assert len(ppa_txns) == 0

    def test_writes_adjustment_transaction(self, db):
        from ledger_agent.core.accounting.continuity import materialise_prior_period_adjustments
        from ledger_agent.core.database import TransactionRepo
        from ledger_agent.core.models import TransactionType
        entity = self._setup_entity_with_gap(db)
        delta = materialise_prior_period_adjustments(
            entity.id, "2024-12", "2025-01", dry_run=False
        )
        assert delta == Decimal("600.00")
        txns = TransactionRepo.list_for_period("2025-01")
        ppa_txns = [t for t in txns if t.transaction_type == TransactionType.PRIOR_PERIOD_ADJUSTMENT]
        assert len(ppa_txns) == 1
        assert ppa_txns[0].amount == Decimal("600.00")
        assert ppa_txns[0].coa_code == "9999"
        assert ppa_txns[0].statement_period == "2025-01"

    def test_returns_none_when_no_data(self, db):
        from ledger_agent.core.accounting.continuity import materialise_prior_period_adjustments
        result = materialise_prior_period_adjustments("no-such-entity", "2024-12", "2025-01")
        assert result is None

    def test_returns_zero_when_balanced(self, db):
        from ledger_agent.core.database import init_db, EntityRepo, AccountRepo, SnapshotRepo
        from ledger_agent.core.models import Entity, Account, AccountType, AccountSnapshot
        from ledger_agent.core.accounting.continuity import materialise_prior_period_adjustments
        init_db()
        entity = Entity(name="BALANCED LLC", entity_type="LLC", state="MO")
        EntityRepo.upsert(entity)
        acct = Account(
            entity_id=entity.id, name="Checking", institution="Test Bank",
            account_type=AccountType.CHECKING, account_number_masked="****6666",  # redaction: allow
        )
        AccountRepo.upsert(acct)
        SnapshotRepo.upsert(AccountSnapshot(
            account_id=acct.id, statement_period="2024-11", ending_balance=Decimal("2000.00"),
        ))
        SnapshotRepo.upsert(AccountSnapshot(
            account_id=acct.id, statement_period="2024-12", ending_balance=Decimal("2000.00"),
        ))
        delta = materialise_prior_period_adjustments(entity.id, "2024-11", "2024-12")
        assert delta == Decimal("0")
