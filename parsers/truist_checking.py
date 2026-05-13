"""
parsers/truist_checking.py  –  Truist Simple Business Checking statement parser
────────────────────────────────────────────────────────────────────────────────
Parses the Truist bank statement format:

  Other withdrawals, debits and service charges
  DATE  DESCRIPTION                    AMOUNT($)
  01/09 DEBIT CARD RECURRING PYMT ...  29.00

  Deposits, credits and interest
  DATE  DESCRIPTION                    AMOUNT($)
  01/21 MONEYLINE FID BKG ...          250.00

Account summary block supplies beginning / ending balances.
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
class TruistCheckingParser(BaseStatementParser):
    PARSER_ID = "truist_checking"
    INSTITUTION = "Truist Bank"

    @classmethod
    def can_parse(cls, text: str) -> bool:
        return (
                "TRUIST" in text.upper()
                and ("SIMPLE BUSINESS CHECKING" in text.upper()
                     or "TRUIST SIMPLE BUSINESS" in text.upper())
        )

    def parse(self, pdf_path: Path) -> ParsedStatement:
        raw_text = self.extract_text(pdf_path)
        period, year = self._extract_period(raw_text)
        account_no = self._extract_account_number(raw_text)
        entity_name = self._extract_entity_name(raw_text)
        prev_bal, new_bal = self._extract_balances(raw_text)

        debits = self._parse_debits(raw_text, year)
        credits = self._parse_credits(raw_text, year)

        all_txns = debits + credits
        total_debits = sum(abs(t.amount) for t in debits)
        total_credits = sum(t.amount for t in credits)

        snapshot = AccountSnapshot(
            account_id="",  # filled in by importer after account creation
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
        Looks for:  "For 01/31/2025"  or  "account statement\nFor 01/31/2025"
        Returns ("2025-01", 2025)
        """
        m = re.search(r"For\s+(\d{2}/\d{2}/(\d{4}))", text)
        if m:
            end_date = self.parse_date(m.group(1))
            if end_date:
                return self.period_from_date(end_date), end_date.year
        # Fallback: look for MM/DD/YYYY anywhere
        m2 = re.search(r"(\d{2}/\d{2}/(\d{4}))", text)
        if m2:
            d = self.parse_date(m2.group(1))
            if d:
                return self.period_from_date(d), d.year
        return ("0000-00", 0)

    def _extract_account_number(self, text: str) -> str:
        """Look for 'CHECKING1470018610272' or 'CHECKING 1470018610272'."""
        # pdfplumber sometimes merges spaces → CHECKING1470018610272
        m = re.search(r"CHECKING\s*(\d{10,})", text, re.IGNORECASE)
        if m:
            return m.group(1)
        # Fallback: any standalone 10+ digit number near TRUIST
        m2 = re.search(r"VA\s+(\d{10,})", text)
        if m2:
            return m2.group(1)
        m3 = re.search(r"(?:Account|ACCOUNT)[:\s#]*(\d{8,})", text)
        if m3:
            return m3.group(1)
        return "0000"

    def _extract_entity_name(self, text: str) -> str:
        """Entity name is on the mailing-address block; look for ALL-CAPS 2-3 word line."""
        for line in text.splitlines():
            line = line.strip()
            if re.match(r"^[A-Z][A-Z &]+LLC$", line):
                return line
            if re.match(r"^[A-Z][A-Z &]+INC$", line):
                return line
            if re.match(r"^[A-Z][A-Z &]+CORP$", line):
                return line
        return "UNKNOWN ENTITY"

    def _extract_balances(self, text: str) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        prev = new = None
        m_prev = re.search(
            r"[Pp]revious\s+balance\s+as\s+of[^$]*\$([0-9,]+\.\d{2})", text
        )
        if m_prev:
            prev = self.parse_amount(m_prev.group(1))
        m_new = re.search(
            r"[Nn]ew\s+balance\s+as\s+of[^$]*=?\s*\$([0-9,]+\.\d{2})", text
        )
        if m_new:
            new = self.parse_amount(m_new.group(1))
        return prev, new

    def _parse_debits(self, text: str, year: int) -> List[Transaction]:
        """
        Extract lines between:
          "Other withdrawals, debits and service charges"  (spaces may be collapsed)
        and:
          "Total other withdrawals ..."
        pdfplumber may collapse section headers to e.g.:
          "Otherwithdrawals,debitsandservicecharges"
        """
        section = self._extract_section(
            text,
            start_pattern=r"Other\s*withdrawals,?\s*debits\s*and\s*service\s*charges",
            end_pattern=r"Total\s*other\s*withdrawals",
        )
        txns = []
        for raw_date, desc, amt_str in self._parse_tx_lines(section):
            d = self.parse_date(raw_date, year)
            amt = self.parse_amount(amt_str)
            if d and amt is not None:
                txns.append(Transaction(
                    account_id="",
                    date=d,
                    description=self._clean_desc(desc),
                    raw_description=desc,
                    amount=-abs(amt),  # debits are negative
                    transaction_type=self._classify_debit_type(desc),
                    statement_period=self.period_from_date(d),
                ))
        return txns

    def _parse_credits(self, text: str, year: int) -> List[Transaction]:
        """
        Extract lines between:
          "Deposits, credits and interest"  (spaces may be collapsed)
        and:
          "Total deposits, credits"
        """
        section = self._extract_section(
            text,
            start_pattern=r"Deposits,?\s*credits\s*and\s*interest",
            end_pattern=r"Total\s*deposits",
        )
        txns = []
        for raw_date, desc, amt_str in self._parse_tx_lines(section):
            d = self.parse_date(raw_date, year)
            amt = self.parse_amount(amt_str)
            if d and amt is not None:
                txn_type = TransactionType.TRANSFER_IN \
                    if "MONEYLINE" in desc.upper() \
                    else TransactionType.CREDIT
                txns.append(Transaction(
                    account_id="",
                    date=d,
                    description=self._clean_desc(desc),
                    raw_description=desc,
                    amount=abs(amt),  # credits are positive
                    transaction_type=txn_type,
                    statement_period=self.period_from_date(d),
                    is_transfer=(txn_type == TransactionType.TRANSFER_IN),
                ))
        return txns

    def _extract_section(self, text: str,
                         start_pattern: str, end_pattern: str) -> str:
        """
        Extract text between start_pattern and end_pattern.
        Uses the LAST occurrence of start_pattern before end_pattern to avoid
        matching the account-summary line that repeats section labels with totals.
        """
        end_m = re.search(end_pattern, text, re.IGNORECASE)
        end = end_m.start() if end_m else len(text)

        # Collect all start-pattern matches that come before the end boundary
        start_matches = [
            m for m in re.finditer(start_pattern, text, re.IGNORECASE)
            if m.start() < end
        ]
        if not start_matches:
            return ""

        # Use the last valid occurrence (the actual detail section, not the summary)
        start_m = start_matches[-1]
        start = start_m.end()

        if start >= end:
            return ""
        return text[start:end]

    # Transaction line regex:
    #   01/09 DEBIT CARD RECURRING PYMT INCFILE LLC ...  29.00
    _TX_RE = re.compile(
        r"^(\d{2}/\d{2})\s+(.+?)\s+([\d,]+\.\d{2})\s*$",
        re.MULTILINE,
    )

    def _parse_tx_lines(self, section: str):
        """Yield (date_str, description, amount_str) tuples from a section."""
        for m in self._TX_RE.finditer(section):
            yield m.group(1), m.group(2).strip(), m.group(3)

    @staticmethod
    def _clean_desc(raw: str) -> str:
        """Normalise description: collapse whitespace, strip junk suffixes."""
        d = re.sub(r"\s+", " ", raw).strip()
        # remove trailing card codes like  "0047"
        d = re.sub(r"\s+\d{4}\s*$", "", d)
        return d

    @staticmethod
    def _classify_debit_type(desc: str) -> TransactionType:
        desc_up = desc.upper()
        if "IRS" in desc_up or "USATAXPYMT" in desc_up:
            return TransactionType.TAX
        if "PAYROLL" in desc_up:
            return TransactionType.TAX
        if "TRAN FEE" in desc_up or "SERVICE CHARGE" in desc_up:
            return TransactionType.FEE
        return TransactionType.DEBIT
