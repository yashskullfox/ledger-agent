"""
accounting/period_coverage.py  –  Which months of a fiscal year are fully covered?
==================================================================================

Tax and year-end reporting must know how far the books are complete:

* **full year** – every month Jan–Dec is covered for every account that is active
  in the fiscal year → annual figures.
* **quarter-to-date** – months are complete from January through the end of some
  quarter → figures through that quarter only (later, partially imported months
  are excluded so a half-imported month never inflates or deflates the result).

A month counts as *covered* for an account when the account has a statement
snapshot for that period OR at least one transaction tagged with that
statement_period.  An account is *required* for a month when the month lies inside
the account's active span for the year:

* start = January if the account has any data in an earlier year, else its first
  covered month in this year (account opened mid-year);
* months before the first month in which ANY account has data (entity formed
  mid-year) are complete by definition — there was nothing to report;
* end   = December if the account has data in a later year, else the latest month
  covered by ANY account this year (an account that simply stopped being imported
  while others continued is treated as a gap, not as closed — fail closed).

Pure module: reads SQLite via ``get_conn`` only.
"""
from __future__ import annotations

from dataclasses import dataclass, field

QUARTER_END_MONTH = {1: 3, 2: 6, 3: 9, 4: 12}


@dataclass
class PeriodCoverage:
    fiscal_year: int
    entity_id: str
    # account_id → sorted list of covered months (1..12)
    covered: dict[str, list[int]] = field(default_factory=dict)
    # account_id → months inside its required span with no statement/transactions
    missing: dict[str, list[int]] = field(default_factory=dict)
    complete_months: list[int] = field(default_factory=list)  # months complete for all accounts
    complete_through_month: int = 0  # largest M such that months 1..M are all complete

    @property
    def is_full_year(self) -> bool:
        return self.complete_through_month == 12

    @property
    def last_complete_quarter(self) -> int:
        return self.complete_through_month // 3

    @property
    def basis(self) -> str:
        if self.is_full_year:
            return "full_year"
        if self.last_complete_quarter >= 1:
            return "quarter_to_date"
        if self.complete_through_month >= 1:
            return "partial_quarter"
        return "insufficient_data"

    @property
    def through_month(self) -> int:
        """Last month that year-to-date figures should include for this basis."""
        if self.basis == "partial_quarter":
            return self.complete_through_month
        return QUARTER_END_MONTH.get(self.last_complete_quarter, 0)

    @property
    def through_period(self) -> str | None:
        m = self.through_month
        return f"{self.fiscal_year}-{m:02d}" if m else None

    def to_dict(self) -> dict:
        return {
            "fiscal_year": self.fiscal_year,
            "basis": self.basis,
            "complete_through_month": self.complete_through_month,
            "last_complete_quarter": self.last_complete_quarter,
            "through_period": self.through_period,
            "complete_months": list(self.complete_months),
            # account ids are opaque internal UUIDs, never account numbers
            "missing": {k: list(v) for k, v in self.missing.items() if v},
        }


def _month_of(period: str, fiscal_year: int) -> int | None:
    if not period or not period.startswith(f"{fiscal_year}-"):
        return None
    try:
        m = int(period[5:7])
    except ValueError:
        return None
    return m if 1 <= m <= 12 else None


def compute_period_coverage(entity_id: str, fiscal_year: int) -> PeriodCoverage:
    from ledger_agent.core.database import get_conn

    covered: dict[str, set[int]] = {}
    earlier: set[str] = set()
    later: set[str] = set()
    year_prefix = f"{fiscal_year}-"

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT s.account_id, s.statement_period FROM account_snapshots s "
            "JOIN accounts a ON a.id = s.account_id WHERE a.entity_id = ? "
            "UNION "
            "SELECT t.account_id, t.statement_period FROM transactions t "
            "JOIN accounts a ON a.id = t.account_id WHERE a.entity_id = ?",
            (entity_id, entity_id),
        ).fetchall()

    for account_id, period in rows:
        if not period:
            continue
        if period < year_prefix:
            earlier.add(account_id)
            continue
        if period[:4] > str(fiscal_year):
            later.add(account_id)
            continue
        m = _month_of(period, fiscal_year)
        if m is not None:
            covered.setdefault(account_id, set()).add(m)

    cov = PeriodCoverage(fiscal_year=fiscal_year, entity_id=entity_id)
    if not covered:
        return cov

    latest_any = max(max(ms) for ms in covered.values())
    required_by_month: dict[int, set[str]] = {m: set() for m in range(1, 13)}
    for acct, months in covered.items():
        start = 1 if acct in earlier else min(months)
        end = 12 if acct in later else latest_any
        missing = [m for m in range(start, end + 1) if m not in months]
        cov.covered[acct] = sorted(months)
        cov.missing[acct] = missing
        for m in range(start, end + 1):
            required_by_month[m].add(acct)
        # an account that continues into a later year must be covered through December
        if acct in later:
            for m in range(latest_any + 1, 13):
                required_by_month[m].add(acct)

    # Months before ANY account has data (entity formed / first account opened mid-year)
    # had no activity to report, so they are complete by definition.
    inception = 1 if earlier else min(min(ms) for ms in covered.values())
    complete = []
    for m in range(1, 13):
        req = required_by_month[m]
        if (not req and m < inception) or (req and all(m in covered.get(a, set()) for a in req)):
            complete.append(m)
    cov.complete_months = complete

    through = 0
    for m in range(1, 13):
        if m in complete:
            through = m
        else:
            break
    cov.complete_through_month = through
    return cov
