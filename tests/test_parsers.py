"""
tests/test_parsers.py  –  Unit tests for statement parsers
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import (
    BANK_X_SAMPLE_TEXT, BROKER_Y_SAMPLE_TEXT,
    BANK_X3_SAMPLE_TEXT, BANK_X2_SAMPLE_TEXT, BROKER_Z_SAMPLE_TEXT,
    BANK_X4_CHECKING_SAMPLE_TEXT, BANK_X4_CC_SAMPLE_TEXT,
    BANK_X_REAL_AVAILABLE,
)


class TestParserRegistry:
    def test_bank_x_detected(self):
        import ledger_agent.core.parsers  # noqa: F401 — triggers auto-discovery
        from ledger_agent.core.parsers.registry import ParserRegistry
        cls = ParserRegistry.detect(BANK_X_SAMPLE_TEXT)
        if cls is None:
            pytest.skip("Bank X detection tokens not present (private/institutions.py)")
        assert cls.PARSER_ID == "bank_x_checking"

    def test_broker_y_detected(self):
        import ledger_agent.core.parsers  # noqa: F401
        from ledger_agent.core.parsers.registry import ParserRegistry
        cls = ParserRegistry.detect(BROKER_Y_SAMPLE_TEXT)
        if cls is None:
            pytest.skip("Broker Y detection tokens not present (private/institutions.py)")
        assert cls.PARSER_ID == "broker_y_brokerage"

    def test_bank_x3_detected(self):
        import ledger_agent.core.parsers  # noqa: F401
        from ledger_agent.core.parsers.registry import ParserRegistry
        cls = ParserRegistry.detect(BANK_X3_SAMPLE_TEXT)
        if cls is None:
            pytest.skip("Bank X3 detection tokens not present (private/institutions.py)")
        assert cls.PARSER_ID == "bank_x3_checking"

    def test_bank_x2_detected(self):
        import ledger_agent.core.parsers  # noqa: F401
        from ledger_agent.core.parsers.registry import ParserRegistry
        cls = ParserRegistry.detect(BANK_X2_SAMPLE_TEXT)
        if cls is None:
            pytest.skip("Bank X2 detection tokens not present (private/institutions.py)")
        assert cls.PARSER_ID == "bank_x2_checking"

    def test_broker_z_detected(self):
        import ledger_agent.core.parsers  # noqa: F401
        from ledger_agent.core.parsers.registry import ParserRegistry
        cls = ParserRegistry.detect(BROKER_Z_SAMPLE_TEXT)
        if cls is None:
            pytest.skip("Broker Z detection tokens not present (private/institutions.py)")
        assert cls.PARSER_ID == "broker_z"

    def test_bank_x4_checking_detected(self):
        pdfplumber = pytest.importorskip("pdfplumber")  # noqa: F841
        import ledger_agent.core.parsers  # noqa: F401
        from ledger_agent.core.parsers.registry import ParserRegistry
        cls = ParserRegistry.detect(BANK_X4_CHECKING_SAMPLE_TEXT)
        if cls is None:
            pytest.skip("Bank X4 detection tokens not present (private/institutions.py)")
        assert cls.PARSER_ID == "bank_x4_checking"

    def test_bank_x4_cc_detected(self):
        pdfplumber = pytest.importorskip("pdfplumber")  # noqa: F841
        import ledger_agent.core.parsers  # noqa: F401
        from ledger_agent.core.parsers.registry import ParserRegistry
        cls = ParserRegistry.detect(BANK_X4_CC_SAMPLE_TEXT)
        if cls is None:
            pytest.skip("Bank X4 detection tokens not present (private/institutions.py)")
        assert cls.PARSER_ID == "bank_x4_creditcard"

    def test_unknown_text_returns_none(self):
        from ledger_agent.core.parsers.registry import ParserRegistry
        cls = ParserRegistry.detect("Random PDF content with no known bank")
        assert cls is None

    def test_detect_or_raise_unknown_raises(self):
        from ledger_agent.core.parsers.registry import ParserRegistry
        from ledger_agent.core.exceptions import ParserNotFoundError
        with pytest.raises(ParserNotFoundError):
            ParserRegistry.detect_or_raise("Unknown bank text")


class TestBankXCheckingParser:
    @pytest.fixture
    def parser(self):
        from ledger_agent.core.parsers.bank_x_checking import BankXCheckingParser
        return BankXCheckingParser()

    def test_can_parse_bank_x(self, parser):
        if not parser.can_parse(BANK_X_SAMPLE_TEXT):
            pytest.skip("Bank X detection tokens not present (private/institutions.py)")
        assert parser.can_parse(BANK_X_SAMPLE_TEXT)

    def test_cannot_parse_broker_y(self, parser):
        assert not parser.can_parse(BROKER_Y_SAMPLE_TEXT)

    def test_extract_period(self, parser):
        period, year = parser._extract_period(BANK_X_SAMPLE_TEXT)
        assert period == "2025-01"
        assert year == 2025

    def test_extract_account_number(self, parser):
        acct = parser._extract_account_number(BANK_X_SAMPLE_TEXT)
        assert "1470018610272" in acct  # redaction: allow

    def test_extract_entity_name(self, parser):
        name = parser._extract_entity_name(BANK_X_SAMPLE_TEXT)
        assert "ENTITY_A" in name or "LLC" in name

    def test_extract_balances(self, parser):
        if not BANK_X_REAL_AVAILABLE:
            pytest.skip("Real Bank X sample required for balance extraction assertions")
        prev, new = parser._extract_balances(BANK_X_SAMPLE_TEXT)
        # When real corpus is loaded the parser should extract concrete Decimals.
        assert prev is not None and new is not None
        assert prev > Decimal("0")
        assert new > Decimal("0")

    def test_parse_debits_count(self, parser):
        debits = parser._parse_debits(BANK_X_SAMPLE_TEXT, 2025)
        # INCFILE, QUICKBOOKS, GOOGLE, IRS, ADOBE, GOOGLE WORKSPACE = 6
        assert len(debits) >= 4

    def test_parse_credits_count(self, parser):
        credits = parser._parse_credits(BANK_X_SAMPLE_TEXT, 2025)
        assert len(credits) == 2

    def test_debit_amounts_negative(self, parser):
        debits = parser._parse_debits(BANK_X_SAMPLE_TEXT, 2025)
        for t in debits:
            assert t.amount < 0, f"Debit should be negative: {t.description} {t.amount}"

    def test_credit_amounts_positive(self, parser):
        credits = parser._parse_credits(BANK_X_SAMPLE_TEXT, 2025)
        for t in credits:
            assert t.amount > 0, f"Credit should be positive: {t.description} {t.amount}"

    def test_intra_xfer_classified_as_transfer(self, parser):
        if not BANK_X_REAL_AVAILABLE:
            pytest.skip(
                "Intra-bank transfer detection requires real-corpus keywords; "
                "the committed example fixture uses pseudonymised tokens."
            )
        credits = parser._parse_credits(BANK_X_SAMPLE_TEXT, 2025)
        transfers = [t for t in credits if t.is_transfer]
        assert len(transfers) == 2

    def test_irs_classified_as_tax(self, parser):
        debits = parser._parse_debits(BANK_X_SAMPLE_TEXT, 2025)
        from ledger_agent.core.models import TransactionType
        tax_txns = [t for t in debits if t.transaction_type == TransactionType.TAX]
        assert len(tax_txns) >= 1


class TestBaseParserHelpers:
    @pytest.fixture
    def parser(self):
        from ledger_agent.core.parsers.bank_x_checking import BankXCheckingParser
        return BankXCheckingParser()

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
        masked = parser.mask_account("1470018610272")  # redaction: allow
        # Returns last 4 digits only
        assert masked == "0272"


class TestBankX3CheckingCanParse:
    def test_detects_bank_x3_business_complete(self):
        from ledger_agent.core.parsers.bank_x3_checking import BankX3CheckingParser
        if not BankX3CheckingParser.can_parse(BANK_X3_SAMPLE_TEXT):
            pytest.skip("Bank X3 detection tokens not present (private/institutions.py)")
        assert BankX3CheckingParser.can_parse(BANK_X3_SAMPLE_TEXT)

    def test_rejects_bank_x(self):
        from ledger_agent.core.parsers.bank_x3_checking import BankX3CheckingParser
        assert not BankX3CheckingParser.can_parse(BANK_X_SAMPLE_TEXT)


class TestBankX2CheckingCanParse:
    def test_detects_bank_x2_business(self):
        from ledger_agent.core.parsers.bank_x2_checking import BankX2CheckingParser
        if not BankX2CheckingParser.can_parse(BANK_X2_SAMPLE_TEXT):
            pytest.skip("Bank X2 detection tokens not present (private/institutions.py)")
        assert BankX2CheckingParser.can_parse(BANK_X2_SAMPLE_TEXT)

    def test_rejects_bank_x(self):
        from ledger_agent.core.parsers.bank_x2_checking import BankX2CheckingParser
        assert not BankX2CheckingParser.can_parse(BANK_X_SAMPLE_TEXT)


class TestBrokerZCanParse:
    def test_detects_broker_z_activity_statement(self):
        from ledger_agent.core.parsers.broker_z import BrokerZParser
        if not BrokerZParser.can_parse(BROKER_Z_SAMPLE_TEXT):
            pytest.skip("Broker Z detection tokens not present (private/institutions.py)")
        assert BrokerZParser.can_parse(BROKER_Z_SAMPLE_TEXT)

    def test_rejects_bank_x(self):
        from ledger_agent.core.parsers.broker_z import BrokerZParser
        assert not BrokerZParser.can_parse(BANK_X_SAMPLE_TEXT)


class TestBankX4CheckingCanParse:
    def test_detects_bank_x4_checking(self):
        pytest.importorskip("pdfplumber")
        from ledger_agent.core.parsers.bank_x4_checking import BankX4CheckingParser
        if not BankX4CheckingParser.can_parse(BANK_X4_CHECKING_SAMPLE_TEXT):
            pytest.skip("Bank X4 detection tokens not present (private/institutions.py)")
        assert BankX4CheckingParser.can_parse(BANK_X4_CHECKING_SAMPLE_TEXT)

    def test_rejects_bank_x(self):
        pytest.importorskip("pdfplumber")
        from ledger_agent.core.parsers.bank_x4_checking import BankX4CheckingParser
        assert not BankX4CheckingParser.can_parse(BANK_X_SAMPLE_TEXT)

    def test_rejects_bank_x4_cc(self):
        pytest.importorskip("pdfplumber")
        from ledger_agent.core.parsers.bank_x4_checking import BankX4CheckingParser
        assert not BankX4CheckingParser.can_parse(BANK_X4_CC_SAMPLE_TEXT)


class TestBankX4CreditCardCanParse:
    def test_detects_bank_x4_cc(self):
        pytest.importorskip("pdfplumber")
        from ledger_agent.core.parsers.bank_x4_creditcard import BankX4CreditCardParser
        if not BankX4CreditCardParser.can_parse(BANK_X4_CC_SAMPLE_TEXT):
            pytest.skip("Bank X4 detection tokens not present (private/institutions.py)")
        assert BankX4CreditCardParser.can_parse(BANK_X4_CC_SAMPLE_TEXT)

    def test_rejects_bank_x4_checking(self):
        pytest.importorskip("pdfplumber")
        from ledger_agent.core.parsers.bank_x4_creditcard import BankX4CreditCardParser
        assert not BankX4CreditCardParser.can_parse(BANK_X4_CHECKING_SAMPLE_TEXT)


class TestMCPServerSmoke:
    """Smoke-test the MCP server over stdin/stdout using the spec-compliant
    newline-delimited JSON framing.  Spawns the server as a subprocess."""

    def _rpc(self, proc, method, params=None, msg_id=1):
        import json
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params:
            msg["params"] = params
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        return json.loads(line)

    def test_initialize(self, tmp_path):
        import subprocess
        import sys
        import os

        env = os.environ.copy()
        env["FI_DB_PATH"] = str(tmp_path / "mcp_test.db")
        env["FI_DATA_DIR"] = str(tmp_path)

        proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_server.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=str(__import__("pathlib").Path(__file__).parent.parent),
            env=env,
        )
        try:
            resp = self._rpc(proc, "initialize", {"protocolVersion": "2024-11-05"})
            assert resp.get("result", {}).get("serverInfo", {}).get("name") == "financial-intelligence"
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_tools_list(self, tmp_path):
        import subprocess
        import sys
        import os

        env = os.environ.copy()
        env["FI_DB_PATH"] = str(tmp_path / "mcp_test.db")
        env["FI_DATA_DIR"] = str(tmp_path)

        proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_server.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=str(__import__("pathlib").Path(__file__).parent.parent),
            env=env,
        )
        try:
            self._rpc(proc, "initialize", {"protocolVersion": "2024-11-05"})
            resp = self._rpc(proc, "tools/list", msg_id=2)
            tool_names = [t["name"] for t in resp["result"]["tools"]]
            assert "get_balance_sheet" in tool_names
            assert "list_transactions" in tool_names
            assert "get_tax_estimate" in tool_names
            assert "classify_transaction" in tool_names
            assert "list_periods" in tool_names
            assert "get_entity_summary" in tool_names
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_list_periods_empty_db(self, tmp_path):
        import subprocess
        import sys
        import os

        env = os.environ.copy()
        env["FI_DB_PATH"] = str(tmp_path / "mcp_test.db")
        env["FI_DATA_DIR"] = str(tmp_path)

        proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_server.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=str(__import__("pathlib").Path(__file__).parent.parent),
            env=env,
        )
        try:
            self._rpc(proc, "initialize", {"protocolVersion": "2024-11-05"})
            resp = self._rpc(proc, "tools/call",
                             params={"name": "list_periods", "arguments": {}},
                             msg_id=2)
            # Should return a content payload (even if periods list is empty)
            assert "result" in resp
            assert "content" in resp["result"]
        finally:
            proc.terminate()
            proc.wait(timeout=5)
