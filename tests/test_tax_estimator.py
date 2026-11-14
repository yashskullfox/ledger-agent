"""
tests/test_tax_estimator.py  –  Unit tests for tax estimator
"""
from __future__ import annotations

import os
from decimal import Decimal

import pytest


@pytest.fixture(autouse=True)
def default_rates():
    """Ensure tax rate env vars are at predictable defaults for tests."""
    os.environ["FI_SE_TAX_RATE"] = "0.153"
    os.environ["FI_FED_INCOME_RATE"] = "0.22"
    os.environ["FI_STATE_TAX_RATE"] = "0.05"
    os.environ["FI_QBI_DEDUCTION"] = "0.20"
    yield


class TestTaxEstimator:
    def test_returns_estimate(self):
        from ledger_agent.core.accounting.tax_estimator import TaxEstimator
        est = TaxEstimator("TEST LLC", 2025).estimate_from_net_income(
            Decimal("50000.00")
        )
        assert est is not None
        assert est.total_annual_tax > 0

    def test_se_tax_calculated(self):
        from ledger_agent.core.accounting.tax_estimator import TaxEstimator
        est = TaxEstimator("TEST LLC", 2025).estimate_from_net_income(
            Decimal("10000.00")
        )
        # SE tax = 10000 * 0.153 = 1530
        assert est.se_tax == Decimal("1530.00")

    def test_zero_income_no_tax(self):
        from ledger_agent.core.accounting.tax_estimator import TaxEstimator
        est = TaxEstimator("TEST LLC", 2025).estimate_from_net_income(
            Decimal("0.00")
        )
        assert est.total_annual_tax == Decimal("0.00")
        assert est.effective_rate == Decimal("0.00")

    def test_quarterly_payments_count(self):
        from ledger_agent.core.accounting.tax_estimator import TaxEstimator
        est = TaxEstimator("TEST LLC", 2025).estimate_from_net_income(
            Decimal("100000.00")
        )
        assert len(est.quarterly_payments) == 4

    def test_quarterly_sum_equals_total(self):
        from ledger_agent.core.accounting.tax_estimator import TaxEstimator
        est = TaxEstimator("TEST LLC", 2025).estimate_from_net_income(
            Decimal("48000.00")
        )
        quarterly_sum = sum(p.amount for p in est.quarterly_payments)
        # Allow 0.04 rounding difference (4 quarters x 0.01)
        assert abs(quarterly_sum - est.total_annual_tax) <= Decimal("0.04")

    def test_annualize_factor(self):
        from ledger_agent.core.accounting.tax_estimator import TaxEstimator
        # 1 month = annualize × 12
        est_monthly = TaxEstimator("TEST LLC", 2025).estimate_from_net_income(
            Decimal("5000.00"), annualize_factor=Decimal("12")
        )
        est_annual = TaxEstimator("TEST LLC", 2025).estimate_from_net_income(
            Decimal("60000.00")
        )
        assert abs(est_monthly.total_annual_tax - est_annual.total_annual_tax) < Decimal("1.00")

    def test_notes_not_empty(self):
        from ledger_agent.core.accounting.tax_estimator import TaxEstimator
        est = TaxEstimator("TEST LLC", 2025).estimate_from_net_income(
            Decimal("50000.00")
        )
        assert len(est.notes) > 0

    def test_entity_name_preserved(self):
        from ledger_agent.core.accounting.tax_estimator import TaxEstimator
        est = TaxEstimator("MY COMPANY LLC", 2025).estimate_from_net_income(
            Decimal("50000.00")
        )
        assert est.entity_name == "MY COMPANY LLC"


class TestRenderTaxEstimate:
    def test_renders_without_exception(self, capsys):
        from ledger_agent.core.accounting.tax_estimator import TaxEstimator, render_tax_estimate
        est = TaxEstimator("TEST LLC", 2025).estimate_from_net_income(
            Decimal("60000.00")
        )
        render_tax_estimate(est)
        out = capsys.readouterr().out
        assert "TEST LLC" in out
        assert "Net Income" in out
        assert "TOTAL ESTIMATED TAX" in out

    def test_renders_plain_text_fallback_on_import_error(self, monkeypatch, capsys):
        import builtins

        from ledger_agent.core.accounting.tax_estimator import TaxEstimator, render_tax_estimate

        est = TaxEstimator("TEST LLC", 2025).estimate_from_net_income(
            Decimal("60000.00")
        )

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name.startswith("rich"):
                raise ImportError("No module named 'rich'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        render_tax_estimate(est)
        out = capsys.readouterr().out
        assert "=== Tax Estimate – TEST LLC (2025) ===" in out
        assert "Net Income (annual):" in out
        assert "60,000.00" in out


class TestExternalPTEPayments:
    """W31 / T7: Entity-level external PTE payment recording and deduction."""

    def test_record_pte_payment_updates_estimate_and_summary(self, tmp_path, monkeypatch):
        from datetime import date

        import ledger_agent.core.api as api
        from ledger_agent.core.database import AccountRepo, EntityRepo, TransactionRepo, init_db
        from ledger_agent.core.models import (
            Account,
            AccountType,
            Entity,
            Transaction,
            TransactionType,
        )

        db_file = tmp_path / "pte_test.db"
        monkeypatch.setenv("FI_DB_PATH", str(db_file))
        init_db(db_file)

        entity = Entity(id="ent-pte-1", name="ENTITY_A", entity_type="LLC", state="MO")
        EntityRepo.upsert(entity, db_file)
        acct = Account(
            id="acct-pte-1",
            entity_id=entity.id,
            name="Operating",
            institution="Bank X",
            account_type=AccountType.CHECKING,
            account_number_masked="1234",
        )
        AccountRepo.upsert(acct, db_file)
        TransactionRepo.bulk_insert(
            [
                Transaction(
                    id="txn-pte-1",
                    account_id=acct.id,
                    date=date(2026, 3, 15),
                    description="Consulting Revenue",
                    amount=Decimal("20000.00"),
                    transaction_type=TransactionType.CREDIT,
                    statement_period="2026-03",
                    coa_code="4020",
                    coa_name="Service Revenue",
                )
            ],
            db_file,
        )

        est_before = api.pte_estimate(2026)
        assert est_before.paid_ytd == Decimal("0.00")
        assert est_before.remaining_balance == est_before.total_annual_tax

        rec = api.record_pte_payment(2026, "Q1", Decimal("1300.00"), paid_on="2026-04-15", source="MO-PTEAP")
        assert rec["quarter"] == "Q1"
        assert rec["amount"] == "1300.00"  # redaction: allow

        est_after = api.pte_estimate(2026)
        assert est_after.paid_ytd == Decimal("1300.00")  # redaction: allow
        assert est_after.remaining_balance == est_before.total_annual_tax - Decimal("1300.00")  # redaction: allow
        q1_entry = next(p for p in est_after.quarterly_payments if p["quarter"] == "Q1")
        assert q1_entry["status"] == "paid"
        assert q1_entry["paid_amount"] == 1300.0  # redaction: allow

        summary = api.build_customer_summary(2026, as_of="2026-03-31")
        assert summary.pte_due_signal["paid_ytd"] == "1300.00"  # redaction: allow
        # Q1 is already paid, so next_due advances to Q2 (June 15)
        assert summary.pte_due_signal["next_due"] == "June 15"

