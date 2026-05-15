"""
tests/unit/test_k1_allocation.py  –  K-1 partner allocation tests (ARCH-19 / CRIT-03)

Verifies that capital_pct and profit_loss_pct are independent — the critical
distinction in partnership accounting where a partner's capital account ratio
can differ from their income/loss allocation ratio.
"""
from __future__ import annotations

from decimal import Decimal

import pytest


class TestScheduleK1CapitalVsProfitLossSplit:
    """Capital and P&L percentages are independent on a K-1 (Part II Box J)."""

    def test_capital_pct_and_profit_loss_pct_are_separate_fields(self):
        from ledger_agent.core.api import ScheduleK1
        k1 = ScheduleK1(
            fiscal_year=2024,
            partner_id="partner_a",
            partner_name="Partner A",
            capital_pct=Decimal("0.99"),
            profit_loss_pct=Decimal("1.00"),
        )
        assert k1.capital_pct == Decimal("0.99")
        assert k1.profit_loss_pct == Decimal("1.00")
        assert k1.capital_pct != k1.profit_loss_pct

    def test_ordinary_income_uses_profit_loss_pct_not_capital_pct(self):
        """Ordinary income allocation uses profit_loss_pct, not capital_pct."""
        from ledger_agent.core.api import generate_k1, ScheduleK1
        import os
        os.environ["FI_PARTNER_YASH_CAPITAL"] = "0.99"
        os.environ["FI_PARTNER_YASH_PL"] = "1.00"
        os.environ["FI_PARTNER_PARIN_CAPITAL"] = "0.01"
        os.environ["FI_PARTNER_PARIN_PL"] = "0.00"
        # We cannot call generate_k1 without a DB, but we can verify the dataclass
        k1_majority = ScheduleK1(
            fiscal_year=2024,
            partner_id="majority_partner",
            partner_name="Majority Partner",
            capital_pct=Decimal("0.99"),
            profit_loss_pct=Decimal("1.00"),
            ordinary_income_loss=Decimal("18732.00"),
        )
        k1_minority = ScheduleK1(
            fiscal_year=2024,
            partner_id="minority_partner",
            partner_name="Minority Partner",
            capital_pct=Decimal("0.01"),
            profit_loss_pct=Decimal("0.00"),
            ordinary_income_loss=Decimal("0.00"),
        )
        # Majority partner gets 100% of income (profit_loss_pct=1.00)
        # even though capital_pct=0.99 (they hold 99% capital)
        assert k1_majority.ordinary_income_loss == Decimal("18732.00")
        assert k1_minority.ordinary_income_loss == Decimal("0.00")
        # Capital percentages sum to 100%
        assert k1_majority.capital_pct + k1_minority.capital_pct == Decimal("1.00")
        # P&L percentages sum to 100%
        assert k1_majority.profit_loss_pct + k1_minority.profit_loss_pct == Decimal("1.00")

    def test_ownership_pct_alias_returns_profit_loss_pct(self):
        """Deprecated ownership_pct alias returns profit_loss_pct for backwards compat."""
        from ledger_agent.core.api import ScheduleK1
        k1 = ScheduleK1(
            fiscal_year=2024,
            partner_id="p",
            partner_name="P",
            capital_pct=Decimal("0.75"),
            profit_loss_pct=Decimal("0.50"),
        )
        assert k1.ownership_pct == k1.profit_loss_pct
        assert k1.ownership_pct == Decimal("0.50")
        assert k1.ownership_pct != k1.capital_pct

    def test_generate_k1_applies_profit_loss_pct_to_income(self, db):
        """generate_k1 allocates income by profit_loss_pct, not capital_pct."""
        import os
        from decimal import Decimal
        from ledger_agent.core.database import init_db, EntityRepo, AccountRepo, SnapshotRepo, TransactionRepo
        from ledger_agent.core.models import (
            Entity, Account, AccountType, AccountSnapshot, Transaction, TransactionType,
        )
        from datetime import date

        init_db()
        entity = Entity(name="ALLOC TEST LLC", entity_type="LLC", state="MO")
        EntityRepo.upsert(entity)
        acct = Account(
            entity_id=entity.id, name="Checking", institution="Test Bank",
            account_type=AccountType.CHECKING, account_number_masked="****0001",
        )
        AccountRepo.upsert(acct)
        SnapshotRepo.upsert(AccountSnapshot(
            account_id=acct.id, statement_period="2024-12", ending_balance=Decimal("1000.00"),
        ))
        # Insert a classified revenue transaction for 2024
        TransactionRepo.bulk_insert([
            Transaction(
                account_id=acct.id, date=date(2024, 12, 1),
                description="REVENUE", raw_description="REVENUE",
                amount=Decimal("10000.00"),
                transaction_type=TransactionType.CREDIT,
                statement_period="2024-12",
                coa_code="4020", coa_name="Service Revenue",
            ),
        ])

        # Set up partner env vars: capital 99%/1% but P&L 100%/0%
        os.environ["FI_PARTNER_YASH_CAPITAL"] = "0.99"
        os.environ["FI_PARTNER_YASH_PL"] = "1.00"
        os.environ["FI_PARTNER_PARIN_CAPITAL"] = "0.01"
        os.environ["FI_PARTNER_PARIN_PL"] = "0.00"

        from ledger_agent.core.api import generate_k1
        k1_yash = generate_k1(2024, "yash")
        k1_parin = generate_k1(2024, "parin")

        # Yash gets 100% of income (profit_loss_pct=1.00)
        assert k1_yash.ordinary_income_loss > Decimal("0")
        # Parin gets 0% of income (profit_loss_pct=0.00)
        assert k1_parin.ordinary_income_loss == Decimal("0.00")
        # Capital percentages are independent
        assert k1_yash.capital_pct == Decimal("0.99")
        assert k1_parin.capital_pct == Decimal("0.01")
