"""
tests/unit/test_codeql_quality.py  –  X-07 CodeQL quality fixes (no behaviour change)

The "Tax Estimate" fallback lines used implicit string concatenation inside a
list literal.  The text is now one explicit string; output must be identical.
"""
from __future__ import annotations

from adapters.context_builder import _tax_prompt_lines


def test_tax_prompt_lines_unavailable_text_unchanged():
    assert _tax_prompt_lines(None) == [
        "",
        "## Tax Estimate",
        "  Not available for this entity/year (not enough statements).",
    ]
    assert _tax_prompt_lines({}) == _tax_prompt_lines(None)
    assert len(_tax_prompt_lines(None)) == 3


def test_tax_prompt_basis_line_unchanged():
    tax = {
        "fiscal_year": 2099,
        "basis": "full_year",
        "through_period": "2099-12",
        "months_covered": 12,
        "ytd_entity_taxable_income": 0.0,
        "annual_entity_taxable_income": 0.0,
        "total_annual_tax": 0.0,
        "effective_rate_pct": 0.0,
        "paid_ytd": 0.0,
        "remaining_balance": 0.0,
        "quarterly_payments": [],
    }
    lines = _tax_prompt_lines(tax)
    assert lines[2] == "  Basis: full_year through 2099-12 (12/12 months complete)"



def test_ptet_rate_note_text_unchanged():
    from decimal import Decimal

    from ledger_agent.core.accounting.tax_estimator import STATE_INCOME_RATE, TaxEstimator

    est = TaxEstimator("ENTITY_A", 2099).estimate_entity_ptet(Decimal("1000"))
    expected = (
        f"State PTET rate: {float(STATE_INCOME_RATE) * 100:.2f}% of entity taxable income "
        "(ordinary business income + net ST/LT capital gain + dividends + interest "
        "− investment interest, floored at 0)."
    )
    assert expected in est.notes
    assert len(est.notes) == 3
