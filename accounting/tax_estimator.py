"""
accounting/tax_estimator.py  –  Quarterly tax obligation estimator
──────────────────────────────────────────────────────────────────
Estimates federal and state quarterly estimated tax payments for a
pass-through entity (LLC taxed as sole prop or S-Corp).

IRS safe-harbor rule (Form 1040-ES):
  Pay the lesser of:
    (a) 90% of current-year tax liability, OR
    (b) 100% of prior-year tax liability  (110% if AGI > $150k)

Rates used (approximate – consult a tax professional for actual filings):
  Federal self-employment tax  : 15.3% on net self-employment income
  Federal income tax estimate  : 22% effective rate (adjustable)
  State tax estimate           : configurable via FI_STATE_TAX_RATE

Output: TaxEstimate dataclass with quarterly payment schedule.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List


def _env_decimal(key: str, default: str) -> Decimal:
    """Read a Decimal from an env var; silently fall back to *default* if malformed."""
    raw = os.environ.get(key, "").strip()
    if not raw:
        return Decimal(default)
    try:
        return Decimal(raw)
    except Exception:
        import warnings
        warnings.warn(
            f"[tax_estimator] {key}={raw!r} is not a valid decimal; "
            f"using default {default}",
            stacklevel=2,
        )
        return Decimal(default)


SE_TAX_RATE = _env_decimal("FI_SE_TAX_RATE", "0.153")  # 15.3%
FEDERAL_INCOME_RATE = _env_decimal("FI_FED_INCOME_RATE", "0.22")  # ~22% effective
STATE_INCOME_RATE = _env_decimal("FI_STATE_TAX_RATE", "0.05")  # 5% (state-specific)
QBI_DEDUCTION_RATE = _env_decimal("FI_QBI_DEDUCTION", "0.20")  # 20% QBI deduction

# IRS Q-dates (approximate – actual dates vary by year)
QUARTERLY_DUE_DATES = {
    "Q1": "April 15",
    "Q2": "June 15",
    "Q3": "September 15",
    "Q4": "January 15 (next year)",
}


@dataclass
class QuarterlyPayment:
    quarter: str  # "Q1", "Q2", etc.
    due_date: str
    amount: Decimal
    description: str


@dataclass
class TaxEstimate:
    entity_name: str
    period: str  # "2025" or "2025-01"
    net_income: Decimal
    se_tax: Decimal
    federal_income_tax: Decimal
    state_income_tax: Decimal
    total_annual_tax: Decimal
    quarterly_payments: List[QuarterlyPayment] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def effective_rate(self) -> Decimal:
        if self.net_income == 0:
            return Decimal("0")
        return (self.total_annual_tax / self.net_income * 100).quantize(Decimal("0.01"))


class TaxEstimator:
    """
    Estimate tax obligations from a BalanceSheet or raw net income figure.
    """

    def __init__(self, entity_name: str, year: int):
        self.entity_name = entity_name
        self.year = year

    def estimate_from_net_income(
            self,
            net_income: Decimal,
            annualize_factor: Decimal = Decimal("1"),
    ) -> TaxEstimate:
        """
        Args:
            net_income:        Net income for the period.
            annualize_factor:  Multiply to get annual equivalent
                               (e.g., Decimal("12") if net_income is for 1 month).
        """
        annual_ni = net_income * annualize_factor

        # SE tax applies only to positive net self-employment income (>$400 IRS rule).
        # For a loss or break-even period, return a zero-tax estimate rather than
        # computing nonsensical negative payments.
        if annual_ni <= Decimal("0"):
            zero = Decimal("0.00")
            payments = [
                QuarterlyPayment(
                    quarter=q,
                    due_date=QUARTERLY_DUE_DATES[q],
                    amount=zero,
                    description=f"No estimated payment due – net income is not positive ({QUARTERLY_DUE_DATES[q]})",
                )
                for q in ("Q1", "Q2", "Q3", "Q4")
            ]
            return TaxEstimate(
                entity_name=self.entity_name,
                period=str(self.year),
                net_income=annual_ni,
                se_tax=zero,
                federal_income_tax=zero,
                state_income_tax=zero,
                total_annual_tax=zero,
                quarterly_payments=payments,
                notes=[
                    "ℹ️  Net income is zero or negative — no estimated tax payments are due.",
                    "⚠️  These are rough estimates only — consult a CPA for actual tax filings.",
                ],
            )

        # Self-employment tax (only on net self-employment income)
        # SE tax deduction: 50% of SE tax is deductible
        se_tax = (annual_ni * SE_TAX_RATE).quantize(Decimal("0.01"))
        se_deduction = (se_tax * Decimal("0.5")).quantize(Decimal("0.01"))

        # QBI deduction (20% of qualified business income)
        qbi_deduction = (annual_ni * QBI_DEDUCTION_RATE).quantize(Decimal("0.01"))

        # Federal income tax base
        taxable_income = max(annual_ni - se_deduction - qbi_deduction, Decimal("0"))
        fed_tax = (taxable_income * FEDERAL_INCOME_RATE).quantize(Decimal("0.01"))

        # State income tax
        state_tax = (annual_ni * STATE_INCOME_RATE).quantize(Decimal("0.01"))

        total_tax = se_tax + fed_tax + state_tax
        quarterly_amount = (total_tax / 4).quantize(Decimal("0.01"))

        # Build quarterly schedule
        payments = [
            QuarterlyPayment(
                quarter=q,
                due_date=QUARTERLY_DUE_DATES[q],
                amount=quarterly_amount,
                description=(
                    f"Estimated federal + state + SE tax – "
                    f"${quarterly_amount:,.2f} due {QUARTERLY_DUE_DATES[q]}"
                ),
            )
            for q in ("Q1", "Q2", "Q3", "Q4")
        ]

        notes = [
            "⚠️  These are rough estimates only — consult a CPA for actual tax filings.",
            f"SE Tax rate: {float(SE_TAX_RATE) * 100:.1f}%  |  "
            f"Federal rate: {float(FEDERAL_INCOME_RATE) * 100:.1f}%  |  "
            f"State rate: {float(STATE_INCOME_RATE) * 100:.1f}%",
            f"QBI deduction applied: 20% of net income (${qbi_deduction:,.2f})",
            "IRS safe harbor: pay 100% of prior-year liability or 90% of current-year.",
        ]
        if annual_ni != net_income:
            notes.insert(0,
                         f"ℹ️  Net income ${net_income:,.2f} annualized ×{annualize_factor} "
                         f"= ${annual_ni:,.2f}"
                         )

        return TaxEstimate(
            entity_name=self.entity_name,
            period=str(self.year),
            net_income=annual_ni,
            se_tax=se_tax,
            federal_income_tax=fed_tax,
            state_income_tax=state_tax,
            total_annual_tax=total_tax,
            quarterly_payments=payments,
            notes=notes,
        )

    def estimate_from_balance_sheet(
            self,
            balance_sheet,
            months_covered: int = 1,
    ) -> TaxEstimate:
        """
        Build a tax estimate from a BalanceSheet object.
        months_covered: how many months the sheet represents (for annualisation).
        """
        annualize = Decimal(str(12 / months_covered)) if months_covered else Decimal("12")
        return self.estimate_from_net_income(
            balance_sheet.net_income,
            annualize_factor=annualize,
        )


def render_tax_estimate(est: TaxEstimate) -> None:
    """Print a formatted tax estimate to stdout.

    .. deprecated::
        This function is a presentation concern and has been moved to
        ``reports.renderer.render_tax_estimate``.  This shim is kept for
        backward compatibility with callers that import directly from
        ``accounting.tax_estimator``.  New code should import from
        ``reports.renderer`` instead.
    """
    from reports.renderer import render_tax_estimate as _render
    _render(est)
