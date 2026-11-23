"""
tests/unit/test_bank_x2_x3_parsers.py – Unit tests for Bank X2 and Bank X3 parsers.
Uses pseudonymised test data (ENTITY_A, synthetic dates and amounts).
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from ledger_agent.core.models import StatementType, TransactionType
from ledger_agent.core.parsers.bank_x2_checking import BankX2CheckingParser
from ledger_agent.core.parsers.bank_x3_checking import BankX3CheckingParser


class TestBankX2CheckingParserUnit:
    def test_extract_period_through_format(self):
        parser = BankX2CheckingParser()
        text = "Statement Period: 01/01/2025 through 01/31/2025"
        period, year = parser._extract_period(text)
        assert period == "2025-01"
        assert year == 2025

    def test_extract_period_hyphen_format(self):
        parser = BankX2CheckingParser()
        text = "Statement Period: 02/01/2025 - 02/28/2025"
        period, year = parser._extract_period(text)
        assert period == "2025-02"
        assert year == 2025

    def test_extract_period_fallback(self):
        parser = BankX2CheckingParser()
        period, year = parser._extract_period("No dates here")
        assert period == "0000-00"
        assert year == 0

    def test_extract_account_number_formats(self):
        parser = BankX2CheckingParser()
        assert parser._extract_account_number("Account Number: 987654321") == "987654321"  # redaction: allow
        assert parser._extract_account_number("account ending in 4321") == "4321"  # redaction: allow
        assert parser._extract_account_number("123456789012") == "123456789012"  # redaction: allow
        assert parser._extract_account_number("No account digits") == "0000"

    def test_extract_entity_name(self):
        parser = BankX2CheckingParser()
        text = "HEADER\nALPHA TRADING LLC\nAddress line"
        assert parser._extract_entity_name(text) == "ALPHA TRADING LLC"
        assert parser._extract_entity_name("lower case line\nno entity") == "UNKNOWN ENTITY"

    def test_extract_balances(self):
        parser = BankX2CheckingParser()
        text = "Beginning Balance $1,234.56\nEnding Balance $2,345.67"
        prev, new = parser._extract_balances(text)
        assert prev == Decimal("1234.56")
        assert new == Decimal("2345.67")

    def test_classify_type_and_clean_desc(self):
        parser = BankX2CheckingParser()
        assert parser._classify_type("TRANSFER TO SAVINGS", is_debit=True) == TransactionType.TRANSFER_OUT
        assert parser._classify_type("ZELLE FROM ALICE", is_debit=False) == TransactionType.TRANSFER_IN
        assert parser._classify_type("WIRE OUT", is_debit=True) == TransactionType.TRANSFER_OUT
        assert parser._classify_type("IRS USATAXPYMT", is_debit=True) == TransactionType.TAX
        assert parser._classify_type("MONTHLY SERVICE FEE", is_debit=True) == TransactionType.FEE
        assert parser._classify_type("GROCERY STORE", is_debit=True) == TransactionType.DEBIT
        assert parser._classify_type("CLIENT PAYMENT", is_debit=False) == TransactionType.CREDIT

        assert parser._clean_desc("  SUPPLIES STORE   1234  ") == "SUPPLIES STORE"

    def test_parse_synthetic_statement(self, monkeypatch):
        parser = BankX2CheckingParser()
        sample_text = (
            "ALPHA TRADING LLC\n"
            "Account Number: 987654321012\n"  # redaction: allow
            "Statement Period: 01/01/2025 through 01/31/2025\n"
            "Opening Balance $1,000.00\n"
            "Deposits and other credits\n"
            "Date   Description                                    Amount\n"
            "01/06/25  Online Banking transfer from savings        2,000.00\n"
            "01/20  Zelle Credit from ALICE                         500.00\n"
            "Total deposits and other credits                      2,500.00\n"
            "Withdrawals and other debits\n"
            "Date   Description                                    Amount\n"
            "01/07  RECURRING PAYMENT SLACK                          15.00\n"
            "01/14  USATAXPYMT IRS                                   70.00\n"
            "Total withdrawals and other debits                       85.00\n"
            "Ending Balance $3,415.00\n"
        )
        monkeypatch.setattr(parser, "extract_text", MagicMock(return_value=sample_text))

        parsed = parser.parse(Path("dummy.pdf"))
        assert parsed.parser_id == "bank_x2_checking"
        assert parsed.statement_type == StatementType.BANK_CHECKING
        assert parsed.statement_period == "2025-01"
        assert parsed.entity_name == "ALPHA TRADING LLC"
        assert len(parsed.transactions) == 4

        credits = [t for t in parsed.transactions if t.amount > 0]
        debits = [t for t in parsed.transactions if t.amount < 0]
        assert len(credits) == 2
        assert len(debits) == 2

        assert parsed.snapshot.beginning_balance == Decimal("1000.00")
        assert parsed.snapshot.ending_balance == Decimal("3415.00")
        assert parsed.snapshot.total_credits == Decimal("2500.00")
        assert parsed.snapshot.total_debits == Decimal("85.00")


class TestBankX3CheckingParserUnit:
    def test_extract_period_long_format(self):
        parser = BankX3CheckingParser()
        text = "January 01 - January 31, 2025"
        period, year = parser._extract_period(text)
        assert period == "2025-01"
        assert year == 2025

    def test_extract_period_numeric_format(self):
        parser = BankX3CheckingParser()
        text = "Statement period: 03/01/25 - 03/31/25"
        period, year = parser._extract_period(text)
        assert period == "2025-03"
        assert year == 2025

        text4 = "Statement period: 04/01/2025 - 04/30/2025"
        period4, year4 = parser._extract_period(text4)
        assert period4 == "2025-04"
        assert year4 == 2025

    def test_extract_period_fallback(self):
        parser = BankX3CheckingParser()
        period, year = parser._extract_period("No dates present")
        assert period == "0000-00"
        assert year == 0

    def test_extract_account_number_formats(self):
        parser = BankX3CheckingParser()
        assert parser._extract_account_number("Account Number: ...123456") == "123456"  # redaction: allow
        assert parser._extract_account_number("Account ending in 7890") == "7890"  # redaction: allow
        assert parser._extract_account_number("1234567890") == "1234567890"  # redaction: allow
        assert parser._extract_account_number("Nothing here") == "0000"

    def test_extract_entity_name(self):
        parser = BankX3CheckingParser()
        text = "TITLE\nPARTNER HOLDINGS LLC\n123 Street"
        assert parser._extract_entity_name(text) == "PARTNER HOLDINGS LLC"
        assert parser._extract_entity_name("invalid line") == "UNKNOWN ENTITY"

    def test_extract_balances(self):
        parser = BankX3CheckingParser()
        text = "Beginning Balance $5,000.00\nClosing Balance $7,500.00"
        prev, new = parser._extract_balances(text)
        assert prev == Decimal("5000.00")
        assert new == Decimal("7500.00")

    def test_classify_type_and_clean_desc(self):
        parser = BankX3CheckingParser()
        assert parser._classify_type("ONLINE TRANSFER TO SAVINGS", is_debit=True) == TransactionType.TRANSFER_OUT
        assert parser._classify_type("ZELLE TRANSFER IN", is_debit=False) == TransactionType.TRANSFER_IN
        assert parser._classify_type("IRS PAYMENT", is_debit=True) == TransactionType.TAX
        assert parser._classify_type("SERVICE CHARGE", is_debit=True) == TransactionType.FEE
        assert parser._classify_type("STORE PURCHASE", is_debit=True) == TransactionType.DEBIT
        assert parser._classify_type("DIRECT DEPOSIT", is_debit=False) == TransactionType.CREDIT

        assert parser._clean_desc("  VENDOR STORE   5678  ") == "VENDOR STORE"

    def test_parse_synthetic_statement(self, monkeypatch):
        parser = BankX3CheckingParser()
        sample_text = (
            "PARTNER HOLDINGS LLC\n"
            "Account Number: ...11223344\n"  # redaction: allow
            "January 01 - January 31, 2025\n"
            "Beginning Balance $2,000.00\n"
            "DEPOSITS AND ADDITIONS\n"
            "DATE        DESCRIPTION                                 AMOUNT\n"
            "01/15       Online Transfer from savings                1,500.00\n"
            "01/22       Zelle payment from BOB                       250.00\n"
            "Total Deposits                                          1,750.00\n"
            "ATM & DEBIT CARD WITHDRAWALS\n"
            "DATE        DESCRIPTION                                 AMOUNT\n"
            "01/03       RECURRING DEBIT CARD QUICKBOOKS                30.00\n"
            "01/10       RECURRING DEBIT CARD GOOGLE                    10.00\n"
            "ELECTRONIC WITHDRAWALS\n"
            "DATE        DESCRIPTION                                 AMOUNT\n"
            "01/07       Zelle To CHARLIE                              500.00\n"
            "Total Electronic                                          500.00\n"
            "Ending Balance $3,210.00\n"
        )
        monkeypatch.setattr(parser, "extract_text", MagicMock(return_value=sample_text))

        parsed = parser.parse(Path("dummy.pdf"))
        assert parsed.parser_id == "bank_x3_checking"
        assert parsed.statement_type == StatementType.BANK_CHECKING
        assert parsed.statement_period == "2025-01"
        assert parsed.entity_name == "PARTNER HOLDINGS LLC"
        assert len(parsed.transactions) == 5

        credits = [t for t in parsed.transactions if t.amount > 0]
        debits = [t for t in parsed.transactions if t.amount < 0]
        assert len(credits) == 2
        assert len(debits) == 3

        assert parsed.snapshot.beginning_balance == Decimal("2000.00")
        assert parsed.snapshot.ending_balance == Decimal("3210.00")
        assert parsed.snapshot.total_credits == Decimal("1750.00")
        assert parsed.snapshot.total_debits == Decimal("540.00")
