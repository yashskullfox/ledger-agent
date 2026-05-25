"""
tests/integration/test_golden_parity.py  –  CPA-parity golden integration test (ARCH-32)
=========================================================================================

A single-module "golden" parity gate that verifies all core Form 1065, Schedule K-1,
and structural invariants against the committed fixture in one shot.

This complements ``test_2024_cpa_parity.py`` (which has per-class test methods) with
a compact, machine-readable summary useful for CI dashboards and release checklists.

Running
-------
    pytest tests/integration/test_golden_parity.py -v -m parity

The test module skips gracefully when the fixture is absent.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

pytestmark = pytest.mark.parity

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE = ROOT / "tests" / "integration" / "fixtures" / "2024_cpa_expected.json"
TOLERANCE = Decimal("1.00")

_P1 = "partner_1"
_P2 = "partner_2"


# ── Fixture loading ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ref():
    """Load CPA reference fixture; skip if absent."""
    if not FIXTURE.exists():
        pytest.skip("CPA reference fixture not found — run scripts/regen_parity_corpus.py")
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {k: Decimal(str(v)) for k, v in raw.items() if not k.startswith("_") and v}


@pytest.fixture(scope="module")
def f1065(ref):
    import ledger_agent.core.api as api
    try:
        return api.generate_form_1065(2024)
    except ValueError as e:
        pytest.skip(f"No 2024 data: {e}")


@pytest.fixture(scope="module")
def k1_p1(ref):
    import ledger_agent.core.api as api
    try:
        return api.generate_k1(2024, _P1)
    except ValueError as e:
        pytest.skip(f"No 2024 data: {e}")


@pytest.fixture(scope="module")
def k1_p2(ref):
    import ledger_agent.core.api as api
    try:
        return api.generate_k1(2024, _P2)
    except ValueError as e:
        pytest.skip(f"No 2024 data: {e}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _near(computed: Decimal, ref_val: Decimal, label: str) -> None:
    diff = abs(computed - ref_val)
    assert diff <= TOLERANCE, (
        f"GOLDEN PARITY FAIL — {label}\n"
        f"  Computed:   {computed:>12,.2f}\n"
        f"  Reference:  {ref_val:>12,.2f}\n"
        f"  Divergence: {diff:>12,.2f}  (tolerance {TOLERANCE:,.2f})\n"
        f"  Fixture: {FIXTURE}"
    )


# ── Structural invariants (always run — no xfail) ─────────────────────────────

class TestForm1065Invariants:
    """Structural correctness checks that must pass regardless of data completeness."""

    def test_entity_name_present(self, f1065):
        assert f1065.entity_name, "entity_name must not be empty"

    def test_fiscal_year(self, f1065):
        assert f1065.fiscal_year == 2024

    def test_partner_ids_present(self, f1065):
        assert _P1 in f1065.partner_ids
        assert _P2 in f1065.partner_ids

    def test_gross_profit_equals_income_minus_cogs(self, f1065):
        """gross_profit = total_income - cost_of_goods_sold (W15 structural)."""
        expected = f1065.total_income - f1065.cost_of_goods_sold
        assert f1065.gross_profit == expected, (
            f"gross_profit ({f1065.gross_profit}) != income ({f1065.total_income}) "
            f"- COGS ({f1065.cost_of_goods_sold})"
        )

    def test_ordinary_income_equals_gross_profit_minus_deductions(self, f1065):
        """ordinary_business_income = gross_profit - total_deductions."""
        expected = f1065.gross_profit - f1065.total_deductions
        assert f1065.ordinary_business_income == expected, (
            f"OBI ({f1065.ordinary_business_income}) != gross_profit "
            f"({f1065.gross_profit}) - deductions ({f1065.total_deductions})"
        )


class TestK1Invariants:
    """Schedule K-1 allocation invariants."""

    def test_p1_profit_loss_pct(self, k1_p1):
        assert k1_p1.profit_loss_pct == Decimal("1.00")

    def test_p2_profit_loss_pct(self, k1_p2):
        assert k1_p2.profit_loss_pct == Decimal("0.00")

    def test_p1_plus_p2_capital_pct_equals_one(self, k1_p1, k1_p2):
        total = k1_p1.capital_pct + k1_p2.capital_pct
        assert total == Decimal("1.00"), f"Capital pcts must sum to 1.00, got {total}"

    def test_p2_ordinary_income_zero(self, k1_p2):
        """partner_2 has 0% P&L — ordinary income must be zero."""
        assert k1_p2.ordinary_income_loss == Decimal("0.00")


# ── CPA parity checks (xfail pending data completeness) ───────────────────────

class TestGoldenParity:
    """CPA-reference parity checks — single consolidated view."""

    def test_total_income(self, ref, f1065):
        _near(f1065.total_income, ref["total_income"], "total_income")

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "W15/W17 — COGS structural fix applied but payroll data missing from 2024 DB. "
            "See docs/w9-deductions-diagnostic.md"
        ),
    )
    def test_total_deductions(self, ref, f1065):
        _near(f1065.total_deductions, ref["total_deductions"], "total_deductions")

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "W15/W17 — Cascades from total_deductions gap. "
            "See docs/w9-deductions-diagnostic.md"
        ),
    )
    def test_ordinary_business_income(self, ref, f1065):
        _near(f1065.ordinary_business_income, ref["ordinary_business_income"],
              "ordinary_business_income")

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "W16 — Wash-sale adjustment CSV absent in public CI. "
            "Place private/wash_sale_adjustments.csv to pass locally. "
            "See docs/wash-sale.md"
        ),
    )
    def test_net_stcg(self, ref, f1065):
        _near(f1065.net_short_term_capital_gain, ref["net_stcg"], "net_stcg")

    def test_dividend_income(self, ref, f1065):
        if "dividend_income" in ref:
            _near(f1065.dividend_income, ref["dividend_income"], "dividend_income")

    @pytest.mark.xfail(
        strict=False,
        reason="W15/W17 — Cascades from OBI gap.",
    )
    def test_p1_ordinary_income(self, ref, k1_p1):
        _near(k1_p1.ordinary_income_loss, ref["partner_1_ordinary_income"],
              "partner_1_ordinary_income")
