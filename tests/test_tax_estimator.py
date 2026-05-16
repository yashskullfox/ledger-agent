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
        # Should not raise; rich or plain output
        render_tax_estimate(est)
