"""
parsers/usbank_checking.py  –  U.S. Bank Business Checking statement parser

Handles U.S. Bank Business Essentials / Silver Business Checking PDFs.

Statement layout (char-level reconstruction per line):
  Other Deposits
  DateDescription of TransactionRef NumberAmount
  Mar4Ext Tfr DepositTRN #= EB089E3FAD0686B$800.00

  Other Withdrawals
  DateDescription of TransactionRef NumberAmount
  Mar16Internet Banking PaymentTo Credit Card *************4594$925.44-

Amounts use US Bank's negative-suffix convention: 925.44- means debit.
"""
from __future__ import annotations

import collections
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

from ledger_agent.core.models import (
    AccountSnapshot, ParsedStatement,
    StatementType, Transaction, TransactionType,
)
from ledger_agent.core.parsers.base import BaseStatementParser
from ledger_agent.core.parsers.registry import ParserRegistry

_MONTH_MAP: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NUM: dict[str, str] = {k: f"{v:02d}" for k, v in _MONTH_MAP.items()}


@ParserRegistry.register
class USBankCheckingParser(BaseStatementParser):
    """Parser for U.S. Bank Business Checking statements."""

    PARSER_ID = "usbank_checking"
    INSTITUTION = "U.S. Bank"

    @classmethod
    def can_parse(cls, text: str) -> bool:
        upper = text.upper()
        has_usbank = "U.S. BANK" in upper or "USBANK" in upper
        has_checking = "CHECKING" in upper or "BUSINESS ESSENTIALS" in upper
        not_card = "TRIPLE CASH" not in upper and "CREDIT CARD" not in upper
        return has_usbank and has_checking and not_card

    def parse(self, pdf_path: Path) -> ParsedStatement:
        lines = _extract_lines_by_y(pdf_path)
        full_text = "\n".join(lines)

        period, year = self._extract_period(lines, full_text)
        account_no = self._extract_account_number(lines)
        entity_name = self._extract_entity_name(lines)
        prev_bal, new_bal = self._extract_balances(full_text)

        credits = self._parse_section(lines, year, period, is_debit=False)
        debits = self._parse_section(lines, year, period, is_debit=True)

        snapshot = AccountSnapshot(
            account_id="",
            statement_period=period,
            ending_balance=new_bal if new_bal is not None else Decimal("0"),
            beginning_balance=prev_bal,
            total_debits=sum(abs(t.amount) for t in debits),
            total_credits=sum(t.amount for t in credits),
        )

        return ParsedStatement(
            parser_id=self.PARSER_ID,
            statement_type=StatementType.BANK_CHECKING,
            institution=self.INSTITUTION,
            account_number_masked=self.mask_account(account_no),
            statement_period=period,
            entity_name=entity_name,
            transactions=credits + debits,
            snapshot=snapshot,
            raw_text=full_text,
            source_file=str(pdf_path),
        )

    def _extract_period(self, lines: List[str], full_text: str) -> Tuple[str, int]:
        m = re.search(
            r"Ending Balance on\s+"
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*\d{1,2},?\s*(\d{4})",
            full_text, re.IGNORECASE,
        )
        if m:
            month_num = _MONTH_NUM.get(m.group(1).lower()[:3], "01")
            year = int(m.group(2))
            return f"{year}-{month_num}", year
        for i, line in enumerate(lines):
            if "through" in line.lower():
                for nxt in lines[i + 1: i + 4]:
                    dm = re.search(
                        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*\d{1,2},?\s*(\d{4})",
                        nxt, re.IGNORECASE,
                    )
                    if dm:
                        month_num = _MONTH_NUM.get(dm.group(1).lower()[:3], "01")
                        year = int(dm.group(2))
                        return f"{year}-{month_num}", year
        return "0000-00", 0

    def _extract_account_number(self, lines: List[str]) -> str:
        for i, line in enumerate(lines):
            if "Account Number" in line:
                for candidate in [line] + lines[i + 1: i + 3]:
                    digits = re.sub(r"\D", "", candidate)
                    if len(digits) >= 8:
                        return digits
        return "0000"

    def _extract_entity_name(self, lines: List[str]) -> str:
        for line in lines:
            stripped = line.strip()
            if re.match(r"^[A-Z][A-Z &,]+(?:LLC|INC|CORP|CO|LTD)$", stripped):
                return stripped
        return "UNKNOWN ENTITY"

    def _extract_balances(self, full_text: str) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        prev = new = None
        m = re.search(r"Beginning Balance on[^$]*\$?([\d,]+\.\d{2})", full_text, re.IGNORECASE)
        if m:
            prev = self.parse_amount(m.group(1))
        m2 = re.search(r"Ending Balance on[^$]*\$?([\d,]+\.\d{2})", full_text, re.IGNORECASE)
        if m2:
            new = self.parse_amount(m2.group(1))
        return prev, new

    _TX_RE = re.compile(
        r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*(\d{1,2})"
        r"(.+?)"
        r"\$([\d,]+\.\d{2})(-?)\s*$",
        re.IGNORECASE,
    )

    def _parse_section(self, lines: List[str], year: int, period: str,
                       is_debit: bool) -> List[Transaction]:
        if is_debit:
            start_pat = r"^Other Withdrawals\s*$"
            end_pat = r"^Total Other Withdrawals"
        else:
            start_pat = r"^Other Deposits\s*$"
            end_pat = r"^Total Other Deposits"

        section_lines = _slice_section(lines, start_pat, end_pat)
        txns: List[Transaction] = []

        for line in section_lines:
            m = self._TX_RE.match(line)
            if not m:
                continue
            month_int = _MONTH_MAP.get(m.group(1).lower()[:3], 1)
            day = int(m.group(2))
            desc_raw = m.group(3).strip()
            amt_str = m.group(4)
            try:
                txn_date = date(year, month_int, day)
            except ValueError:
                continue
            amt = self.parse_amount(amt_str)
            if amt is None:
                continue
            desc = _clean_usbank_desc(desc_raw)
            txn_type = _classify(desc, is_debit)
            txns.append(Transaction(
                account_id="",
                date=txn_date,
                description=desc,
                raw_description=desc_raw,
                amount=-abs(amt) if is_debit else abs(amt),
                transaction_type=txn_type,
                statement_period=period,
                is_transfer=(txn_type in (
                    TransactionType.TRANSFER_IN, TransactionType.TRANSFER_OUT,
                )),
            ))
        return txns


