"""
tests/test_parsers.py  –  Unit tests for statement parsers
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import (
    TRUIST_SAMPLE_TEXT, FIDELITY_SAMPLE_TEXT,
    CHASE_SAMPLE_TEXT, BOFA_SAMPLE_TEXT, IBKR_SAMPLE_TEXT,
    USBANK_CHECKING_SAMPLE_TEXT, USBANK_CC_SAMPLE_TEXT,
)


class TestParserRegistry:
    def test_truist_detected(self):
        import parsers  # noqa: F401 — triggers auto-discovery
        from parsers.registry import ParserRegistry
        cls = ParserRegistry.detect(TRUIST_SAMPLE_TEXT)
        assert cls is not None
        assert cls.PARSER_ID == "truist_checking"

    def test_fidelity_detected(self):
        import parsers  # noqa: F401
        from parsers.registry import ParserRegistry
        cls = ParserRegistry.detect(FIDELITY_SAMPLE_TEXT)
        assert cls is not None
        assert cls.PARSER_ID == "fidelity_brokerage"

    def test_chase_detected(self):
        import parsers  # noqa: F401
        from parsers.registry import ParserRegistry
        cls = ParserRegistry.detect(CHASE_SAMPLE_TEXT)
        assert cls is not None
        assert cls.PARSER_ID == "chase_checking"

    def test_bofa_detected(self):
        import parsers  # noqa: F401
        from parsers.registry import ParserRegistry
        cls = ParserRegistry.detect(BOFA_SAMPLE_TEXT)
        assert cls is not None
        assert cls.PARSER_ID == "bofa_checking"

    def test_ibkr_detected(self):
        import parsers  # noqa: F401
        from parsers.registry import ParserRegistry
        cls = ParserRegistry.detect(IBKR_SAMPLE_TEXT)
        assert cls is not None
        assert cls.PARSER_ID == "ibkr"

    def test_usbank_checking_detected(self):
        pdfplumber = pytest.importorskip("pdfplumber")  # noqa: F841
        import parsers  # noqa: F401
        from parsers.registry import ParserRegistry
        cls = ParserRegistry.detect(USBANK_CHECKING_SAMPLE_TEXT)
        assert cls is not None
        assert cls.PARSER_ID == "usbank_checking"

    def test_usbank_cc_detected(self):
        pdfplumber = pytest.importorskip("pdfplumber")  # noqa: F841
        import parsers  # noqa: F401
        from parsers.registry import ParserRegistry
        cls = ParserRegistry.detect(USBANK_CC_SAMPLE_TEXT)
        assert cls is not None
        assert cls.PARSER_ID == "usbank_creditcard"

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
        assert "0000000000001" in acct

    def test_extract_entity_name(self, parser):
        name = parser._extract_entity_name(TRUIST_SAMPLE_TEXT)
        assert "SAMPLE ENTITY LLC" in name

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
        masked = parser.mask_account("0000000000001")
        # Returns last 4 digits only
        assert masked == "0001"


class TestChaseCheckingCanParse:
    def test_detects_chase_business_complete(self):
        from parsers.chase_checking import ChaseCheckingParser
        assert ChaseCheckingParser.can_parse(CHASE_SAMPLE_TEXT)

    def test_rejects_truist(self):
        from parsers.chase_checking import ChaseCheckingParser
        assert not ChaseCheckingParser.can_parse(TRUIST_SAMPLE_TEXT)


class TestBofACheckingCanParse:
    def test_detects_bofa_business(self):
        from parsers.bofa_checking import BofACheckingParser
        assert BofACheckingParser.can_parse(BOFA_SAMPLE_TEXT)

    def test_rejects_truist(self):
        from parsers.bofa_checking import BofACheckingParser
        assert not BofACheckingParser.can_parse(TRUIST_SAMPLE_TEXT)


class TestIBKRCanParse:
    def test_detects_ibkr_activity_statement(self):
        from parsers.ibkr import IBKRParser
        assert IBKRParser.can_parse(IBKR_SAMPLE_TEXT)

    def test_rejects_truist(self):
        from parsers.ibkr import IBKRParser
        assert not IBKRParser.can_parse(TRUIST_SAMPLE_TEXT)


class TestUSBankCheckingCanParse:
    def test_detects_usbank_checking(self):
        pytest.importorskip("pdfplumber")
        from parsers.usbank_checking import USBankCheckingParser
        assert USBankCheckingParser.can_parse(USBANK_CHECKING_SAMPLE_TEXT)

    def test_rejects_truist(self):
        pytest.importorskip("pdfplumber")
        from parsers.usbank_checking import USBankCheckingParser
        assert not USBankCheckingParser.can_parse(TRUIST_SAMPLE_TEXT)

    def test_rejects_usbank_cc(self):
        pytest.importorskip("pdfplumber")
        from parsers.usbank_checking import USBankCheckingParser
        assert not USBankCheckingParser.can_parse(USBANK_CC_SAMPLE_TEXT)


class TestUSBankCreditCardCanParse:
    def test_detects_usbank_cc(self):
        pytest.importorskip("pdfplumber")
        from parsers.usbank_creditcard import USBankCreditCardParser
        assert USBankCreditCardParser.can_parse(USBANK_CC_SAMPLE_TEXT)

    def test_rejects_usbank_checking(self):
        pytest.importorskip("pdfplumber")
        from parsers.usbank_creditcard import USBankCreditCardParser
        assert not USBankCreditCardParser.can_parse(USBANK_CHECKING_SAMPLE_TEXT)


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
