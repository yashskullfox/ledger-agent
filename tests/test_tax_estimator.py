"""
tests/test_tax_estimator.py  –  Unit tests for tax estimator

The legacy individual (SE + federal + state) method and its tests (``TestTaxEstimator``,
``TestRenderTaxEstimate``) were removed by card Q-02: every surface now renders
``api.pte_estimate`` (see tests/unit/test_cli_tax_pte.py).
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


class TestEntityPTET:
    """Q-04 / owner decision D1: entity-level state PTET only."""

    def test_taxable_income_includes_schedule_k_items(self):
        from ledger_agent.core.accounting.tax_estimator import entity_taxable_income

        ti = entity_taxable_income(
            ordinary_business_income=Decimal("10000"),
            net_short_term_capital_gain=Decimal("2000"),
            net_long_term_capital_gain=Decimal("3000"),
            dividend_income=Decimal("400"),
            interest_income=Decimal("100"),
            investment_interest_expense=Decimal("500"),
        )
        assert ti == Decimal("15000")

    def test_taxable_income_floors_at_zero(self):
        from ledger_agent.core.accounting.tax_estimator import entity_taxable_income

        assert entity_taxable_income(Decimal("1000"), Decimal("-5000")) == Decimal("0")
        assert entity_taxable_income(Decimal("-1000")) == Decimal("0")

    def test_ptet_is_state_rate_times_taxable_income(self):
        from ledger_agent.core.accounting.tax_estimator import STATE_INCOME_RATE, TaxEstimator

        est = TaxEstimator("TEST LLC", 2025).estimate_entity_ptet(Decimal("40000.00"))
        expected = (Decimal("40000.00") * STATE_INCOME_RATE).quantize(Decimal("0.01"))
        assert est.total_annual_tax == expected
        assert est.state_income_tax == expected
        # Partners' individual taxes are out of scope.
        assert est.se_tax == Decimal("0.00")
        assert est.federal_income_tax == Decimal("0.00")
        assert est.net_income == Decimal("40000.00")
        assert est.effective_rate == (STATE_INCOME_RATE * 100).quantize(Decimal("0.01"))

    def test_quarterly_schedule_is_annual_over_four(self):
        from ledger_agent.core.accounting.tax_estimator import QUARTERLY_DUE_DATES, TaxEstimator

        est = TaxEstimator("TEST LLC", 2025).estimate_entity_ptet(Decimal("48000.00"))
        q = (est.total_annual_tax / 4).quantize(Decimal("0.01"))
        assert [p.quarter for p in est.quarterly_payments] == ["Q1", "Q2", "Q3", "Q4"]
        assert all(p.amount == q for p in est.quarterly_payments)
        assert [p.due_date for p in est.quarterly_payments] == list(QUARTERLY_DUE_DATES.values())

    @pytest.mark.parametrize("income", ["0", "-2500.00"])
    def test_zero_or_negative_income_no_tax(self, income):
        from ledger_agent.core.accounting.tax_estimator import TaxEstimator

        est = TaxEstimator("TEST LLC", 2025).estimate_entity_ptet(Decimal(income))
        assert est.total_annual_tax == Decimal("0.00")
        assert est.effective_rate == Decimal("0")
        assert est.net_income == Decimal("0")
        assert all(p.amount == Decimal("0.00") for p in est.quarterly_payments)
        assert len(est.quarterly_payments) == 4

    def test_annualize_factor(self):
        from ledger_agent.core.accounting.tax_estimator import TaxEstimator

        qtd = TaxEstimator("TEST LLC", 2025).estimate_entity_ptet(
            Decimal("15000.00"), annualize_factor=Decimal("4")
        )
        annual = TaxEstimator("TEST LLC", 2025).estimate_entity_ptet(Decimal("60000.00"))
        assert qtd.net_income == Decimal("60000.00")
        assert qtd.total_annual_tax == annual.total_annual_tax
        assert any("annualized" in n for n in qtd.notes)

    def test_notes_state_entity_level_scope(self):
        from ledger_agent.core.accounting.tax_estimator import TaxEstimator

        for income in ("50000.00", "0"):
            notes = " ".join(
                TaxEstimator("TEST LLC", 2025).estimate_entity_ptet(Decimal(income)).notes
            )
            assert "Entity-level state PTET only" in notes
            assert "partners' individual estimated taxes are not included" in notes


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

        # Entity's first activity is March → Jan/Feb are pre-inception, Q1 is complete,
        # so the estimate is quarter-to-date through 2026-03, annualized ×4.
        assert est_before.basis == "quarter_to_date"
        assert est_before.through_period == "2026-03"
        assert est_before.ytd_net_income == Decimal("20000.00")  # redaction: allow
        q1_due = Decimal(str(est_before.quarterly_payments[0]["amount"]))

        # A partial payment does not mark the quarter paid.
        api.record_pte_payment(2026, "Q1", Decimal("100.00"), paid_on="2026-04-01", source="MO-PTEAP")
        est_partial = api.pte_estimate(2026)
        q1_partial = next(p for p in est_partial.quarterly_payments if p["quarter"] == "Q1")
        assert q1_partial["status"] == "partial"
        assert api.build_customer_summary(2026, as_of="2026-03-31").pte_due_signal[
            "next_due"
        ] == "April 15"

        # record_pte_payment stores ONE amount per quarter (re-recording replaces it).
        rec = api.record_pte_payment(2026, "Q1", q1_due, paid_on="2026-04-15", source="MO-PTEAP")
        assert rec["quarter"] == "Q1"

        est_after = api.pte_estimate(2026)
        assert est_after.paid_ytd == q1_due
        assert est_after.remaining_balance == est_before.total_annual_tax - q1_due
        q1_entry = next(p for p in est_after.quarterly_payments if p["quarter"] == "Q1")
        assert q1_entry["status"] == "paid"
        assert Decimal(str(q1_entry["paid_amount"])) == q1_due

        summary = api.build_customer_summary(2026, as_of="2026-03-31")
        assert Decimal(summary.pte_due_signal["paid_ytd"]) == q1_due
        # Q1 is fully paid, so next_due advances to Q2 (June 15)
        assert summary.pte_due_signal["next_due"] == "June 15"

