"""
tests/test_parsers.py  –  Unit tests for statement parsers
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import TRUIST_SAMPLE_TEXT, FIDELITY_SAMPLE_TEXT


class TestParserRegistry:
    def test_truist_detected(self):
        from parsers.registry import ParserRegistry
        import parsers.truist_checking  # noqa: F401
        cls = ParserRegistry.detect(TRUIST_SAMPLE_TEXT)
        assert cls is not None
        assert cls.PARSER_ID == "truist_checking"

    def test_fidelity_detected(self):
        from parsers.registry import ParserRegistry
        import parsers.fidelity_brokerage  # noqa: F401
        cls = ParserRegistry.detect(FIDELITY_SAMPLE_TEXT)
        assert cls is not None
        assert cls.PARSER_ID == "fidelity_brokerage"

    def test_unknown_text_returns_none(self):
        from parsers.registry import ParserRegistry
        cls = ParserRegistry.detect("Random PDF content with no known bank")
        assert cls is None

    def test_detect_or_raise_unknown_raises(self):
        from parsers.registry import ParserRegistry
        from core.exceptions import ParserNotFoundError
        with pytest.raises(ParserNotFoundError):
            ParserRegistry.detect_or_raise("Unknown bank text")


class TestTruistCheckingParser:
    @pytest.fixture
    def parser(self):
        from parsers.truist_checking import TruistCheckingParser
        return TruistCheckingParser()

    def test_can_parse_truist(self, parser):
        assert parser.can_parse(TRUIST_SAMPLE_TEXT)

    def test_cannot_parse_fidelity(self, parser):
        assert not parser.can_parse(FIDELITY_SAMPLE_TEXT)

    def test_extract_period(self, parser):
        period, year = parser._extract_period(TRUIST_SAMPLE_TEXT)
        assert period == "2025-01"
        assert year == 2025

    def test_extract_account_number(self, parser):
        acct = parser._extract_account_number(TRUIST_SAMPLE_TEXT)
        assert "1470018610272" in acct

    def test_extract_entity_name(self, parser):
        name = parser._extract_entity_name(TRUIST_SAMPLE_TEXT)
        assert "SYNCED LLC" in name

    def test_extract_balances(self, parser):
        prev, new = parser._extract_balances(TRUIST_SAMPLE_TEXT)
        assert prev == Decimal("572.15")
        assert new == Decimal("4031.20")

    def test_parse_debits_count(self, parser):
        debits = parser._parse_debits(TRUIST_SAMPLE_TEXT, 2025)
        # INCFILE, QUICKBOOKS, GOOGLE, IRS, ADOBE, GOOGLE WORKSPACE = 6
        assert len(debits) >= 4

    def test_parse_credits_count(self, parser):
        credits = parser._parse_credits(TRUIST_SAMPLE_TEXT, 2025)
        assert len(credits) == 2

    def test_debit_amounts_negative(self, parser):
        debits = parser._parse_debits(TRUIST_SAMPLE_TEXT, 2025)
        for t in debits:
            assert t.amount < 0, f"Debit should be negative: {t.description} {t.amount}"

    def test_credit_amounts_positive(self, parser):
        credits = parser._parse_credits(TRUIST_SAMPLE_TEXT, 2025)
        for t in credits:
            assert t.amount > 0, f"Credit should be positive: {t.description} {t.amount}"

    def test_moneyline_is_transfer(self, parser):
        credits = parser._parse_credits(TRUIST_SAMPLE_TEXT, 2025)
        transfers = [t for t in credits if t.is_transfer]
        assert len(transfers) == 2

    def test_irs_classified_as_tax(self, parser):
        debits = parser._parse_debits(TRUIST_SAMPLE_TEXT, 2025)
        from core.models import TransactionType
        tax_txns = [t for t in debits if t.transaction_type == TransactionType.TAX]
        assert len(tax_txns) >= 1


class TestBaseParserHelpers:
    @pytest.fixture
    def parser(self):
        from parsers.truist_checking import TruistCheckingParser
        return TruistCheckingParser()

    def test_parse_amount_decimal(self, parser):
        assert parser.parse_amount("1,234.56") == Decimal("1234.56")
        assert parser.parse_amount("29.00") == Decimal("29.00")
        assert parser.parse_amount("invalid") is None

    def test_parse_date_with_year(self, parser):
        from datetime import date
        d = parser.parse_date("01/15", 2025)
        assert d == date(2025, 1, 15)

    def test_parse_date_full(self, parser):
        from datetime import date
        d = parser.parse_date("01/15/2025")
        assert d == date(2025, 1, 15)

    def test_period_from_date(self, parser):
        from datetime import date
        p = parser.period_from_date(date(2025, 1, 31))
        assert p == "2025-01"

    def test_mask_account(self, parser):
        masked = parser.mask_account("1470018610272")
        # Returns last 4 digits only
        assert masked == "0272"
