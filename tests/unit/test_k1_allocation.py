"""
tests/unit/test_k1_allocation.py  –  K-1 allocation unit tests (ARCH-19 / CRIT-03)
====================================================================================

Verifies that the PARTNERS table encodes the correct SYNCED LLC ownership split
and that generate_k1() routes income strictly according to profit_loss_pct, not
capital_pct.

CRIT-03 business invariants under test:
  - Yash:  capital=99%,  P&L=100%  → receives ALL ordinary income
  - Parin: capital=1%,   P&L=0%   → receives ZERO ordinary income
  - Yash + Parin ordinary income must sum to Form 1065 ordinary income

These tests use no database; generate_form_1065 is patched to return a
controlled Form1065 so the tests verify allocation math only.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ledger_agent.core.api import (
    PARTNERS,
    Form1065,
    ScheduleK1,
    _build_partners,
    generate_k1,
)


# ---------------------------------------------------------------------------
# PARTNERS table integrity (CRIT-03)
# ---------------------------------------------------------------------------

class TestPartnersTable:

    def test_yash_capital_pct(self):
        """Yash holds 99% of capital (K-1 Part II J)."""
        _, capital_pct, _ = PARTNERS["yash"]
        assert capital_pct == Decimal("0.99"), (
            f"Expected yash capital_pct=0.99, got {capital_pct}"
        )

    def test_yash_profit_loss_pct(self):
        """Yash receives 100% of P&L allocations (CRIT-03)."""
        _, _, pl_pct = PARTNERS["yash"]
        assert pl_pct == Decimal("1.00"), (
            f"Expected yash profit_loss_pct=1.00, got {pl_pct}"
        )

    def test_parin_capital_pct(self):
        """Parin holds 1% of capital (K-1 Part II J)."""
        _, capital_pct, _ = PARTNERS["parin"]
        assert capital_pct == Decimal("0.01"), (
            f"Expected parin capital_pct=0.01, got {capital_pct}"
        )

    def test_parin_profit_loss_pct(self):
        """Parin receives 0% of P&L allocations (CRIT-03)."""
        _, _, pl_pct = PARTNERS["parin"]
        assert pl_pct == Decimal("0.00"), (
            f"Expected parin profit_loss_pct=0.00, got {pl_pct}"
        )

    def test_capital_pcts_sum_to_one(self):
        """Capital percentages across all partners must sum to 1.00."""
        total = sum(cap for _, cap, _ in PARTNERS.values())
        assert total == Decimal("1.00"), (
            f"Capital percentages sum to {total}, expected 1.00"
        )

    def test_profit_loss_pcts_sum_to_one(self):
        """P&L percentages across all partners must sum to 1.00."""
        total = sum(pl for _, _, pl in PARTNERS.values())
        assert total == Decimal("1.00"), (
            f"P&L percentages sum to {total}, expected 1.00"
        )

    def test_both_partners_present(self):
        """Both yash and parin must be registered as canonical partner_ids."""
        assert "yash" in PARTNERS
        assert "parin" in PARTNERS

    def test_build_partners_returns_independent_copy(self):
        """_build_partners() must return a fresh dict each call (no mutation risk)."""
        p1 = _build_partners()
        p2 = _build_partners()
        assert p1 is not p2


# ---------------------------------------------------------------------------
# generate_k1 income routing (CRIT-03)
# ---------------------------------------------------------------------------

def _fake_form1065(ordinary_income: str = "19683.19") -> Form1065:
    obi = Decimal(ordinary_income)
    return Form1065(
        fiscal_year=2024,
        entity_name="SYNCED LLC",
        ein_masked="XX-XXXXXXX",
        total_income=obi + Decimal("8917.81"),
        total_deductions=Decimal("8917.81"),
        ordinary_business_income=obi,
        net_short_term_capital_gain=Decimal("3699.99"),
        net_long_term_capital_gain=Decimal("-116.00"),
        dividend_income=Decimal("37.31"),
        interest_income=Decimal("0.00"),
        partner_ids=["yash", "parin"],
    )


@pytest.fixture
def patched_form1065():
    """Patch generate_form_1065 in the api module so no DB is touched."""
    fake = _fake_form1065()
    with patch("ledger_agent.core.api.generate_form_1065", return_value=fake) as m:
        yield fake


class TestGenerateK1Routing:

    def test_yash_receives_full_ordinary_income(self, patched_form1065):
        """Yash P&L=100% → ordinary_income_loss equals Form 1065 ordinary income."""
        k1 = generate_k1(2024, "yash")
        assert k1.ordinary_income_loss == patched_form1065.ordinary_business_income

    def test_parin_receives_zero_ordinary_income(self, patched_form1065):
        """Parin P&L=0% → ordinary_income_loss is exactly $0.00 (CRIT-03)."""
        k1 = generate_k1(2024, "parin")
        assert k1.ordinary_income_loss == Decimal("0.00")

    def test_k1_allocations_sum_to_form_1065(self, patched_form1065):
        """Yash + Parin K-1 ordinary income must equal Form 1065 ordinary income."""
        k1_yash = generate_k1(2024, "yash")
        k1_parin = generate_k1(2024, "parin")
        total = k1_yash.ordinary_income_loss + k1_parin.ordinary_income_loss
        assert total == patched_form1065.ordinary_business_income

    def test_yash_k1_percentages(self, patched_form1065):
        """K-1 for Yash must carry the correct capital and P&L percentages."""
        k1 = generate_k1(2024, "yash")
        assert k1.capital_pct == Decimal("0.99")
        assert k1.profit_loss_pct == Decimal("1.00")

    def test_parin_k1_percentages(self, patched_form1065):
        """K-1 for Parin must carry the correct capital and P&L percentages."""
        k1 = generate_k1(2024, "parin")
        assert k1.capital_pct == Decimal("0.01")
        assert k1.profit_loss_pct == Decimal("0.00")

    def test_yash_k1_stcg_routing(self, patched_form1065):
        """Yash receives 100% of net STCG."""
        k1 = generate_k1(2024, "yash")
        assert k1.net_stcg == patched_form1065.net_short_term_capital_gain.quantize(
            Decimal("0.01")
        )

    def test_parin_k1_stcg_is_zero(self, patched_form1065):
        """Parin receives 0% of net STCG (P&L=0%)."""
        k1 = generate_k1(2024, "parin")
        assert k1.net_stcg == Decimal("0.00")

    def test_allocation_uses_pl_pct_not_capital_pct(self, patched_form1065):
        """
        Income must be allocated by profit_loss_pct, not capital_pct.

        Yash holds 99% capital but 100% P&L.  If allocation used capital_pct,
        Yash would receive 99% of income.  The correct allocation is 100%.
        """
        k1 = generate_k1(2024, "yash")
        expected_by_pl = (
            patched_form1065.ordinary_business_income * Decimal("1.00")
        ).quantize(Decimal("0.01"))
        wrong_by_capital = (
            patched_form1065.ordinary_business_income * Decimal("0.99")
        ).quantize(Decimal("0.01"))

        assert k1.ordinary_income_loss == expected_by_pl
        assert k1.ordinary_income_loss != wrong_by_capital, (
            "generate_k1 is using capital_pct instead of profit_loss_pct — CRIT-03 violated"
        )
