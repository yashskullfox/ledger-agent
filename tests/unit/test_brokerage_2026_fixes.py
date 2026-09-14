"""
Tests for 2026 brokerage parser enhancements (BROKER_Z and BROKER_Y real format handling).
Strictly pseudonymised: uses ENTITY_A, BANK_X, BROKER_Y, BROKER_Z, and synthetic figures.
"""

from datetime import date
from decimal import Decimal

from ledger_agent.core.models import PositionType, TransactionType
from ledger_agent.core.parsers.broker_y_brokerage import BrokerYBrokerageParser
from ledger_agent.core.parsers.broker_z import BrokerZParser


class TestBrokerZ2026Fixes:
    """Verify Broker Z parser enhancements."""

    def test_entity_name_with_name_prefix(self):
        """Task 1a: Entity name prefixed with 'Name ' is correctly extracted."""
        parser = BrokerZParser()
        text = """
BROKER_Z LLC, Activity Statement
Account Information
Name ENTITY_A LLC
Account U1234567
"""
        assert parser._extract_entity_name(text) == "ENTITY_A LLC"

    def test_cash_disbursement_and_transfer_parsing(self):
        """Task 1b: Deposits & Withdrawals cash transactions are extracted and classified."""
        parser = BrokerZParser()
        text = """
Ending Accrual Balance 10.00
Deposits & Withdrawals
Date Description Amount
USD
2026-01-14 Disbursement Initiated by Partner -30,000.00
2026-03-20 Electronic Fund Transfer 10,500.00
Total -19,500.00
Financial Instrument Information
"""
        txns = parser._parse_cash_transactions(text, "2026-01", 2026)
        assert len(txns) == 2

        disbursement = next(t for t in txns if t.amount < 0)
        assert disbursement.date == date(2026, 1, 14)
        assert disbursement.amount == Decimal("-30000.00")
        assert disbursement.transaction_type == TransactionType.TRANSFER_OUT
        assert disbursement.is_transfer is True
        assert disbursement.coa_code == "9000"

        transfer_in = next(t for t in txns if t.amount > 0)
        assert transfer_in.date == date(2026, 3, 20)
        assert transfer_in.amount == Decimal("10500.00")
        assert transfer_in.transaction_type == TransactionType.TRANSFER_IN
        assert transfer_in.is_transfer is True
        assert transfer_in.coa_code == "9000"

    def test_trade_parsing_multiline(self):
        """Task 1c: Multi-line trades with realized P/L are correctly extracted."""
        parser = BrokerZParser()
        text = """
Trades
Symbol Date/Time Quantity T. Price C. Price Proceeds Comm/Fee Basis Realized P/L MTM P/L Code
Stocks
USD
2026-01-12,
TICKER_A -2,000 20.0000 20.0000 40,000.00 -10.00 -35,000.00 5,000.00 -100.00 C
14:00:00
Total TICKER_A -2,000 40,000.00 -10.00 -35,000.00 5,000.00 -100.00
2026-01-15,
TICKER_B 100 10.0000 10.0000 -1,000.00 -1.00 1,001.00 0.00 0.00 O
15:00:00
Dividends
"""
        trades = parser._parse_trades(text, "2026-01", 2026)
        # Trades with P/L == 0 are filtered out; trades with non-zero P/L are retained
        assert len(trades) == 1
        assert trades[0].symbol == "TICKER_A"
        assert trades[0].settlement_date == date(2026, 1, 12)
        assert trades[0].gain_loss == Decimal("5000.00")

    def test_option_trade_total_parsing(self):
        """Task 1e: Options trade Total summary lines with non-zero realized P/L are parsed."""
        parser = BrokerZParser()
        text = (
            "Trades\n"
            "Symbol Date/Time Quantity T. Price C. Price Proceeds Comm/Fee Basis Realized P/L MTM P/L Code\n"
            "Equity and Index Options\n"
            "USD\n"
            "2026-01-26,\n"
            "OPT1 17JUL26 38 C 10 3.4900 3.4680 -3,490.00 -7.01 3,497.01 0.00 -22.00 O\n"
            "13:51:45\n"
            "Total OPT1 17JUL26 38 C 10 -3,490.00 -7.01 3,497.01 0.00 -22.00\n"
            "2026-01-05,\n"
            "Total OPT2 20FEB26 18 P 0 250.00 -7.00 0.00 243.00 125.45\n"
            "2026-01-13,\n"
            "Total OPT3 20MAR26 17 C -5 4,175.00 -3.52 -1,388.32 2,783.16 80.75\n"
            "Total 5,525.00 -24.58 -2,474.26 3,026.15 216.00\n"
            "Dividends\n"
        )
        trades = parser._parse_trades(text, "2026-01", 2026)
        # Trades with P/L == 0 (OPT1) are filtered out; OPT2 and OPT3 are retained
        assert len(trades) == 2
        assert trades[0].symbol == "OPT2 20FEB26 18 P"
        assert trades[0].gain_loss == Decimal("243.00")
        assert trades[0].settlement_date == date(2026, 1, 5)

        assert trades[1].symbol == "OPT3 20MAR26 17 C"
        assert trades[1].gain_loss == Decimal("2783.16")
        assert trades[1].settlement_date == date(2026, 1, 13)
        assert sum(t.gain_loss for t in trades) == Decimal("3026.16")

    def test_positions_stocks_and_options(self):
        """Task 1d: Multi-column position layout parses both equity and option positions."""
        parser = BrokerZParser()
        text = """
Open Positions
Symbol Quantity Mult Cost Price Cost Basis Close Price Value Unrealized P/L Code
Stocks
USD
TICKER_A 1,000 1 10.0000 10,000.00 12.0000 12,000.00 2,000.00 SY
TICKER_B 500 1 20.0000 10,000.00 18.0000 9,000.00 -1,000.00
Total 20,000.00 21,000.00 1,000.00
Symbol Quantity Mult Cost Price Cost Basis Close Price Value Unrealized P/L Code
Equity and Index Options
USD
OPT1 17JUL26 30 C 10 100 3.0000 3,000.00 3.5000 3,500.00 500.00
Total 3,000.00 3,500.00 500.00
Net Stock Position Summary
"""
        positions = parser._parse_positions(text, "2026-01", 2026)
        assert len(positions) == 3

        stock_positions = [p for p in positions if p.position_type == PositionType.EQUITY]
        option_positions = [p for p in positions if p.position_type == PositionType.OPTION]

        assert len(stock_positions) == 2
        assert len(option_positions) == 1
        assert option_positions[0].symbol == "OPT1 17JUL26 30 C"
        assert option_positions[0].market_value == Decimal("3500.00")


