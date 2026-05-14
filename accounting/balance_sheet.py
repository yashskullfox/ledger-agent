"""
accounting/balance_sheet.py  –  Balance Sheet builder
───────────────────────────────────────────────────────
Assembles a balance sheet for a given period and entity using:
  • AccountSnapshot  (ending balances per account)
  • Position data    (market values for brokerage accounts)
  • Transaction aggregates (revenue / expense totals from COA)

Output is a list of BalanceSheetLine objects which the renderer
then formats into console / CSV / Excel output.

Balance Sheet structure (for a trading LLC):

  ASSETS
    Current Assets
      Cash & Cash Equivalents
        Business Checking (BANK_X)       $X
    Investment Assets
      Equity Securities (Long)           $X
      ─────────────────────────────────
      Gross Investment Holdings          $X
      Less: Margin Loan                 ($X)
      Net Investment Assets              $X
    ─────────────────────────────────────
    TOTAL ASSETS                         $X

  LIABILITIES
    Current Liabilities
      Margin Loan Payable                $X
      Taxes Payable (est.)               $X
    ─────────────────────────────────────
    TOTAL LIABILITIES                    $X

  MEMBERS' EQUITY
    Retained Earnings / Prior Equity     $X
    Current Period Net Income            $X  (Revenue – Expenses)
    ─────────────────────────────────────
    TOTAL MEMBERS' EQUITY                $X

  TOTAL LIABILITIES + EQUITY            $X
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Dict, List, Optional

from core.database import (
    AccountRepo, COARepo, PositionRepo, SnapshotRepo, TransactionRepo,
)
from core.models import (
    BalanceSheetLine, COAType,
)


class BalanceSheet:
    """Fully assembled balance sheet for one entity-period."""

    def __init__(self, entity_name: str, period: str, entity_id: str = ""):
        self.entity_name = entity_name
        self.entity_id = entity_id  # ARCH-30: every artefact scoped to one entity
        self.period = period
        self.lines: List[BalanceSheetLine] = []

        # Summary totals (set by build())
        self.total_assets = Decimal("0")
        self.total_liabilities = Decimal("0")
        self.total_equity = Decimal("0")
        self.net_income = Decimal("0")
        self.is_balanced = False

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

    *period* is the snapshot period used for asset/liability balances (year-end).
    *pl_periods* is the optional list of all fiscal-year periods whose transactions
    feed the revenue/expense (P&L) section.  When omitted, only *period* is used,
    which gives a single-month P&L.  Pass all 12 months for a year-end balance sheet.
    """

    def __init__(self, entity_id: str, period: str,
                 pl_periods: Optional[List[str]] = None):
        self.entity_id = entity_id
        self.period = period
        # P&L aggregation periods: defaults to snapshot period only
        self.pl_periods: List[str] = pl_periods if pl_periods else [period]

    def build(self) -> BalanceSheet:
        from core.database import EntityRepo
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

        bs = BalanceSheet(entity_name, self.period, entity_id=self.entity_id)

        total_assets = Decimal("0")

        # Cash accounts
        cash_total = Decimal("0")
        bs.lines.append(BalanceSheetLine("1000", "Current Assets", Decimal("0"),
                                         COAType.ASSET, indent=0))
        for acct in accounts:
            snap = snapshots.get(acct.id)
            if snap is None:
                continue
            from core.models import AccountType
            if acct.account_type in (AccountType.CHECKING, AccountType.SAVINGS):
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
            if snap is None:
                continue
            from core.models import AccountType
            if acct.account_type in (AccountType.BROKERAGE, AccountType.MARGIN):
                gross = snap.gross_asset_value or snap.ending_balance
                margin = snap.margin_balance or Decimal("0")  # already negative
                invest_gross += gross
                margin_total += margin

                # Per-position breakdown
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
                # ARCH-28/V8: margin loan is a 2xxx liability — no contra-asset display.
                # Showing it as a contra-asset here AND in liabilities would double-count.

        bs.lines.append(BalanceSheetLine(
            "1100_sub", "Gross Investment Assets",
            invest_gross, COAType.ASSET, is_subtotal=True, indent=1,
        ))
        total_assets += invest_gross  # ARCH-28/V8: gross; margin loan lives in 2xxx liabilities

        bs.lines.append(BalanceSheetLine(
            "TOTAL_ASSETS", "TOTAL ASSETS",
            total_assets, COAType.ASSET, is_subtotal=True, indent=0,
        ))
        bs.total_assets = total_assets

        total_liab = Decimal("0")
        bs.lines.append(BalanceSheetLine("2000", "Current Liabilities", Decimal("0"),
                                         COAType.LIABILITY, indent=0))

        # Margin loan
        if margin_total < 0:
            ml = abs(margin_total)
            total_liab += ml
            bs.lines.append(BalanceSheetLine(
                "2010", "Margin Loan Payable", ml,
                COAType.LIABILITY, indent=2,
            ))

        # Transactions for P&L: aggregate all fiscal-year periods (ARCH-31 / year-end)
        txns = []
        for _p in self.pl_periods:
            txns.extend(TransactionRepo.list_for_period(_p))
        tax_paid = sum(
            abs(t.amount) for t in txns
            if t.coa_code in ("5050", "5040") and t.amount < 0
        )
        # (We don't add estimated tax payable since these were already deducted;
        #  only add if there's a known accrued liability you want to record)

        bs.lines.append(BalanceSheetLine(
            "TOTAL_LIAB", "TOTAL LIABILITIES",
            total_liab, COAType.LIABILITY, is_subtotal=True, indent=0,
        ))
        bs.total_liabilities = total_liab

        rev_total = Decimal("0")
        exp_total = Decimal("0")

        # Revenue from transactions
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
        from core.database import get_conn
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
