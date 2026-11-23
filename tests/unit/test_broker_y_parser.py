"""
tests/unit/test_broker_y_parser.py – Unit tests for BrokerYBrokerageParser.
Uses pseudonymised test data (ENTITY_A, synthetic dates, and synthetic figures).
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ledger_agent.core.exceptions import ParserGap
from ledger_agent.core.models import StatementType, TransactionType
from ledger_agent.core.parsers.broker_y_brokerage import BrokerYBrokerageParser


class TestBrokerYBrokerageParserUnit:
    def test_extract_period_range_format(self):
        parser = BrokerYBrokerageParser()
        text = "For Period January 1, 2025 – January 31, 2025"
        period, year = parser._extract_period(text)
        assert period == "2025-01"
        assert year == 2025

    def test_extract_period_single_date_format(self):
        parser = BrokerYBrokerageParser()
        text = "As of February 28, 2025"
        period, year = parser._extract_period(text)
        assert period == "2025-02"
        assert year == 2025

    def test_extract_period_fallback(self):
        parser = BrokerYBrokerageParser()
        period, year = parser._extract_period("No valid dates here")
        assert period == "0000-00"
        assert year == 0

    def test_extract_account_number(self):
        parser = BrokerYBrokerageParser()
        assert parser._extract_account_number("Account Z23-123456") == "Z23123456"  # redaction: allow
        assert parser._extract_account_number("Account Number: ACCT-98765") == "98765"  # redaction: allow
        assert parser._extract_account_number("No account pattern") == "0000"

    def test_extract_entity_name(self):
        parser = BrokerYBrokerageParser()
        text = "REPORT\nBETA ASSETS LLC\nAddress"
        assert parser._extract_entity_name(text) == "BETA ASSETS LLC"
        assert parser._extract_entity_name("invalid line") == "UNKNOWN ENTITY"

    def test_parse_summary_success(self):
        parser = BrokerYBrokerageParser()
        text = (
            "Beginning Account Value $10,000.00\n"
            "Ending Account Value $12,500.00\n"
            "Market Value of Holdings $15,000.00\n"
            "Margin balance -$2,500.00\n"
            "Withdrawals -$500.00\n"
            "Net Gain/Loss $1,000.00\n"
        )
        snap = parser._parse_summary(text, "2025-01")
        assert snap.statement_period == "2025-01"
        assert snap.beginning_balance == Decimal("10000.00")
        assert snap.ending_balance == Decimal("12500.00")
        assert snap.gross_asset_value == Decimal("15000.00")
        assert snap.margin_balance == Decimal("-2500.00")
        assert snap.total_withdrawals == Decimal("500.00")
        assert snap.realised_gain_loss == Decimal("1000.00")

    def test_parse_summary_missing_ending_nav_raises_parser_gap(self):
        parser = BrokerYBrokerageParser()
        text = "Beginning Account Value $10,000.00\nNo ending value"
        with pytest.raises(ParserGap) as exc_info:
            parser._parse_summary(text, "2025-01")
        assert "ending_balance" in exc_info.value.missing_fields

    def test_parse_deposits(self):
        parser = BrokerYBrokerageParser()
        text = (
            "Deposits\n"
            "Date  Description                   Amount\n"
            "01/10 Wire Transfer In             $5,000.00\n"
            "01/15 Direct Deposit Client Fee     $1,200.00\n"
            "Total Deposits                     $6,200.00\n"
        )
        txns = parser._parse_deposits(text, "2025-01", 2025)
        assert len(txns) == 2
        wire_txn = next(t for t in txns if "Wire" in t.description)
        assert wire_txn.is_transfer is True
        assert wire_txn.transaction_type == TransactionType.TRANSFER_IN
        assert wire_txn.amount == Decimal("5000.00")

        fee_txn = next(t for t in txns if "Client Fee" in t.description)
        assert fee_txn.is_transfer is False
        assert fee_txn.transaction_type == TransactionType.CREDIT

    def test_parse_withdrawals(self):
        parser = BrokerYBrokerageParser()
        text = (
            "Withdrawals\n"
            "Date  Description                   Amount\n"
            "01/20 Wire Transfer Out           -$2,000.00\n"
            "01/25 Annual Fee                  -$150.00\n"
            "Total Withdrawals                 -$2,150.00\n"
        )
        txns = parser._parse_withdrawals(text, "2025-01", 2025)
        assert len(txns) == 2
        wire_out = next(t for t in txns if "Wire" in t.description)
        assert wire_out.is_transfer is True
        assert wire_out.transaction_type == TransactionType.TRANSFER_OUT
        assert wire_out.amount == Decimal("-2000.00")

        fee_out = next(t for t in txns if "Annual Fee" in t.description)
        assert fee_out.is_transfer is False
        assert fee_out.transaction_type == TransactionType.DEBIT
        assert fee_out.amount == Decimal("-150.00")

    def test_parse_margin_interest(self):
        parser = BrokerYBrokerageParser()
        text = "01/01-01/31  10,000  8.5%  31  -$65.20\n"
        txns = parser._parse_margin_interest(text, "2025-01", 2025)
        assert len(txns) == 1
        assert txns[0].transaction_type == TransactionType.MARGIN_INTEREST
        assert txns[0].amount == Decimal("-65.20")
        assert txns[0].coa_code == "5030"

    def test_parse_dividends(self):
        parser = BrokerYBrokerageParser()
        text = (
            "Dividends, Interest & Other Income\n"
            "01/18  CORP_ALPHA INC  123456789  Dividend Received  $145.50\n"
            "Total Dividends  $145.50\n"
        )
        txns = parser._parse_dividends(text, "2025-01", 2025)
        assert len(txns) == 1
        assert txns[0].amount == Decimal("145.50")
        assert txns[0].coa_code == "4021"
        assert txns[0].coa_name == "Dividend Income"

    def test_parse_trades(self):
        parser = BrokerYBrokerageParser()
        text = (
            "s 01/12  ABC COMMON STOCK You Sold  Short-term gain: $320.00\n"
            "s 01/14  XYZ CORP NOTE You Sold      Long-term loss: $150.00\n"
        )
        trades = parser._parse_trades(text, "2025-01", 2025)
        assert len(trades) == 2
        assert trades[0].symbol == "ABC"
        assert trades[0].gain_loss == Decimal("320.00")
        assert trades[0].term == "short"

        assert trades[1].symbol == "XYZ"
        assert trades[1].gain_loss == Decimal("-150.00")
        assert trades[1].term == "long"

    def test_parse_synthetic_statement(self, monkeypatch):
        parser = BrokerYBrokerageParser()
        sample_text = (
            "BETA ASSETS LLC\n"
            "Account Z23-123456\n"  # redaction: allow
            "For Period January 1, 2025 – January 31, 2025\n"
            "Beginning Account Value $10,000.00\n"
            "Ending Account Value $12,500.00\n"
            "Market Value of Holdings $12,500.00\n"
            "Deposits\n"
            "Date  Description                   Amount\n"
            "01/10 Wire Transfer In             $2,000.00\n"
            "Total Deposits                     $2,000.00\n"
            "Withdrawals\n"
            "Date  Description                   Amount\n"
            "01/25 Broker Fee                  -$100.00\n"
            "Total Withdrawals                 -$100.00\n"
            "s 01/12  SEC1 STOCK You Sold  Short-term gain: $600.00\n"
        )
        monkeypatch.setattr(parser, "extract_text", MagicMock(return_value=sample_text))

        parsed = parser.parse(Path("dummy.pdf"))
        assert parsed.parser_id == "broker_y_brokerage"
        assert parsed.statement_type == StatementType.BROKERAGE
        assert parsed.statement_period == "2025-01"
        assert parsed.entity_name == "BETA ASSETS LLC"
        assert len(parsed.transactions) == 3  # 1 deposit + 1 withdrawal + 1 trade
        assert parsed.snapshot.ending_balance == Decimal("12500.00")
