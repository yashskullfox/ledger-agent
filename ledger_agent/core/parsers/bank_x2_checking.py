"""
parsers/bofa_checking.py  –  Bank of America Business Checking parser
───────────────────────────────────────────────────────────────────────
Handles BofA statement PDF format:

  Deposits and other credits
  Date   Description                         Amount
  01/06  Online Banking transfer from ...   2,000.00

  Withdrawals and other debits
  Date   Description                         Amount
  01/07  RECURRING PAYMENT authorized ...      29.99

Account summary block supplies beginning / ending balance.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

from ledger_agent.core.models import (
    AccountSnapshot, ParsedStatement,
    StatementType, Transaction, TransactionType,
)
from ledger_agent.core.parsers.base import BaseStatementParser
from ledger_agent.core.parsers.registry import ParserRegistry


@ParserRegistry.register
class BofACheckingParser(BaseStatementParser):
    PARSER_ID = "bofa_checking"
    INSTITUTION = "Bank of America"

    @classmethod
    def can_parse(cls, text: str) -> bool:
        upper = text.upper()
        return (
                "BANK OF AMERICA" in upper
        ) and (
                "BUSINESS ADVANTAGE" in upper
                or "BUSINESS CHECKING" in upper
                or "BUSINESS FUNDAMENTALS" in upper
                or "MERRILL" not in upper  # exclude Merrill investment statements
        )

    def parse(self, pdf_path: Path) -> ParsedStatement:
        raw_text = self.extract_text(pdf_path)
        period, year = self._extract_period(raw_text)
        account_no = self._extract_account_number(raw_text)
        entity_name = self._extract_entity_name(raw_text)
        prev_bal, new_bal = self._extract_balances(raw_text)

        credits = self._parse_deposits(raw_text, year)
        debits = self._parse_withdrawals(raw_text, year)

        all_txns = credits + debits
        total_debits = sum(abs(t.amount) for t in debits)
        total_credits = sum(t.amount for t in credits)

        snapshot = AccountSnapshot(
            account_id="",
            statement_period=period,
            ending_balance=new_bal if new_bal is not None else Decimal("0"),
            beginning_balance=prev_bal,
            total_debits=total_debits,
            total_credits=total_credits,
        )

        return ParsedStatement(
            parser_id=self.PARSER_ID,
            statement_type=StatementType.BANK_CHECKING,
            institution=self.INSTITUTION,
            account_number_masked=self.mask_account(account_no),
            statement_period=period,
            entity_name=entity_name,
            transactions=all_txns,
            snapshot=snapshot,
            raw_text=raw_text,
            source_file=str(pdf_path),
        )

    def _extract_period(self, text: str) -> Tuple[str, int]:
        """
        BofA: "Your account at a glance"  section, or
              "Statement Period: MM/DD/YYYY through MM/DD/YYYY"
        """
        m = re.search(
            r"(?:through|to)\s+(\d{2}/\d{2}/(\d{4}))",
            text, re.IGNORECASE,
        )
        if m:
            d = self.parse_date(m.group(1))
            if d:
                return self.period_from_date(d), d.year
        # MM/DD/YYYY - MM/DD/YYYY
        m2 = re.search(r"\d{2}/\d{2}/\d{4}\s*[-–]\s*(\d{2}/\d{2}/(\d{4}))", text)
        if m2:
            d = self.parse_date(m2.group(1))
            if d:
                return self.period_from_date(d), d.year
        return "0000-00", 0

    def _extract_account_number(self, text: str) -> str:
        m = re.search(r"Account\s*[Nn]umber[:\s]*(\d{4,})", text)
        if m:
            return m.group(1)
        m2 = re.search(r"[Aa]ccount\s+ending\s+in\s+(\d{4})", text)
        if m2:
            return m2.group(1)
        m3 = re.search(r"(\d{10,12})", text)
        if m3:
            return m3.group(1)
        return "0000"

    def _extract_entity_name(self, text: str) -> str:
        for line in text.splitlines():
            line = line.strip()
            if re.match(r"^[A-Z][A-Z &,\.]+(?:LLC|INC|CORP|CO|LTD|COMPANY)$", line):
                return line
        return "UNKNOWN ENTITY"

    def _extract_balances(self, text: str) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        prev = new = None
        m_prev = re.search(
            r"(?:Beginning|Opening)\s+[Bb]alance\s+[\$]?\s*([\d,]+\.\d{2})", text
        )
        if m_prev:
            prev = self.parse_amount(m_prev.group(1))
        m_new = re.search(
            r"(?:Ending|Closing)\s+[Bb]alance\s+[\$]?\s*([\d,]+\.\d{2})", text
        )
        if m_new:
            new = self.parse_amount(m_new.group(1))
        return prev, new

    # BofA transaction line format:
    # "01/06/25  DESCRIPTION                          2,000.00"
    # or just "01/06" with rest of line
    _TX_RE = re.compile(
        r"^(\d{2}/\d{2}(?:/\d{2,4})?)\s{2,}(.+?)\s{2,}([\d,]+\.\d{2})\s*$",
        re.MULTILINE,
    )

    def _parse_deposits(self, text: str, year: int) -> List[Transaction]:
        section = self._extract_section(
            text,
            r"Deposits\s+and\s+other\s+credits",
            r"Total\s+deposits|Withdrawals",
        )
        return self._parse_lines(section, year, is_debit=False)

    def _parse_withdrawals(self, text: str, year: int) -> List[Transaction]:
        section = self._extract_section(
            text,
            r"Withdrawals\s+and\s+other\s+debits",
            r"Total\s+withdrawals|Service\s+fees|Checks",
        )
        return self._parse_lines(section, year, is_debit=True)

    def _parse_lines(self, section: str, year: int, is_debit: bool) -> List[Transaction]:
        txns = []
        for m in self._TX_RE.finditer(section):
            date_str, desc, amt_str = m.group(1), m.group(2).strip(), m.group(3)
            # BofA date may include /YY – normalize
            if "/" in date_str:
                parts = date_str.split("/")
                if len(parts) == 3:
                    date_str = f"{parts[0]}/{parts[1]}"  # drop year part, let parse_date handle
            d = self.parse_date(date_str, year)
            amt = self.parse_amount(amt_str)
            if d and amt is not None:
                txn_type = self._classify_type(desc, is_debit)
                txns.append(Transaction(
                    account_id="",
                    date=d,
                    description=self._clean_desc(desc),
                    raw_description=desc,
                    amount=-abs(amt) if is_debit else abs(amt),
                    transaction_type=txn_type,
                    statement_period=self.period_from_date(d),
                    is_transfer=(txn_type in (
                        TransactionType.TRANSFER_IN, TransactionType.TRANSFER_OUT
                    )),
                ))
        return txns

    def _extract_section(self, text: str, start_pattern: str, end_pattern: str) -> str:
        end_m = re.search(end_pattern, text, re.IGNORECASE)
        end = end_m.start() if end_m else len(text)
        starts = [m for m in re.finditer(start_pattern, text, re.IGNORECASE) if m.start() < end]
        if not starts:
            return ""
        start = starts[-1].end()
        return text[start:end] if start < end else ""

    @staticmethod
    def _clean_desc(raw: str) -> str:
        d = re.sub(r"\s+", " ", raw).strip()
        d = re.sub(r"\s+\d{4}\s*$", "", d)
        return d

    @staticmethod
    def _classify_type(desc: str, is_debit: bool) -> TransactionType:
        up = desc.upper()
        if "TRANSFER" in up or "ZELLE" in up or "WIRE" in up:
            return TransactionType.TRANSFER_OUT if is_debit else TransactionType.TRANSFER_IN
        if "IRS" in up or "TAX" in up:
            return TransactionType.TAX
        if "FEE" in up or "SERVICE CHARGE" in up or "MAINTENANCE" in up:
            return TransactionType.FEE
        return TransactionType.DEBIT if is_debit else TransactionType.CREDIT
