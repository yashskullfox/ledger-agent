"""
parsers/usbank_creditcard.py  –  U.S. Bank Business Credit Card parser

Handles U.S. Bank Business Triple Cash Rewards Card statements (4-page PDF).

Statement layout:
  Page 1  – Account summary: period, account number, new balance, summary totals
  Pages 3-4 – Transaction detail sections:
    "Other Credits"           – refunds/credits (marked CR on the following char-row)
    "Purchases and Other Debits" – expense charges
    "BILLING ACCOUNT ACTIVITY / Payments and Other Credits" – card payments

Transaction line format (chars merged by y-coordinate):
  PostDate TransDate RefNum Description $Amount
  02/1702/135614IN *HACKING LAW  314-9618200  MO$3,000.00

Regex captures: PostDate(MM/DD) TransDate(MM/DD) RefNum(4d) Description Amount
CR notation appears on the next y-row; we track this to mark credits correctly.
"""
from __future__ import annotations

import collections
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

import pdfplumber

from core.models import (
    AccountSnapshot, ParsedStatement,
    StatementType, Transaction, TransactionType,
)
from parsers.base import BaseStatementParser
from parsers.registry import ParserRegistry


@ParserRegistry.register
class USBankCreditCardParser(BaseStatementParser):
    """Parser for U.S. Bank Business Credit Card statements."""

    PARSER_ID = "usbank_creditcard"
    INSTITUTION = "U.S. Bank"

    @classmethod
    def can_parse(cls, text: str) -> bool:
        upper = text.upper()
        return (
                ("U.S. BANK" in upper or "USBANK" in upper)
                and ("TRIPLE CASH" in upper or "CREDIT CARD" in upper
                     or "CENTRAL BILL" in upper or "CARDMEMBER" in upper)
        )

    def parse(self, pdf_path: Path) -> ParsedStatement:
        lines = _extract_lines_by_y(pdf_path)
        full_text = "\n".join(lines)

        period, year = self._extract_period(lines)
        account_no = self._extract_account_number(lines)
        entity_name = self._extract_entity_name(lines)
        new_bal = self._extract_new_balance(full_text)

        charges, credits, payments = self._parse_transactions(lines, year, period)
        all_txns = charges + credits + payments
        total_charges = sum(abs(t.amount) for t in charges)
        total_credits = sum(abs(t.amount) for t in credits + payments)

        snapshot = AccountSnapshot(
            account_id="",
            statement_period=period,
            ending_balance=new_bal if new_bal is not None else Decimal("0"),
            total_debits=total_charges,
            total_credits=total_credits,
        )

        return ParsedStatement(
            parser_id=self.PARSER_ID,
            statement_type=StatementType.CREDIT_CARD,
            institution=self.INSTITUTION,
            account_number_masked=self.mask_account(account_no),
            statement_period=period,
            entity_name=entity_name,
            transactions=all_txns,
            snapshot=snapshot,
            raw_text=full_text,
            source_file=str(pdf_path),
        )

    def _extract_period(self, lines: List[str]) -> Tuple[str, int]:
        for line in lines:
            m = re.search(r"Closing Date[:\s]*(\d{2}/\d{2}/(\d{4}))", line, re.IGNORECASE)
            if m:
                d = self.parse_date(m.group(1))
                if d:
                    return self.period_from_date(d), d.year
            m2 = re.search(
                r"(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/(\d{4}))", line
            )
            if m2:
                d = self.parse_date(m2.group(2))
                if d:
                    return self.period_from_date(d), d.year
        return "0000-00", 0

    def _extract_account_number(self, lines: List[str]) -> str:
        for line in lines:
            m = re.search(r"Account Ending in[:\s#*]+(\d{4})\s*$", line, re.IGNORECASE)
            if m:
                return m.group(1)
            m2 = re.search(r"\d{4}\s+\d{4}\s+\d{4}\s+(\d{4})", line)
            if m2:
                return m2.group(1)
        return "0000"

    def _extract_entity_name(self, lines: List[str]) -> str:
        for line in lines:
            stripped = line.strip()
            if re.match(r"^[A-Z][A-Z &,]+(?:LLC|INC|CORP|CO|LTD)$", stripped):
                return stripped
            m = re.match(r"^([A-Z][A-Z &,]+(?:LLC|INC|CORP|CO|LTD))\s*\(", stripped)
            if m:
                return m.group(1).strip()
        return "UNKNOWN ENTITY"

    def _extract_new_balance(self, full_text: str) -> Optional[Decimal]:
        m = re.search(r"New Balance[=\s]*\$?([\d,]+\.\d{2})", full_text, re.IGNORECASE)
        return self.parse_amount(m.group(1)) if m else None

    _TX_RE = re.compile(
        r"^(\d{2}/\d{2})(\d{2}/\d{2})\d{4}(.+?)\$([\d,]+\.\d{2})\s*$"
    )

    def _parse_transactions(
            self, lines: List[str], year: int, period: str,
    ) -> Tuple[List[Transaction], List[Transaction], List[Transaction]]:
        """
        Return (charges, credits, payments).
        Charges are expenses (negative), credits are refunds (positive),
        payments are transfer-in to the card account (positive, reduce liability).
        """
        charges: List[Transaction] = []
        credits: List[Transaction] = []
        payments: List[Transaction] = []

        mode: Optional[str] = None
        prev_txn: Optional[Transaction] = None

        for line in lines:
            if re.search(r"^Other Credits\s*$", line, re.IGNORECASE):
                mode = "credit"
                continue
            if re.search(r"^Purchases and Other Debits\s*$", line, re.IGNORECASE):
                mode = "charge"
                continue
            if re.search(r"Payments and Other Credits", line, re.IGNORECASE):
                mode = "payment"
                continue
            if re.search(r"^Total for Account|^2026 Totals|^Interest Charge", line, re.IGNORECASE):
                mode = None
                prev_txn = None
                continue

            if line.strip() == "CR" and prev_txn is not None:
                prev_txn = None
                continue

            if mode is None:
                continue

            m = self._TX_RE.match(line)
            if not m:
                prev_txn = None
                continue

            post_date_str = m.group(1)
            desc_raw = m.group(3).strip()
            amt_str = m.group(4)

            post_date = _parse_mmdd(post_date_str, year)
            if post_date is None:
                prev_txn = None
                continue
            amt = self.parse_amount(amt_str)
            if amt is None:
                prev_txn = None
                continue

            desc = _clean_cc_desc(desc_raw)

            if mode == "charge":
                txn = Transaction(
                    account_id="",
                    date=post_date,
                    description=desc,
                    raw_description=desc_raw,
                    amount=-abs(amt),
                    transaction_type=TransactionType.DEBIT,
                    statement_period=period,
                )
                charges.append(txn)
            elif mode == "credit":
                txn = Transaction(
                    account_id="",
                    date=post_date,
                    description=desc,
                    raw_description=desc_raw,
                    amount=abs(amt),
                    transaction_type=TransactionType.CREDIT,
                    statement_period=period,
                )
                credits.append(txn)
            elif mode == "payment":
                txn = Transaction(
                    account_id="",
                    date=post_date,
                    description=desc,
                    raw_description=desc_raw,
                    amount=abs(amt),
                    transaction_type=TransactionType.TRANSFER_IN,
                    statement_period=period,
                    is_transfer=True,
                )
                payments.append(txn)
            else:
                prev_txn = None
                continue

            prev_txn = txn

        return charges, credits, payments


def _extract_lines_by_y(pdf_path: Path) -> List[str]:
    """Reconstruct text lines by grouping PDF chars sharing the same vertical position."""
    lines: List[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            rows: dict = collections.defaultdict(list)
            for c in page.chars:
                rows[round(c["top"])].append(c)
            for y in sorted(rows):
                line = "".join(
                    ch["text"] for ch in sorted(rows[y], key=lambda ch: ch["x0"])
                )
                if line.strip():
                    lines.append(line.strip())
    return lines


def _parse_mmdd(raw: str, year: int) -> Optional[date]:
    m = re.match(r"(\d{2})/(\d{2})", raw)
    if not m:
        return None
    try:
        return date(year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def _clean_cc_desc(raw: str) -> str:
    cleaned = re.sub(r"\s{2,}", " ", raw).strip()
    cleaned = re.sub(r"\s+[A-Z]{2}$", "", cleaned)
    return cleaned
