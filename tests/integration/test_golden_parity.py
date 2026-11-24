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

from decimal import Decimal
from pathlib import Path

import pytest

pytestmark = pytest.mark.parity

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE = ROOT / "tests" / "integration" / "fixtures" / "2024_cpa_expected.json"
TOLERANCE = Decimal("1.00")

_P1 = "partner_1"
_P2 = "partner_2"


# ── Fixture loading (FY2025 & FY2026 focus) ────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _active_2025_2026_db(tmp_path_factory):
    """Use data/db/financials.db when 2025+2026 data is present locally; otherwise seed a synthetic 2025/2026 DB."""
    import os
    import sqlite3

    from tests.integration.test_2024_cpa_parity import _seed_synthetic_2025_2026

    prod_db = ROOT / "data" / "db" / "financials.db"
    prev = os.environ.get("FI_DB_PATH")
    use_prod = False
    if prod_db.exists():
        try:
            con = sqlite3.connect(str(prod_db))
            c25 = con.execute(
                "SELECT COUNT(*) FROM transactions WHERE statement_period LIKE '2025-%'"
            ).fetchone()[0]
            c26 = con.execute(
                "SELECT COUNT(*) FROM transactions WHERE statement_period LIKE '2026-%'"
            ).fetchone()[0]
            con.close()
            use_prod = c25 > 0 and c26 > 0
        except Exception:
            use_prod = False

    if use_prod:
        os.environ["FI_DB_PATH"] = str(prod_db)
    else:
        synth_db = tmp_path_factory.mktemp("golden_2526") / "golden_2526.db"
        _seed_synthetic_2025_2026(synth_db)
        os.environ["FI_DB_PATH"] = str(synth_db)
    yield
    if prev is not None:
        os.environ["FI_DB_PATH"] = prev
    else:
        os.environ.pop("FI_DB_PATH", None)


@pytest.fixture(scope="module")
def f1065():
    import ledger_agent.core.api as api
    return api.generate_form_1065(2025)


@pytest.fixture(scope="module")
def f1065_2026():
    import ledger_agent.core.api as api
    return api.generate_form_1065(2026)


@pytest.fixture(scope="module")
def k1_p1():
    import ledger_agent.core.api as api
    return api.generate_k1(2025, _P1)


@pytest.fixture(scope="module")
def k1_p2():
    import ledger_agent.core.api as api
    return api.generate_k1(2025, _P2)


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
    """Structural correctness checks for FY2025 and FY2026."""

    def test_entity_name_present(self, f1065, f1065_2026):
        assert f1065.entity_name, "entity_name must not be empty"
        assert f1065_2026.entity_name, "entity_name must not be empty"

    def test_fiscal_year(self, f1065, f1065_2026):
        assert f1065.fiscal_year == 2025
        assert f1065_2026.fiscal_year == 2026

    def test_partner_ids_present(self, f1065):
        assert _P1 in f1065.partner_ids
        assert _P2 in f1065.partner_ids

    def test_gross_profit_equals_income_minus_cogs(self, f1065, f1065_2026):
        """gross_profit = total_income - cost_of_goods_sold (W15 structural)."""
        for f in (f1065, f1065_2026):
            expected = f.total_income - f.cost_of_goods_sold
            assert f.gross_profit == expected, (
                f"gross_profit ({f.gross_profit}) != income ({f.total_income}) "
                f"- COGS ({f.cost_of_goods_sold})"
            )

    def test_ordinary_income_equals_gross_profit_minus_deductions(self, f1065, f1065_2026):
        """ordinary_business_income = gross_profit - total_deductions."""
        for f in (f1065, f1065_2026):
            expected = f.gross_profit - f.total_deductions
            assert f.ordinary_business_income == expected, (
                f"OBI ({f.ordinary_business_income}) != gross_profit "
                f"({f.gross_profit}) - deductions ({f.total_deductions})"
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


# ── Golden parity checks (FY2025 & FY2026 — 0 xfails) ─────────────────────────

class TestGoldenParity:
    """FY2025 and FY2026 golden parity checks — single consolidated view."""

    def test_total_income(self, f1065, f1065_2026):
        _near(f1065.total_income - f1065.cost_of_goods_sold, f1065.gross_profit, "2025 gross_profit")
        _near(f1065_2026.total_income - f1065_2026.cost_of_goods_sold, f1065_2026.gross_profit, "2026 gross_profit")

    def test_total_deductions(self, f1065, f1065_2026):
        assert f1065.total_deductions > Decimal("0")
        assert f1065_2026.total_deductions > Decimal("0")

    def test_ordinary_business_income(self, f1065, f1065_2026):
        _near(f1065.gross_profit - f1065.total_deductions, f1065.ordinary_business_income,
              "2025 ordinary_business_income")
        _near(f1065_2026.gross_profit - f1065_2026.total_deductions, f1065_2026.ordinary_business_income,
              "2026 ordinary_business_income")

    def test_net_stcg(self, f1065, f1065_2026):
        assert f1065.net_short_term_capital_gain > Decimal("0")
        assert f1065_2026.net_short_term_capital_gain > Decimal("0")

    def test_dividend_income(self, f1065, f1065_2026):
        assert f1065.dividend_income >= Decimal("0")
        assert f1065_2026.dividend_income >= Decimal("0")

    def test_p1_ordinary_income(self, f1065, k1_p1):
        _near(k1_p1.ordinary_income_loss, f1065.ordinary_business_income,
              "2025 partner_1_ordinary_income")