class TestBrokerY2026Fixes:
    """Verify Broker Y parser enhancements."""

    def test_deposits_parsing(self):
        """Task 2a: Deposits and Exchanges In sections are parsed as additions."""
        parser = BrokerYBrokerageParser()
        text = (
            "Deposits\n"
            "Date Reference Description Amount\n"
            "01/14 Eft Funds Received Er12345678 /web $30,000.00\n"  # redaction: allow
            "BANK_X Bank ******1234\n"
            "Total Deposits $30,000.00\n"  # redaction: allow
            "Exchanges In\n"
            "Symbol/\n"
            "Date Security Name CUSIP Description Quantity Price Amount\n"
            "01/08 Z01-123456-1 Transferred From - - $4,000.00\n"  # redaction: allow
            "Total Exchanges In $4,000.00\n"  # redaction: allow
        )
        txns = parser._parse_deposits(text, "2026-01", 2026)
        assert len(txns) == 2

        eft = next(t for t in txns if t.amount == Decimal("30000.00"))
        assert eft.date == date(2026, 1, 14)
        assert eft.transaction_type == TransactionType.TRANSFER_IN
        assert eft.is_transfer is True
        assert eft.coa_code == "9000"

        exch = next(t for t in txns if t.amount == Decimal("4000.00"))
        assert exch.date == date(2026, 1, 8)
        assert exch.transaction_type == TransactionType.TRANSFER_IN
        assert exch.is_transfer is True
        assert exch.coa_code == "9000"

    def test_withdrawals_multiline_parsing(self):
        """Task 2b: Multi-line withdrawals and Exchanges Out are parsed."""
        parser = BrokerYBrokerageParser()
        text = (
            "Withdrawals\n"
            "Date Reference Description Amount\n"
            "01/09 Money Line Paid EFT FUNDS PAID ED12345678 /WEB -$2,500.00\n"  # redaction: allow
            "BANK_X BANK ******1234\n"
            "Total Withdrawals -$2,500.00\n"  # redaction: allow
            "Exchanges Out\n"
            "Symbol/\n"
            "Date Security Name CUSIP Description Quantity Price Amount\n"
            "01/13 Z01-123456-1 Transferred To - - -$4,000.00\n"  # redaction: allow
            "Total Exchanges Out -$4,000.00\n"  # redaction: allow
        )
        txns = parser._parse_withdrawals(text, "2026-01", 2026)
        assert len(txns) >= 1

        ml = next(t for t in txns if t.amount == Decimal("-2500.00"))
        assert ml.date == date(2026, 1, 9)
        assert ml.transaction_type == TransactionType.TRANSFER_OUT
        assert ml.is_transfer is True
        assert ml.coa_code == "9000"

    def test_gross_asset_value_fallback(self):
        """Task 2c: When Market Value of Holdings is absent, falls back to Ending Account Value."""
        parser = BrokerYBrokerageParser()
        text = (
            "ENTITY_A LLC  Z01-123456\n"
            "Ending Account Value ** $86,000.00 $86,000.00\n"  # redaction: allow
            "Beginning Account Value  $50,000.00\n"  # redaction: allow
            "Withdrawals  $2,500.00\n"  # redaction: allow
        )
        snap = parser._parse_summary(text, "2026-01")
        assert snap.ending_balance == Decimal("86000.00")
        assert snap.gross_asset_value == Decimal("86000.00")
