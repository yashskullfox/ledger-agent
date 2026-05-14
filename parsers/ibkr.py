"""
parsers/ibkr.py  –  Interactive Brokers Activity Statement parser

Handles IBKR monthly/quarterly Activity Statement PDFs.

Uses pdfplumber's table extraction (page.extract_tables()) for all structured
data. Text extraction is used only for can_parse() fingerprinting.

Table mapping:
  Account Information  → entity name, account number
  Net Asset Value      → snapshot balances (ARCH-24: gross_asset_value, margin_balance)
  Open Positions       → positions (ARCH-25)
  Deposits & Withdrawals → cash transactions
  Trades               → realised trades with P/L

Detection fingerprints:
  - "Interactive Brokers" in text
  - "Activity Statement" or "Account Statement" in text
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import List, Optional, Tuple

from core.models import (
    AccountSnapshot, ParsedStatement, Position, RealisedTrade,
    StatementType, Transaction, TransactionType,
)
from ledger_agent.core.exceptions import ParserGap
from parsers.base import BaseStatementParser
from parsers.registry import ParserRegistry


# ─── Module-level helpers ────────────────────────────────────────────────────

def _extract_all_tables(pdf_path: Path) -> List[List]:
    """Collect all tables from all pages in page order."""
    import pdfplumber
    all_tables: List[List] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            tbls = page.extract_tables()
            if tbls:
                all_tables.extend(tbls)
    return all_tables


def _find_table(all_tables: List[List], header_name: str) -> Optional[List]:
    """Return first table whose first cell matches *header_name*.

    Special case for 'Cash Report': some statements (e.g. Nov 2025) have two
    Cash Report tables; prefer the one that contains an 'Ending Cash' row.
    """
    matches = [
        t for t in all_tables
        if t and t[0] and t[0][0] == header_name
    ]
    if not matches:
        return None
    if header_name == "Cash Report" and len(matches) > 1:
        for t in matches:
            if any(row and row[0] == "Ending Cash" for row in t):
                return t
    return matches[0]


def _parse_cell(cell) -> Optional[Decimal]:
    """Convert a table cell value to Decimal; None for empty / dash / non-numeric."""
    if cell is None:
        return None
    raw = str(cell).replace(",", "").strip()
    if not raw or raw in ("-", "—"):
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _parse_ibkr_date(raw: str) -> Optional[date]:
    """Parse IBKR date strings.

    Handles:
      '2025-12-22'               → ISO date
      '2025-12-22,\\n10:14:34'   → trade rows — strip time part after comma
      'December 31, 2025'        → NAV header row (comma is part of date; do NOT split)
    """
    if not raw:
        return None
    # Normalise newlines
    clean = raw.strip().replace("\n", " ")
    # ISO date (possibly followed by time): split on comma only for ISO-prefix strings
    if re.match(r"^\d{4}-\d{2}-\d{2}", clean):
        clean = clean.split(",")[0].strip()
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", clean)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        return None
    # Month-name format ("December 31, 2025") — keep the full string so dateutil
    # sees the year.  Splitting by comma would produce "December 31" and dateutil
    # would default to the current year, which is wrong for historical statements.
    try:
        from dateutil import parser as _dp
        return _dp.parse(clean).date()
    except Exception:
        return None


# ─── Parser ─────────────────────────────────────────────────────────────────

@ParserRegistry.register
class IBKRParser(BaseStatementParser):
    """Parser for Interactive Brokers Activity Statement PDFs (table-based)."""

    PARSER_ID = "ibkr"
    INSTITUTION = "Interactive Brokers"

    @classmethod
    def can_parse(cls, text: str) -> bool:
        upper = text.upper()
        return "INTERACTIVE BROKERS" in upper and (
                "ACTIVITY STATEMENT" in upper or "ACCOUNT STATEMENT" in upper
        )

    def parse(self, pdf_path: Path) -> ParsedStatement:
        # Raw text is needed only for can_parse detection and regex fallbacks
        raw_text = self.extract_text(pdf_path)
        all_tables = _extract_all_tables(pdf_path)

        period, year = self._extract_period(all_tables, raw_text)
        account_no = self._extract_account_number(all_tables, raw_text)
        entity_name = self._extract_entity_name(all_tables, raw_text)

        snapshot = self._parse_cash_report(all_tables, period)
        positions = self._parse_positions(all_tables, period, year)
        trades = self._parse_trades(all_tables, period, year)
        txns = self._parse_cash_transactions(all_tables, period, year)

        # Append a Transaction for every closed trade (realised P/L → COA)
        for trade in trades:
            txns.append(Transaction(
                account_id="",
                date=trade.settlement_date or date(int(period[:4]), int(period[5:7]), 1),
                description=(
                    f"{'GAIN' if trade.gain_loss >= 0 else 'LOSS'}"
                    f" – {trade.symbol}: {trade.description}"
                ),
                raw_description=trade.description,
                amount=trade.gain_loss,
                transaction_type=TransactionType.SELL,
                statement_period=period,
                coa_code="4010" if trade.gain_loss >= 0 else "5070",
                coa_name=(
                    "Realised Trading Gains"
                    if trade.gain_loss >= 0
                    else "Realised Trading Losses"
                ),
                tags=[trade.term, "realised", trade.symbol],
            ))

        return ParsedStatement(
            parser_id=self.PARSER_ID,
            statement_type=StatementType.BROKERAGE,
            institution=self.INSTITUTION,
            account_number_masked=self.mask_account(account_no),
            statement_period=period,
            entity_name=entity_name,
            transactions=txns,
            positions=positions,
            snapshot=snapshot,
            raw_text=raw_text,
            source_file=str(pdf_path),
        )

    # ── Period ───────────────────────────────────────────────────────────────

    def _extract_period(
            self, all_tables: List[List], raw_text: str
    ) -> Tuple[str, int]:
        """Extract period end from NAV table date header row (row[1][2]).

        NAV table row[1] format:
          ['October 31, 2025', None, 'November 30, 2025', None, None, '']
          Col[2] is the end-of-period date.
        """
        nav = _find_table(all_tables, "Net Asset Value")
        if nav and len(nav) > 1:
            date_row = nav[1]
            end_date_str = date_row[2] if len(date_row) > 2 else None
            if end_date_str:
                d = _parse_ibkr_date(str(end_date_str))
                if d:
                    return self.period_from_date(d), d.year

        # Fallback: regex on raw text
        for pattern in (
            r"(?:Period|From)[:\s]+(\d{4}-\d{2}-\d{2})\s*(?:to|-)\s*(\d{4}-\d{2}-\d{2})",
            r"(\w+ \d{1,2},\s*\d{4})\s*(?:-|to)\s*(\w+ \d{1,2},\s*\d{4})",
        ):
            m = re.search(pattern, raw_text, re.IGNORECASE)
            if m:
                d = _parse_ibkr_date(m.group(2))
                if d:
                    return self.period_from_date(d), d.year

        return "0000-00", 0

    # ── Account / entity info ────────────────────────────────────────────────

    def _extract_account_number(
            self, all_tables: List[List], raw_text: str
    ) -> str:
        """Extract account number from Account Information table."""
        acct_info = _find_table(all_tables, "Account Information")
        if acct_info:
            for row in acct_info:
                if row and row[0] == "Account" and len(row) > 1 and row[1]:
                    return str(row[1]).strip()
        # Fallback
        m = re.search(r"\b(U\d{7,})\b", raw_text)
        if m:
            return m.group(1)
        m2 = re.search(r"Account[:\s]+([A-Z]\d{6,})", raw_text, re.IGNORECASE)
        if m2:
            return m2.group(1)
        return "0000"

    def _extract_entity_name(
            self, all_tables: List[List], raw_text: str
    ) -> str:
        """Extract entity name from Account Information table."""
        acct_info = _find_table(all_tables, "Account Information")
        if acct_info:
            for row in acct_info:
                if row and row[0] == "Name" and len(row) > 1 and row[1]:
                    return str(row[1]).strip()
        # Fallback: text-based
        m = re.search(r"Name:\s*(.+)", raw_text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        for line in raw_text.splitlines():
            stripped = line.strip()
            if re.match(r"^[A-Z][A-Z &,]+(?:LLC|INC|CORP|CO|LTD)$", stripped):
                return stripped
        return "UNKNOWN ENTITY"

    # ── Snapshot ─────────────────────────────────────────────────────────────

    def _parse_cash_report(
            self, all_tables: List[List], period: str
    ) -> AccountSnapshot:
        """Build AccountSnapshot from NAV Total row (ARCH-24).

        NAV Total row columns: ['Total', prior_total, long, short, ending, change]
          prior_total → beginning_balance
          long        → gross_asset_value
          short       → margin_balance (only if < 0; zero → None)
          ending      → ending_balance (total portfolio NAV)
        """
        nav = _find_table(all_tables, "Net Asset Value")
        total_row = None
        if nav:
            total_row = next((r for r in nav if r and r[0] == "Total"), None)

        if total_row is None:
            raise ParserGap(
                "nav_total", self.PARSER_ID,
                hint="Net Asset Value 'Total' row missing — statement may be truncated",
            )

        beginning = _parse_cell(total_row[1]) if len(total_row) > 1 else None
        gross_asset = _parse_cell(total_row[2]) if len(total_row) > 2 else None
        short_raw = _parse_cell(total_row[3]) if len(total_row) > 3 else None
        ending = _parse_cell(total_row[4]) if len(total_row) > 4 else None

        if ending is None:
            raise ParserGap(
                "ending_balance", self.PARSER_ID,
                hint="NAV ending balance missing — statement may be truncated",
            )

        # Only negative Short values represent a margin loan (Aug 2025: Short=0.00 → None)
        margin = short_raw if (short_raw is not None and short_raw < 0) else None

        deposits, withdrawals = self._sum_deposits_withdrawals(all_tables)

        # Emit audit events in a daemon thread so a slow/hanging audit dir
        # (network fs, sandbox restriction) never blocks the parser.
        def _emit_audit() -> None:
            try:
                from ledger_agent.core.audit import audit
                audit(
                    "parser.field_present" if gross_asset is not None else "parser.gap",
                    parser_id=self.PARSER_ID, field="gross_asset_value",
                    period=period, value=str(gross_asset),
                )
                audit(
                    "parser.field_present" if margin is not None else "parser.gap",
                    parser_id=self.PARSER_ID, field="margin_balance",
                    period=period, value=str(margin),
                )
            except Exception:
                pass

        import threading
        _t = threading.Thread(target=_emit_audit, daemon=True)
        _t.start()
        _t.join(timeout=2.0)  # give audit 2 s; abandon if hung

        return AccountSnapshot(
            account_id="",
            statement_period=period,
            ending_balance=ending,
            beginning_balance=beginning,
            gross_asset_value=gross_asset,
            margin_balance=margin,
            total_credits=deposits,
            total_withdrawals=withdrawals,
        )

    def _sum_deposits_withdrawals(
            self, all_tables: List[List]
    ) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        """Sum deposits (positive rows) and withdrawals (negative rows) from D&W table."""
        dw = _find_table(all_tables, "Deposits & Withdrawals")
        if not dw:
            return None, None

        _SKIP = {"Deposits & Withdrawals", "Date", "USD", "Total"}
        deposits = Decimal("0")
        withdrawals = Decimal("0")

        for row in dw:
            if not row or not row[0] or str(row[0]).strip() in _SKIP:
                continue
            amt_raw = row[2] if len(row) > 2 else None
            if not amt_raw:
                continue
            amt = _parse_cell(amt_raw)
            if amt is None:
                continue
            if amt > 0:
                deposits += amt
            else:
                withdrawals += abs(amt)

        return (
            deposits if deposits else None,
            withdrawals if withdrawals else None,
        )

    # ── Positions ────────────────────────────────────────────────────────────

    def _parse_positions(
            self, all_tables: List[List], period: str, year: int
    ) -> List[Position]:
        """Parse Open Positions table into Position objects.

        Column layout (confirmed across all 2025 statements):
          0:Symbol  1:Quantity  2:Mult  3:Cost Price  4:Cost Basis
          5:Close Price  6:Value  7:Unrealized P/L  8:Code
        """
        pos_table = _find_table(all_tables, "Open Positions")
        if not pos_table:
            return []

        positions: List[Position] = []
        for row in pos_table:
            if not row or not row[0]:
                continue
            symbol = str(row[0]).strip()
            # Data rows have a 1-6 uppercase ticker in col 0
            if not re.match(r"^[A-Z]{1,6}$", symbol):
                continue
            # Quantity (col 1) must be numeric — filters out 'Stocks', 'Options', etc.
            qty = _parse_cell(row[1]) if len(row) > 1 else None
            if qty is None:
                continue

            close = _parse_cell(row[5]) if len(row) > 5 else None
            value = _parse_cell(row[6]) if len(row) > 6 else None
            ugl = _parse_cell(row[7]) if len(row) > 7 else None

            if not value or value == 0:
                continue

            positions.append(Position(
                account_id="",
                symbol=symbol,
                name=symbol,
                quantity=abs(qty),
                price_per_unit=close or Decimal("0"),
                market_value=value,
                statement_period=period,
                unrealized_gain_loss=ugl,
                is_margin=(qty < 0),
                as_of_date=date(year, int(period[5:7]), 28),
            ))

        return positions

    # ── Trades ───────────────────────────────────────────────────────────────

    def _parse_trades(
            self, all_tables: List[List], period: str, year: int
    ) -> List[RealisedTrade]:
        """Parse Trades table(s) into RealisedTrade objects.

        Column layout (confirmed Dec 2025):
          0:Symbol  1:Date/Time  2:''  3:Quantity  4:T.Price  5:C.Price
          6:Proceeds  7:Comm/Fee  8:Basis  9:Realized P/L  10:''  11:MTM P/L  12:Code

        Skips rows where Realized P/L is zero (open-position MTM rows).
        """
        trades: List[RealisedTrade] = []
        # Trades may span multiple pages → collect all Trades tables
        trade_tables = [t for t in all_tables if t and t[0] and t[0][0] == "Trades"]

        for tbl in trade_tables:
            for row in tbl:
                if not row or not row[0]:
                    continue
                symbol = str(row[0]).strip()
                if not re.match(r"^[A-Z]{1,6}$", symbol):
                    continue
                # Must have a Date/Time value
                date_raw = row[1] if len(row) > 1 else None
                if not date_raw:
                    continue
                trade_date = _parse_ibkr_date(str(date_raw))

                pl = _parse_cell(row[9]) if len(row) > 9 else None
                if pl is None or pl == 0:
                    continue

                trades.append(RealisedTrade(
                    account_id="",
                    statement_period=period,
                    symbol=symbol,
                    description=symbol,
                    gain_loss=pl,
                    term="short",
                    settlement_date=trade_date,
                ))

        return trades

    # ── Cash transactions ────────────────────────────────────────────────────

    def _parse_cash_transactions(
            self, all_tables: List[List], period: str, year: int
    ) -> List[Transaction]:
        """Parse Deposits & Withdrawals table into Transaction objects."""
        dw = _find_table(all_tables, "Deposits & Withdrawals")
        if not dw:
            return []

        txns: List[Transaction] = []
        for row in dw:
            if not row or not row[0]:
                continue
            date_raw = str(row[0]).strip()
            # Data rows start with an ISO date
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_raw):
                continue
            txn_date = _parse_ibkr_date(date_raw)
            if txn_date is None:
                continue

            desc = str(row[1]).strip() if len(row) > 1 and row[1] else "Transfer"
            amt = _parse_cell(row[2]) if len(row) > 2 else None
            if amt is None or amt == 0:
                continue

            txn_type = TransactionType.TRANSFER_IN if amt > 0 else TransactionType.TRANSFER_OUT

            txns.append(Transaction(
                account_id="",
                date=txn_date,
                description=desc,
                raw_description=desc,
                amount=amt,
                transaction_type=txn_type,
                statement_period=period,
                is_transfer=True,
            ))

        return txns
