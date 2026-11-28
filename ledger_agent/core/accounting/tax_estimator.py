"""
accounting/tax_estimator.py  –  Quarterly tax obligation estimator
──────────────────────────────────────────────────────────────────
One estimator lives here:

**Entity-level state PTET** (``TaxEstimator.estimate_entity_ptet``) — the figure used by
   ``api.pte_estimate`` and therefore by every form (CLI / MCP / bridge / webapp).
   Owner decision D1 (card Q-04): the estimate is the state pass-through entity tax the
   partnership itself pays, and nothing else::

       entity taxable income = ordinary business income
                             + net short-term capital gain + net long-term capital gain
                             + dividends + interest − investment interest expense
                             (floored at 0)
       PTET                  = FI_STATE_TAX_RATE × entity taxable income

   Partners' individual estimated taxes (SE tax, federal income tax, QBI) are NOT part
   of this figure.

The legacy individual 1040-ES-style estimate (SE tax + federal + state on one net-income
figure, ``estimate_from_net_income`` / ``estimate_from_balance_sheet``) was removed by card
Q-02 once every surface routed its tax figures through ``api.pte_estimate``.

Rates (approximate – consult a tax professional for actual filings):
  State PTET rate              : FI_STATE_TAX_RATE (the only rate setting; the individual
                                 SE / federal / QBI settings were retired by card Q-06)

Output: TaxEstimate dataclass with quarterly payment schedule.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ledger_agent.core.config import NOT_TAX_ADVICE_DISCLAIMER


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


STATE_INCOME_RATE = _env_decimal("FI_STATE_TAX_RATE", "0.05")  # entity-level state PTET rate

# IRS Q-dates (approximate – actual dates vary by year)
QUARTERLY_DUE_DATES = {
    "Q1": "April 15",
    "Q2": "June 15",
    "Q3": "September 15",
    "Q4": "January 15 (next year)",
}


def get_quarterly_due_date(quarter: str, fiscal_year: int) -> date:
    """Return the statutory due date for an estimated quarterly tax payment."""
    if quarter == "Q1":
        return date(fiscal_year, 4, 15)
    elif quarter == "Q2":
        return date(fiscal_year, 6, 15)
    elif quarter == "Q3":
        return date(fiscal_year, 9, 15)
    elif quarter == "Q4":
        return date(fiscal_year + 1, 1, 15)
    raise ValueError(f"Unknown quarter: {quarter!r}")


@dataclass
class QuarterlyPayment:
    quarter: str  # "Q1", "Q2", etc.
    due_date: str
    amount: Decimal
    description: str
    due_date_obj: date | None = None


@dataclass
class TaxEstimate:
    entity_name: str
    period: str  # "2025" or "2025-01"
    net_income: Decimal
    se_tax: Decimal
    federal_income_tax: Decimal
    state_income_tax: Decimal
    total_annual_tax: Decimal
    quarterly_payments: list[QuarterlyPayment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def effective_rate(self) -> Decimal:
        if self.net_income == 0:
            return Decimal("0")
        return (self.total_annual_tax / self.net_income * 100).quantize(Decimal("0.01"))


def entity_taxable_income(
        ordinary_business_income: Decimal,
        net_short_term_capital_gain: Decimal = Decimal("0"),
        net_long_term_capital_gain: Decimal = Decimal("0"),
        dividend_income: Decimal = Decimal("0"),
        interest_income: Decimal = Decimal("0"),
        investment_interest_expense: Decimal = Decimal("0"),
) -> Decimal:
    """Entity-level PTET base: ordinary income + Schedule K portfolio items, floored at 0.

    Capital gains, dividends and interest always count — including when ordinary
    business income is positive (they used to be ignored in that case).
    ``investment_interest_expense`` is the positive Schedule K line 13b amount.
    """
    total = (
        Decimal(ordinary_business_income)
        + Decimal(net_short_term_capital_gain)
        + Decimal(net_long_term_capital_gain)
        + Decimal(dividend_income)
        + Decimal(interest_income)
        - Decimal(investment_interest_expense)
    )
    return max(total, Decimal("0"))


class TaxEstimator:
    """Entity-level state PTET estimator (see ``estimate_entity_ptet``)."""

    def __init__(self, entity_name: str, year: int):
        self.entity_name = entity_name
        self.year = year

    def estimate_entity_ptet(
            self,
            taxable_income: Decimal,
            annualize_factor: Decimal = Decimal("1"),
    ) -> TaxEstimate:
        """Entity-level state PTET (owner decision D1) — the method ``api.pte_estimate`` uses.

        Args:
            taxable_income:    Entity taxable income for the period, normally from
                               ``entity_taxable_income(...)`` (negative is floored at 0).
            annualize_factor:  Multiply to get the annual equivalent (quarter-to-date
                               bases pass 12 / months covered).

        Returns a ``TaxEstimate`` whose ``state_income_tax`` == ``total_annual_tax`` ==
        annual PTET; ``se_tax`` and ``federal_income_tax`` are always zero because
        partners' individual taxes are out of scope.  ``net_income`` is the annualized
        entity taxable income.
        """
        annual_base = max(Decimal(taxable_income), Decimal("0")) * annualize_factor
        zero = Decimal("0.00")
        scope_note = (
            "Entity-level state PTET only; partners' individual estimated taxes are not "
            "included (no SE tax, federal income tax or QBI)."
        )
        if annual_base <= Decimal("0"):
            payments = [
                QuarterlyPayment(
                    quarter=q,
                    due_date=QUARTERLY_DUE_DATES[q],
                    amount=zero,
                    description=(
                        "No PTET payment due – entity taxable income is not positive "
                        f"({QUARTERLY_DUE_DATES[q]})"
                    ),
                    due_date_obj=get_quarterly_due_date(q, self.year),
                )
                for q in ("Q1", "Q2", "Q3", "Q4")
            ]
            return TaxEstimate(
                entity_name=self.entity_name,
                period=str(self.year),
                net_income=annual_base,
                se_tax=zero,
                federal_income_tax=zero,
                state_income_tax=zero,
                total_annual_tax=zero,
                quarterly_payments=payments,
                notes=[
                    scope_note,
                    "ℹ️  Entity taxable income is zero or negative — no PTET payments are due.",
                    f"⚠️  {NOT_TAX_ADVICE_DISCLAIMER}",
                ],
            )

        ptet = (annual_base * STATE_INCOME_RATE).quantize(Decimal("0.01"))
        quarterly_amount = (ptet / 4).quantize(Decimal("0.01"))
        payments = [
            QuarterlyPayment(
                quarter=q,
                due_date=QUARTERLY_DUE_DATES[q],
                amount=quarterly_amount,
                description=(
                    f"Estimated state PTET – ${quarterly_amount:,.2f} due {QUARTERLY_DUE_DATES[q]}"
                ),
                due_date_obj=get_quarterly_due_date(q, self.year),
            )
            for q in ("Q1", "Q2", "Q3", "Q4")
        ]
        notes = [
            scope_note,
            f"⚠️  {NOT_TAX_ADVICE_DISCLAIMER}",
            (
                f"State PTET rate: {float(STATE_INCOME_RATE) * 100:.2f}% of entity taxable income "
                "(ordinary business income + net ST/LT capital gain + dividends + interest "
                "− investment interest, floored at 0)."
            ),
        ]
        if annualize_factor != Decimal("1"):
            notes.insert(
                0,
                f"ℹ️  Entity taxable income ${Decimal(taxable_income):,.2f} annualized "
                f"×{annualize_factor.quantize(Decimal('0.01'))} = ${annual_base:,.2f}",
            )
        return TaxEstimate(
            entity_name=self.entity_name,
            period=str(self.year),
            net_income=annual_base,
            se_tax=zero,
            federal_income_tax=zero,
            state_income_tax=ptet,
            total_annual_tax=ptet,
            quarterly_payments=payments,
            notes=notes,
        )
