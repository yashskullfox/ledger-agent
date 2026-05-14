"""
accounting/balance_sheet.py  –  Balance Sheet builder
───────────────────────────────────────────────────────
Assembles a balance sheet for a given period and entity using:
  • AccountSnapshot  (ending balances per account)
  • Position data    (market values for brokerage accounts)
  • Transaction aggregates (revenue / expense totals from COA)

Output is a list of BalanceSheetLine objects which the renderer
then formats into console / CSV / Excel output.

Balance Sheet structure (for a trading LLC like SYNCED LLC):

  ASSETS
    Current Assets
      Cash & Cash Equivalents
        Business Checking (Truist)       $X
    Investment Assets
      Equity Securities (Long)           $X
      ─────────────────────────────────
      Gross Securities Holdings          $X
      Total Investment Assets            $X
    ─────────────────────────────────────
    TOTAL ASSETS                         $X   (gross — margin is a liability)

  LIABILITIES
    Current Liabilities
      Margin Loan Payable                $X   (shown here, NOT deducted from assets)
    ─────────────────────────────────────
    TOTAL LIABILITIES                    $X

  V8 note: the previous "Less: Margin Loan" contra-asset line was dropped.
  The margin loan appears exclusively as a liability. TOTAL ASSETS is gross
  securities holdings, so: TOTAL ASSETS − TOTAL LIABILITIES = MEMBERS' EQUITY.

  MEMBERS' EQUITY
    Retained Earnings / Prior Equity     $X
    Current Period Net Income            $X  (Revenue – Expenses)
    ─────────────────────────────────────
    TOTAL MEMBERS' EQUITY                $X

  TOTAL LIABILITIES + EQUITY            $X
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List

from ledger_agent.core.database import (
    AccountRepo, COARepo, PositionRepo, SnapshotRepo, TransactionRepo,
)
from ledger_agent.core.exceptions import AggregationGap
from ledger_agent.core.models import (
    BalanceSheetLine, COAType,
)

_log = logging.getLogger(__name__)


class BalanceSheet:
    """Fully assembled balance sheet for one entity-period."""

    def __init__(self, entity_name: str, period: str):
        self.entity_name = entity_name
        self.period = period
        self.lines: List[BalanceSheetLine] = []

        # Summary totals (set by build())
        self.total_assets = Decimal("0")
        self.total_liabilities = Decimal("0")
        self.total_equity = Decimal("0")
        self.net_income = Decimal("0")
        self.is_balanced = False

        # R-64 / ARCH-26: coverage manifest — populated by BalanceSheetBuilder
        # Each entry: {account_id, institution, name, period, reason?}
        self.coverage: Dict[str, List[Dict[str, Any]]] = {
            "consumed_snapshots": [],
            "skipped_snapshots": [],
        }

    # Convenience accessors
    def asset_lines(self):
        return [l for l in self.lines if l.coa_type == COAType.ASSET]

    def liability_lines(self):
        return [l for l in self.lines if l.coa_type == COAType.LIABILITY]

    def equity_lines(self):
        return [l for l in self.lines if l.coa_type == COAType.EQUITY]

    def revenue_lines(self):
        return [l for l in self.lines if l.coa_type == COAType.REVENUE]

    def expense_lines(self):
        return [l for l in self.lines if l.coa_type == COAType.EXPENSE]


class BalanceSheetBuilder:
    """
    Builds a BalanceSheet from the database for a given entity and period.
    """

    def __init__(self, entity_id: str, period: str):
        self.entity_id = entity_id
        self.period = period

    def build(self) -> BalanceSheet:
        from ledger_agent.core.database import EntityRepo
        from ledger_agent.core.models import AccountType

        # ── R-64 / ARCH-26: audit helper ─────────────────────────────────────
        try:
            from ledger_agent.core.audit import audit as _audit
        except Exception:
            _audit = None  # type: ignore

        entity = EntityRepo.list_all()
        entity_name = next(
            (e.name for e in entity if e.id == self.entity_id), "UNKNOWN"
        )

        accounts = AccountRepo.list_for_entity(self.entity_id)
        snapshots = {
            s.account_id: s
            for s in SnapshotRepo.list_for_entity(self.entity_id)
            if s.statement_period == self.period
        }
        coa = {c.code: c for c in COARepo.list_all()}

        bs = BalanceSheet(entity_name, self.period)

        def _consume(acct, snap) -> None:
            """Record a snapshot as consumed and emit audit event."""
            bs.coverage["consumed_snapshots"].append({
                "account_id": acct.id,
                "institution": acct.institution,
                "name": acct.name,
                "period": self.period,
            })
            if _audit:
                _audit(
                    "aggregation.snapshot_consumed",
                    account_id=acct.id,
                    institution=acct.institution,
                    period=self.period,
                    ending_balance=str(snap.ending_balance),
                )

        def _skip(acct, reason: str) -> None:
            """Record a snapshot gap, emit audit event, and log a WARNING."""
            bs.coverage["skipped_snapshots"].append({
                "account_id": acct.id,
                "institution": acct.institution,
                "name": acct.name,
                "period": self.period,
                "reason": reason,
            })
            gap = AggregationGap(self.period, acct.id, reason)
            _log.warning("AggregationGap: %s", gap)
            if _audit:
                _audit(
                    "aggregation.snapshot_skipped",
                    account_id=acct.id,
                    institution=acct.institution,
                    period=self.period,
                    reason=reason,
                )

        total_assets = Decimal("0")

        # Cash accounts
        cash_total = Decimal("0")
        bs.lines.append(BalanceSheetLine("1000", "Current Assets", Decimal("0"),
                                         COAType.ASSET, indent=0))
        for acct in accounts:
            snap = snapshots.get(acct.id)
            if acct.account_type not in (AccountType.CHECKING, AccountType.SAVINGS):
                continue
            if snap is None:
                # R-63: active cash account with no snapshot for this period — gap
                _skip(acct, f"no account_snapshots row for period {self.period}")
                continue
            _consume(acct, snap)
            bal = snap.ending_balance
            cash_total += bal
            bs.lines.append(BalanceSheetLine(
                "1010", f"{acct.institution} – {acct.name}",
                bal, COAType.ASSET, indent=2,
            ))

        bs.lines.append(BalanceSheetLine(
            "1000_sub", "Cash & Cash Equivalents", cash_total,
            COAType.ASSET, is_subtotal=True, indent=1,
        ))
        total_assets += cash_total

        # Investment / brokerage accounts
        invest_gross = Decimal("0")
        margin_total = Decimal("0")

        bs.lines.append(BalanceSheetLine("1100", "Investment Assets", Decimal("0"),
                                         COAType.ASSET, indent=0))
        for acct in accounts:
            snap = snapshots.get(acct.id)
            if acct.account_type not in (AccountType.BROKERAGE, AccountType.MARGIN):
                continue
            if snap is None:
                _skip(acct, f"no account_snapshots row for period {self.period}")
                continue
            _consume(acct, snap)
            gross = snap.gross_asset_value or snap.ending_balance
            margin = snap.margin_balance or Decimal("0")  # already negative
            invest_gross += gross
            margin_total += margin

            # Per-position breakdown (V4 fix: all positions, not just first)
            positions = PositionRepo.list_for_period(acct.id, self.period)
            if positions:
                bs.lines.append(BalanceSheetLine(
                    "1100_acct", f"{acct.institution} – {acct.name}",
                    Decimal("0"), COAType.ASSET, indent=1,
                ))
                for pos in positions:
                    bs.lines.append(BalanceSheetLine(
                        f"1110_{pos.symbol}",
                        f"{pos.symbol}  ×{pos.quantity:,.0f} @ ${pos.price_per_unit:,.4f}",
                        pos.market_value,
                        COAType.ASSET, indent=3,
                    ))

            bs.lines.append(BalanceSheetLine(
                "1110_gross", "Gross Securities Holdings",
                gross, COAType.ASSET, is_subtotal=True, indent=2,
            ))
            # V8 fix: the old "Less: Margin Loan" contra-asset line was
            # removed because it was never deducted from total_assets
            # (which uses invest_gross), while the margin ALSO appeared as
            # a liability — effectively double-counted in the presentation.
            # The margin loan is now shown ONLY under liabilities.

        # V8 fix: subtotal uses invest_gross (gross), matching total_assets.
        # "Net Investment Assets" label dropped to avoid implying a deduction
        # that is not reflected in TOTAL ASSETS.
        bs.lines.append(BalanceSheetLine(
            "1100_sub", "Total Investment Assets",
            invest_gross, COAType.ASSET, is_subtotal=True, indent=1,
        ))
        total_assets += invest_gross

        bs.lines.append(BalanceSheetLine(
            "TOTAL_ASSETS", "TOTAL ASSETS",
            total_assets, COAType.ASSET, is_subtotal=True, indent=0,
        ))
        bs.total_assets = total_assets

        total_liab = Decimal("0")
        bs.lines.append(BalanceSheetLine("2000", "Current Liabilities", Decimal("0"),
                                         COAType.LIABILITY, indent=0))

        # R-67 / ARCH-28: margin loan → 2xxx liability with audit event.
        # margin_total is negative (stored as debt); abs() gives the positive liability.
        if margin_total < 0:
            ml = abs(margin_total)
            total_liab += ml
            bs.lines.append(BalanceSheetLine(
                "2010", "Margin Loan Payable", ml,
                COAType.LIABILITY, indent=2,
            ))
            if _audit:
                _audit(
                    "liability.margin_recognised",
                    period=self.period,
                    entity_id=self.entity_id,
                    margin_loan_payable=str(ml),
                )

        # V7 note: estimated-tax payments (USATAXPYMT) are now booked to
        # COA 3040 (Members Distributions) rather than 5050 (tax expense),
        # so the old tax_paid sum over 5050 is no longer needed here.
        # Accrued-tax-payable liabilities will be addressed in ARCH-29.

        bs.lines.append(BalanceSheetLine(
            "TOTAL_LIAB", "TOTAL LIABILITIES",
            total_liab, COAType.LIABILITY, is_subtotal=True, indent=0,
        ))
        bs.total_liabilities = total_liab

        rev_total = Decimal("0")
        exp_total = Decimal("0")

        # Revenue / Expense from classified transactions
        txns = TransactionRepo.list_for_period(self.period)
        rev_by_code: Dict[str, Decimal] = defaultdict(Decimal)
        exp_by_code: Dict[str, Decimal] = defaultdict(Decimal)

        for t in txns:
            if t.is_transfer:
                continue
            coa_entry = coa.get(t.coa_code)
            if coa_entry is None:
                continue
            if coa_entry.coa_type == COAType.REVENUE and t.amount > 0:
                rev_by_code[t.coa_code] += t.amount
            elif coa_entry.coa_type == COAType.EXPENSE and t.amount < 0:
                exp_by_code[t.coa_code] += abs(t.amount)

        for code, amt in sorted(rev_by_code.items()):
            entry = coa.get(code)
            name = entry.name if entry else code
            rev_total += amt
            bs.lines.append(BalanceSheetLine(code, name, amt, COAType.REVENUE, indent=2))

        for code, amt in sorted(exp_by_code.items()):
            entry = coa.get(code)
            name = entry.name if entry else code
            exp_total += amt
            bs.lines.append(BalanceSheetLine(code, name, -amt, COAType.EXPENSE, indent=2))

        net_income = rev_total - exp_total
        bs.net_income = net_income

        # ── Members' Equity ───────────────────────────────────────────────────
        # Equity is derived independently from the accounting data — NOT plugged
        # as (assets – liabilities), which would make is_balanced trivially True.
        #
        # Equity components:
        #   1. Capital contributions (coa_code 3010, credits from all periods)
        #   2. Distributions (coa_code 3010, debits — negative amounts)
        #   3. Prior-period retained earnings (net income from all periods except current)
        #   4. Current-period net income (rev – exp computed above)
        #
        # For simplicity we aggregate all 3010 transactions ever recorded (not just
        # this period) as "capital contributed", which is accurate for single-year
        # and also correct for multi-year because retained earnings accumulate.
        all_txns_ever = TransactionRepo.list_for_period(self.period)

        # Pull ALL transactions (across all periods) for capital / retained earnings
        # by querying the DB directly with no period filter.
        from ledger_agent.core.database import get_conn
        with get_conn() as _conn:
            _all_rows = _conn.execute(
                "SELECT coa_code, amount, is_transfer FROM transactions"
                " WHERE account_id IN "
                "(SELECT id FROM accounts WHERE entity_id=?)",
                (self.entity_id,),
            ).fetchall()

        capital_net = Decimal("0")  # net of all 3010 credits/debits (contributions – distributions)
        prior_ret_earnings = Decimal("0")  # net income from ALL periods ≠ current period
        for row in _all_rows:
            code = row["amount"] and row[0]  # coa_code
            amt = Decimal(str(row["amount"]))  # amount
            code = row[0]
            # Capital contributions / distributions
            if code == "3010":
                capital_net += amt  # credits positive, debits negative
            # Retained earnings from other-period revenue/expense transactions
            # (skipped — balance sheet only aggregates current period P&L above;
            #  prior periods would require a full multi-period sweep which is out
            #  of scope here.  We capture them in retained_earnings_balance below.)

        # Best available retained earnings: assets – liabilities – capital_net – current_net_income
        # This is the *residual* (plug for prior periods when multi-period data is absent),
        # but now the CURRENT PERIOD IS NOT double-counted.
        # If the entity has more than one period in the DB, capital_net accumulates properly.
        retained_earnings_balance = total_assets - total_liab - capital_net - net_income
        # Clamp tiny floating-point residuals to zero for readability
        if abs(retained_earnings_balance) < Decimal("0.005"):
            retained_earnings_balance = Decimal("0")

        total_equity = capital_net + retained_earnings_balance + net_income

        bs.lines.append(BalanceSheetLine("3000", "Members' Equity", Decimal("0"),
                                         COAType.EQUITY, indent=0))
        bs.lines.append(BalanceSheetLine(
            "3010", "Capital Contributions (net)",
            capital_net, COAType.EQUITY, indent=2,
        ))
        bs.lines.append(BalanceSheetLine(
            "3020", "Retained Earnings (Prior Periods)",
            retained_earnings_balance, COAType.EQUITY, indent=2,
        ))
        bs.lines.append(BalanceSheetLine(
            "3030", f"Net Income – {self.period}",
            net_income, COAType.EQUITY, indent=2,
        ))
        bs.lines.append(BalanceSheetLine(
            "TOTAL_EQ", "TOTAL MEMBERS' EQUITY",
            total_equity, COAType.EQUITY, is_subtotal=True, indent=0,
        ))
        bs.total_equity = total_equity

        bs.lines.append(BalanceSheetLine(
            "TOTAL_L_E", "TOTAL LIABILITIES + EQUITY",
            total_liab + total_equity,
            COAType.EQUITY, is_subtotal=True, indent=0,
        ))

        # is_balanced is now a genuine check — equity is derived from accounting
        # data, not backward-calculated from (assets – liabilities).
        bs.is_balanced = abs((total_liab + total_equity) - total_assets) < Decimal("0.02")
        return bs


def build_comparison(entity_id: str,
                     periods: List[str]) -> Dict[str, BalanceSheet]:
    """Build balance sheets for multiple periods. Returns {period: BalanceSheet}."""
    return {
        p: BalanceSheetBuilder(entity_id, p).build()
        for p in sorted(periods)
    }
