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
            account_type=AccountType.CHECKING, account_number_masked="****9999",
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
            account_type=AccountType.CHECKING, account_number_masked="****8888",
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
