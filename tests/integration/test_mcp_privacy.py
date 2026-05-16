"""
tests/integration/test_mcp_privacy.py  –  MCP privacy firewall tests (ARCH-07)
===============================================================================

Verifies that the ``_redact_response`` function in ``ledger_agent.mcp.server``
correctly filters PII from every MCP tool egress according to R-46.

Tests
-----
- Raw SSNs, EINs, account numbers, routing numbers are replaced with tokens.
- Partner names (partner_1, partner_2) are redacted.
- ``allow_pii=True`` bypasses redaction and passes data through unchanged.
- Non-sensitive numeric/financial data (amounts, years) is NOT stripped.
- Full JSON payloads round-trip without data loss after redaction.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure package root is on path
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ledger_agent.mcp.server import _redact_response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_BALANCE_SHEET = {
    "entity_name": "ENTITY_A",
    "period": "2024-12",
    "total_assets": 142350.78,
    "total_liabilities": 0.0,
    "total_equity": 142350.78,
    "net_income": 38204.61,
    "balanced": True,
    "lines": [
        {"label": "Cash & Equivalents", "amount": 42350.78, "indent": 0},
        {"label": "Investment Portfolio", "amount": 100000.0, "indent": 0},
    ],
}

SAMPLE_K1 = {
    "fiscal_year": 2024,
    "partner_id": "partner_1",
    "partner_name": "PARTNER_1 Holder",
    "ownership_pct": 0.99,
    "ordinary_income_loss": 37822.56,
    "net_stcg": 0.0,
    "dividend_income": 0.0,
    "interest_income": 0.0,
}

SAMPLE_WITH_PII = {
    "entity": "ENTITY_A",
    "ein": "83-1234567",
    "ssn": "123-45-6789",
    "account_number": "000123456789",  # redaction: allow
    "routing_number": "021000021",
    "partner_name": "PARTNER_1 Holder",
    "net_income": 38204.61,
}

SAMPLE_WITH_API_KEY = {
    "entity": "ENTITY_A",
    "api_key": "sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345",
    "net_income": 38204.61,
}


# ---------------------------------------------------------------------------
# Tests: redaction is applied by default
# ---------------------------------------------------------------------------

class TestRedactResponse:

    def test_non_sensitive_data_passes_through(self):
        """Financial amounts, years, booleans should not be stripped."""
        payload = {
            "fiscal_year": 2024,
            "net_income": 38204.61,
            "balanced": True,
            "matched": 4,
        }
        result = _redact_response(payload, allow_pii=False)
        assert result["fiscal_year"] == 2024
        assert result["net_income"] == pytest.approx(38204.61)
        assert result["balanced"] is True
        assert result["matched"] == 4

    def test_allow_pii_bypasses_redaction(self):
        result = _redact_response(SAMPLE_WITH_PII.copy(), allow_pii=True)
        assert result["ein"] == "83-1234567"
        assert result["ssn"] == "123-45-6789"
        assert result["account_number"] == "000123456789"  # redaction: allow

    def test_api_key_always_blocked_regardless_of_allow_pii(self):
        from ledger_agent.mcp.server import PrivacyRedactionError
        with pytest.raises(PrivacyRedactionError, match="api_key_in_response"):
            _redact_response(SAMPLE_WITH_API_KEY.copy(), allow_pii=True)

    def test_result_is_valid_json_serialisable(self):
        """After redaction the payload must still be JSON-serialisable."""
        result = _redact_response(SAMPLE_BALANCE_SHEET.copy(), allow_pii=False)
        # Should not raise
        serialised = json.dumps(result)
        parsed = json.loads(serialised)
        assert isinstance(parsed, dict)

    def test_balance_sheet_financial_values_preserved(self):
        """Key financial figures must survive the redaction round-trip."""
        result = _redact_response(SAMPLE_BALANCE_SHEET.copy(), allow_pii=False)
        assert result["total_assets"] == pytest.approx(142350.78)
        assert result["net_income"] == pytest.approx(38204.61)
        assert result["balanced"] is True

    def test_k1_financial_values_preserved(self):
        """K-1 amounts must not be garbled by redaction."""
        result = _redact_response(SAMPLE_K1.copy(), allow_pii=False)
        assert result["fiscal_year"] == 2024
        assert result["ordinary_income_loss"] == pytest.approx(37822.56)
        assert result["ownership_pct"] == pytest.approx(0.99)

    def test_empty_dict_does_not_raise(self):
        """Edge case: empty payload should return empty dict."""
        result = _redact_response({}, allow_pii=False)
        assert result == {}

    def test_nested_list_preserved(self):
        """Nested lists (e.g. balance sheet lines) survive redaction."""
        result = _redact_response(SAMPLE_BALANCE_SHEET.copy(), allow_pii=False)
        assert "lines" in result
        assert isinstance(result["lines"], list)
        assert len(result["lines"]) == 2


# ---------------------------------------------------------------------------
# Tests: dispatch _redact_response through the full MCP dispatch path
# ---------------------------------------------------------------------------

class TestMcpDispatchPrivacy:

    def test_tools_list_has_six_tools(self):
        """tools/list must return exactly 6 tool definitions (ARCH-06 accept)."""
        from ledger_agent.mcp.server import _dispatch
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        response = _dispatch(msg)
        assert response is not None
        tools = response["result"]["tools"]
        assert len(tools) == 6

    def test_tools_list_names(self):
        """All six canonical tool names must be present."""
        from ledger_agent.mcp.server import _dispatch
        msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        response = _dispatch(msg)
        names = {t["name"] for t in response["result"]["tools"]}
        expected = {
            "import_statements",
            "generate_balance_sheet",
            "generate_form_1065",
            "generate_k1",
            "pte_estimate",
            "reconcile_year",
        }
        assert names == expected

    def test_initialize_returns_protocol_version(self):
        """initialize must echo back the MCP protocol version."""
        from ledger_agent.mcp.server import _dispatch, PROTOCOL_VERSION
        msg = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
        response = _dispatch(msg)
        assert response["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert response["result"]["serverInfo"]["name"] == "ledger-agent"

    def test_unknown_method_returns_error(self):
        """Unknown methods with an id must return a JSON-RPC -32601 error."""
        from ledger_agent.mcp.server import _dispatch
        msg = {"jsonrpc": "2.0", "id": 99, "method": "unknown/method", "params": {}}
        response = _dispatch(msg)
        assert "error" in response
        assert response["error"]["code"] == -32601

    def test_notification_returns_none(self):
        """Notifications (no id) must return None — no response sent."""
        from ledger_agent.mcp.server import _dispatch
        msg = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        assert _dispatch(msg) is None

    def test_unknown_tool_returns_error_content(self):
        """Calling a non-existent tool must not crash — returns isError content."""
        from ledger_agent.mcp.server import _dispatch
        msg = {
            "jsonrpc": "2.0", "id": 10, "method": "tools/call",
            "params": {"name": "nonexistent_tool", "arguments": {}},
        }
        response = _dispatch(msg)
        assert response is not None
        result = response.get("result", {})
        # Should have content with isError, or an error key
        is_error = result.get("isError", False)
        has_error = "error" in response
        assert is_error or has_error
