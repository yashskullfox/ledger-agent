"""
parsers/fidelity_brokerage.py  –  Fidelity Brokerage / Investment Account parser
──────────────────────────────────────────────────────────────────────────────────
Handles Fidelity Investment Report PDFs (monthly format).
Extracts:
  • Account summary (net value, beginning/ending NAV, withdrawals, margin)
  • Holdings (positions: symbol, qty, price, market value, cost basis, U/R G/L)
  • Securities Bought & Sold  (trades with gain/loss annotations)
  • Withdrawals (EFT transfers to Truist)
  • Margin interest charges
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

from core.models import (
    AccountSnapshot, AccountType, ParsedStatement, Position,
    RealisedTrade, StatementType, Transaction, TransactionType,
)
from parsers.base import BaseStatementParser
from parsers.registry import ParserRegistry


@ParserRegistry.register
class FidelityBrokerageParser(BaseStatementParser):

    PARSER_ID   = "fidelity_brokerage"
    INSTITUTION = "Fidelity Investments"

    # ── Detection fingerprint ─────────────────────────────────────────────────

    @classmethod
    def can_parse(cls, text: str) -> bool:
        return (
            "FIDELITY" in text.upper()
            and "INVESTMENT REPORT" in text.upper()
            and ("BROKERAGE" in text.upper() or "Z23-" in text.upper()
                 or "Account Number" in text)
        )

    # ── Main parse ────────────────────────────────────────────────────────────

    def parse(self, pdf_path: Path) -> ParsedStatement:
        raw_text   = self.extract_text(pdf_path)
        period, year = self._extract_period(raw_text)
        account_no = self._extract_account_number(raw_text)
        entity_name = self._extract_entity_name(raw_text)

        snapshot   = self._parse_summary(raw_text, period)
        positions  = self._parse_holdings(raw_text, period, year)
        txns       = self._parse_withdrawals(raw_text, period, year)
        txns      += self._parse_margin_interest(raw_text, period, year)
        trades     = self._parse_trades(raw_text, period, year)

        # Attach realised trades as special transaction records
        for t in trades:
            sign = Decimal("1") if t.gain_loss >= 0 else Decimal("-1")
            txns.append(Transaction(
                account_id="",
                date=t.settlement_date or date(int(period[:4]), int(period[5:7]), 1),
                description=f"{'GAIN' if t.gain_loss>=0 else 'LOSS'} – {t.symbol}: {t.description}",
                raw_description=t.description,
                amount=t.gain_loss,
                transaction_type=TransactionType.SELL,
                statement_period=period,
                coa_code="4010" if t.gain_loss >= 0 else "5070",
                coa_name="Realised Trading Gains" if t.gain_loss >= 0 else "Realised Trading Losses",
                tags=[t.term, "realised", t.symbol],
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

    # ── Period / header extraction ────────────────────────────────────────────

    def _extract_period(self, text: str) -> Tuple[str, int]:
        """
        Looks for  "January 1, 2025 - January 31, 2025"
        Returns ("2025-01", 2025)
        """
        m = re.search(
            r"(\w+ \d{1,2}, (\d{4}))\s*[-–]\s*(\w+ \d{1,2}, \d{4})",
            text,
        )
        if m:
            from dateutil import parser as _dp
            try:
                d = _dp.parse(m.group(3))
                return self.period_from_date(d.date()), d.year
            except Exception:
                pass
        # fallback: January 31, 2025
        m2 = re.search(r"(\w+)\s+\d{1,2},\s+(\d{4})", text)
        if m2:
            from dateutil import parser as _dp
            try:
                d = _dp.parse(m2.group(0))
                return self.period_from_date(d.date()), d.year
            except Exception:
                pass
        return "0000-00", 0

    def _extract_account_number(self, text: str) -> str:
        """Look for  'Z23-945042'  or  'Account Number: Z23-945042'."""
        m = re.search(r"Z\d{2}-\d{6}", text)
        if m:
            return m.group(0).replace("-", "")
        m2 = re.search(r"Account\s+(?:Number|#)[:\s]+([A-Z0-9\-]{5,})", text)
        if m2:
            return re.sub(r"\D", "", m2.group(1))
        return "0000"

    def _extract_entity_name(self, text: str) -> str:
        for line in text.splitlines():
            line = line.strip()
            if re.match(r"^[A-Z][A-Z &]+LLC$", line):
                return line
        return "UNKNOWN ENTITY"

    # ── Account summary / snapshot ────────────────────────────────────────────

    _MONEY_RE = re.compile(r"\$?([\d,]+\.\d{2})")

    def _parse_summary(self, text: str, period: str) -> AccountSnapshot:
        def _find(pattern: str) -> Optional[Decimal]:
            m = re.search(pattern, text, re.IGNORECASE)
            return self.parse_amount(m.group(1)) if m else None

        ending_nav   = _find(r"Ending Net Account Value[^$\d]*\$?([\d,]+\.\d{2})")
        beginning_nav = _find(r"Beginning Net Account Value[^$\d]*\$?([\d,]+\.\d{2})")
        withdrawals  = _find(r"Withdrawals\s+[-–]?([\d,]+\.\d{2})")
        margin_bal   = _find(r"Margin balance\s+[-–]?\$?([\d,]+\.\d{2})")
        gross_market = _find(r"Market Value of Holdings\s+\$?([\d,]+\.\d{2})")
        realised     = _find(r"Net\s+(?:Short-term\s+)?Gain[/\\]Loss\s+\$?([\d,]+\.\d{2})")

        return AccountSnapshot(
            account_id="",          # filled in by importer
            statement_period=period,
            ending_balance=ending_nav or Decimal("0"),
            beginning_balance=beginning_nav,
            gross_asset_value=gross_market,
            margin_balance=-(margin_bal) if margin_bal and margin_bal > 0 else margin_bal,
            total_withdrawals=withdrawals,
            realised_gain_loss=realised,
        )

    # ── Holdings ──────────────────────────────────────────────────────────────
    #
    # Holdings table example line (from the Fidelity statement):
    # M CAREDX INC (CDNA)  $23,551.00  1,100.000  $23.3000  $25,630.00  $16,774.00  $8,856.00
    #
    _HOLDING_RE = re.compile(
        r"M?\s*([A-Z][A-Z0-9 &.,]+?)\s*\(([A-Z]+)\)\s+"   # name (symbol)
        r"(?:unavailable|[\d,]+\.\d{2})\s+"               # begin market value
        r"([\d,]+\.\d{3})\s+"                             # quantity
        r"([\d,]+\.\d{4})\s+"                             # price
        r"([\d,]+\.\d{2})\s+"                             # ending market value
        r"([\d,]+\.\d{2})\s+"                             # cost basis
        r"([-\d,]+\.\d{2})",                              # unrealised G/L
        re.MULTILINE,
    )

    def _parse_holdings(self, text: str, period: str, year: int) -> List[Position]:
        positions = []
        for m in self._HOLDING_RE.finditer(text):
            name, symbol = m.group(1).strip(), m.group(2)
            qty   = self.parse_amount(m.group(3))
            price = self.parse_amount(m.group(4))
            mv    = self.parse_amount(m.group(5))
            cb    = self.parse_amount(m.group(6))
            ugl   = self.parse_amount(m.group(7))
            if qty and price and mv:
                positions.append(Position(
                    account_id="",
                    symbol=symbol,
                    name=name,
                    quantity=abs(qty),
                    price_per_unit=abs(price),
                    market_value=abs(mv),
                    statement_period=period,
                    cost_basis=abs(cb) if cb else None,
                    unrealized_gain_loss=ugl,
                    is_margin=True,
                    as_of_date=date(year, int(period[5:7]), 28),
                ))
        return positions

    # ── Trades (Securities Bought & Sold) ────────────────────────────────────
    #
    # Example sold lines:
    # s 01/07 BIGBEAR AI HLDGS INC COM  08975B109  You Sold  Short-term gain: $82.30  -1,500.000  4.56000  6,757.50  -0.20  6,839.80
    #
    _SOLD_RE = re.compile(
        r"s\s+(\d{2}/\d{2})\s+(.+?)\s+\d{9}\s+You Sold\s+"
        r"Short-term\s+(gain|loss):\s+\$?([\d,]+\.\d{2})",
        re.IGNORECASE | re.DOTALL,
    )
    _SOLD_SIMPLE_RE = re.compile(
        r"Short-term\s+(gain|loss):\s+\$?([\d,]+\.\d{2})",
        re.IGNORECASE,
    )

    def _parse_trades(self, text: str, period: str, year: int) -> List[RealisedTrade]:
        trades: List[RealisedTrade] = []
        # Match every "You Sold" block that has a gain/loss annotation
        block_re = re.compile(
            r"s\s+(\d{2}/\d{2})\s+(.*?)\s+You\s+Sold\s+"
            r"((?:Short-term\s+(?:gain|loss):\s+\$[\d,]+\.\d{2}\s*)+)",
            re.IGNORECASE | re.DOTALL,
        )
        symbol_re = re.compile(r"\(([A-Z]{2,6})\)")

        for m in block_re.finditer(text):
            raw_date  = m.group(1)
            desc_raw  = m.group(2).strip()
            gl_block  = m.group(3)

            d = self.parse_date(raw_date, year)
            # Extract symbol from description parenthetical if present
            sym_m  = symbol_re.search(desc_raw)
            symbol = sym_m.group(1) if sym_m else desc_raw.split()[0][:6]

            net_gl = Decimal("0")
            for gl_m in self._SOLD_SIMPLE_RE.finditer(gl_block):
                gl_type = gl_m.group(1).lower()
                gl_val  = self.parse_amount(gl_m.group(2)) or Decimal("0")
                net_gl += gl_val if gl_type == "gain" else -gl_val

            if net_gl != 0:
                trades.append(RealisedTrade(
                    account_id="",
                    statement_period=period,
                    symbol=symbol,
                    description=desc_raw[:120],
                    gain_loss=net_gl,
                    term="short",
                    settlement_date=d,
                ))
        return trades

    # ── Withdrawals ───────────────────────────────────────────────────────────
    #
    # 01/21  Money Line Paid  EFT FUNDS PAID ED60133796 /WEB  TRUIST BANK ******0272  -$250.00
    #
    _WITHDRAWAL_RE = re.compile(
        r"(\d{2}/\d{2})\s+Money Line Paid\s+EFT FUNDS PAID\s+\S+\s+/WEB\s+"
        r"TRUIST BANK[^-\d]*-([\d,]+\.\d{2})",
        re.IGNORECASE,
    )

    def _parse_withdrawals(self, text: str, period: str, year: int) -> List[Transaction]:
        txns = []
        for m in self._WITHDRAWAL_RE.finditer(text):
            d   = self.parse_date(m.group(1), year)
            amt = self.parse_amount(m.group(2))
            if d and amt:
                txns.append(Transaction(
                    account_id="",
                    date=d,
                    description="Transfer Out – Truist Bank",
                    raw_description=m.group(0),
                    amount=-abs(amt),
                    transaction_type=TransactionType.TRANSFER_OUT,
                    statement_period=period,
                    is_transfer=True,
                    coa_code="3010",
                    coa_name="Members Capital Contributions",
                ))
        return txns

    # ── Margin Interest ───────────────────────────────────────────────────────
    #
    # 12/31-01/20  7,833  12.075%  27,141  -$191.17
    #
    _MARGIN_RE = re.compile(
        r"(\d{2}/\d{2}-\d{2}/\d{2})\s+"
        r"[\d,]+\s+[\d.]+%\s+[\d,]+\s+"
        r"-\$?([\d,]+\.\d{2})",
        re.IGNORECASE,
    )

    def _parse_margin_interest(self, text: str, period: str, year: int) -> List[Transaction]:
        txns = []
        for m in self._MARGIN_RE.finditer(text):
            amt = self.parse_amount(m.group(2))
            if amt:
                # Use last day of period as the date
                mo = int(period[5:7])
                import calendar
                last_day = calendar.monthrange(year, mo)[1]
                d = date(year, mo, last_day)
                txns.append(Transaction(
                    account_id="",
                    date=d,
                    description="Margin Interest Expense",
                    raw_description=m.group(0),
                    amount=-abs(amt),
                    transaction_type=TransactionType.MARGIN_INTEREST,
                    statement_period=period,
                    coa_code="5030",
                    coa_name="Margin Interest Expense",
                ))
        return txns