def _extract_lines_by_y(pdf_path: Path) -> List[str]:
    """Reconstruct text lines by grouping PDF chars sharing the same vertical position."""
    import pdfplumber
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


def _slice_section(lines: List[str], start_pat: str, end_pat: str) -> List[str]:
    """Return lines between the first match of start_pat and end_pat."""
    in_section = False
    result: List[str] = []
    for line in lines:
        if not in_section and re.search(start_pat, line, re.IGNORECASE):
            in_section = True
            continue
        if in_section:
            if re.search(end_pat, line, re.IGNORECASE):
                break
            result.append(line)
    return result


def _clean_usbank_desc(raw: str) -> str:
    cleaned = re.sub(r"\s*TRN\s*#=\s*\S+", "", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _classify(desc: str, is_debit: bool) -> TransactionType:
    up = desc.upper()
    if any(kw in up for kw in ("TRANSFER", "TFR", "CREDIT CARD", "INTERNET BANKING PAYMENT")):
        return TransactionType.TRANSFER_OUT if is_debit else TransactionType.TRANSFER_IN
    if "IRS" in up or "TAX" in up:
        return TransactionType.TAX
    if "FEE" in up or "SERVICE CHARGE" in up:
        return TransactionType.FEE
    return TransactionType.DEBIT if is_debit else TransactionType.CREDIT
