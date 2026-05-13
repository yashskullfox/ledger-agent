"""
parsers/chase_checking.py  –  Chase Business Complete Checking parser
──────────────────────────────────────────────────────────────────────
Handles Chase bank statement PDF format:

  DEPOSITS AND ADDITIONS
  DATE        DESCRIPTION                 AMOUNT
  01/15       Online Transfer from ...    1,500.00

  ATM & DEBIT CARD WITHDRAWALS
  DATE        DESCRIPTION                 AMOUNT
  01/03       RECURRING DEBIT CARD ...   29.99

  ELECTRONIC WITHDRAWALS
  DATE        DESCRIPTION                 AMOUNT
  01/07       Zelle To John Doe           500.00

Account summary supplies beginning / ending balance.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

from core.models import (
    AccountSnapshot, ParsedStatement,
    StatementType, Transaction, TransactionType,
)
from parsers.base import BaseStatementParser
from parsers.registry import ParserRegistry

@ParserRegistry.register
class ChaseCheckingParser(BaseStatementParser):
    PARSER_ID = "chase_checking"
    INSTITUTION = "Chase Bank"

    @classmethod
    def can_parse(cls, text: str) -> bool:
        upper = text.upper()
        return (
                "JPMORGAN CHASE" in upper or "CHASE" in upper
        ) and (
                "BUSINESS COMPLETE CHECKING" in upper
                or "TOTAL CHECKING" in upper
                or "CHASE BUSINESS" in upper
        )

    def parse(self, pdf_path: Path) -> ParsedStatement:
        raw_text = self.extract_text(pdf_path)
        period, year = self._extract_period(raw_text)
        account_no = self._extract_account_number(raw_text)
        entity_name = self._extract_entity_name(raw_text)
        prev_bal, new_bal = self._extract_balances(raw_text)

        credits = self._parse_deposits(raw_text, year)
        debits = self._parse_atm_debits(raw_text, year) + \
                 self._parse_electronic_withdrawals(raw_text, year)

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
        Chase statements show: "January 01 - January 31, 2025"
        or "Statement period: 01/01/25 - 01/31/25"
        """
        # Long format: Month DD - Month DD, YYYY
        m = re.search(
            r"(?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+\d{1,2}\s*[-–]\s*"
            r"(?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+\d{1,2},\s*(\d{4})",
            text, re.IGNORECASE,
        )
        if m:
            # Get the end month for the period
            year = int(m.group(1))
            # Find end month name
            months = {
                "january": "01", "february": "02", "march": "03", "april": "04",
                "may": "05", "june": "06", "july": "07", "august": "08",
                "september": "09", "october": "10", "november": "11", "december": "12",
            }
            end_part = m.group(0).split("-")[-1].strip()
            for month_name, month_num in months.items():
                if month_name in end_part.lower():
                    return f"{year}-{month_num}", year
        # Numeric format: MM/DD/YY or MM/DD/YYYY
        m2 = re.search(r"(\d{2})/(\d{2})/(\d{2,4})\s*[-–]\s*(\d{2})/(\d{2})/(\d{2,4})", text)
        if m2:
            year_raw = m2.group(6)
            year = int(year_raw) if len(year_raw) == 4 else 2000 + int(year_raw)
            month = m2.group(4)
            return f"{year}-{month}", year
        return "0000-00", 0

    def _extract_account_number(self, text: str) -> str:
        m = re.search(r"Account\s*[Nn]umber[:\s]*\.{0,3}(\d{4,})", text)
        if m:
            return m.group(1)
        m2 = re.search(r"ending\s+in\s+(\d{4})", text, re.IGNORECASE)
        if m2:
            return m2.group(1)
        m3 = re.search(r"(\d{9,})", text)
        if m3:
            return m3.group(1)
        return "0000"

    def _extract_entity_name(self, text: str) -> str:
        for line in text.splitlines():
            line = line.strip()
            if re.match(r"^[A-Z][A-Z &,]+(?:LLC|INC|CORP|CO|LTD)$", line):
                return line
        return "UNKNOWN ENTITY"

    def _extract_balances(self, text: str) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        prev = new = None
        m_prev = re.search(r"[Bb]eginning\s+[Bb]alance\s+\$?([\d,]+\.\d{2})", text)
        if m_prev:
            prev = self.parse_amount(m_prev.group(1))
        m_new = re.search(r"(?:Ending|Closing)\s+[Bb]alance\s+\$?([\d,]+\.\d{2})", text)
        if m_new:
            new = self.parse_amount(m_new.group(1))
        return prev, new

    # Transaction line: "01/15  SOME DESCRIPTION       1,234.56"
    _TX_RE = re.compile(
        r"^(\d{2}/\d{2})\s{2,}(.+?)\s{2,}([\d,]+\.\d{2})\s*$",
        re.MULTILINE,
    )

    def _parse_section(self, text: str, start_pat: str, end_pat: str,
                       year: int, is_debit: bool) -> List[Transaction]:
        section = self._extract_section(text, start_pat, end_pat)
        txns = []
        for m in self._TX_RE.finditer(section):
            date_str, desc, amt_str = m.group(1), m.group(2).strip(), m.group(3)
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

    def _parse_deposits(self, text: str, year: int) -> List[Transaction]:
        return self._parse_section(
            text,
            r"DEPOSITS\s+AND\s+ADDITIONS",
            r"Total\s+Deposits",
            year, is_debit=False,
        )

    def _parse_atm_debits(self, text: str, year: int) -> List[Transaction]:
        return self._parse_section(
            text,
            r"ATM\s*[&]\s*DEBIT\s*CARD\s*WITHDRAWALS?",
            r"Total\s+ATM|ELECTRONIC\s+WITHDRAWALS",
            year, is_debit=True,
        )

    def _parse_electronic_withdrawals(self, text: str, year: int) -> List[Transaction]:
        return self._parse_section(
            text,
            r"ELECTRONIC\s+WITHDRAWALS?",
            r"Total\s+Electronic|OTHER\s+WITHDRAWALS",
            year, is_debit=True,
        )

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
        if "TRANSFER" in up or "ZELLE" in up or "ONLINE TRANSFER" in up:
            return TransactionType.TRANSFER_OUT if is_debit else TransactionType.TRANSFER_IN
        if "IRS" in up or "TAX" in up:
            return TransactionType.TAX
        if "FEE" in up or "SERVICE CHARGE" in up:
            return TransactionType.FEE
        return TransactionType.DEBIT if is_debit else TransactionType.CREDIT
