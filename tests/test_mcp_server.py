"""
tests/test_mcp_server.py  –  Unit tests for ledger_agent/mcp/server.py

Covers:
  SMELL-M4 — structural redact walk preserves valid JSON even when a
              redacted string value contains JSON-breaking characters such as `"`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ledger_agent.mcp.server import _redact_struct


# ---------------------------------------------------------------------------
# SMELL-M4: structural walk helpers
# ---------------------------------------------------------------------------

class TestRedactStruct:
    """_redact_struct walks dicts/lists/strings recursively."""

    def _identity_redact(self, text, scope="mcp_response"):
        """Fake redact_fn that returns text unchanged (no replacements)."""
        return text, {}

    def _upper_redact(self, text, scope="mcp_response"):
        """Fake redact_fn that uppercases strings (to verify the walk fires)."""
        return text.upper(), {}

    def test_string_leaf_is_redacted(self):
        obj = "hello world"
        result = _redact_struct(obj, self._upper_redact)
        assert result == "HELLO WORLD"

    def test_dict_values_are_redacted(self):
        obj = {"key": "value", "name": "Alice"}
        result = _redact_struct(obj, self._upper_redact)
        assert result == {"key": "VALUE", "name": "ALICE"}

    def test_dict_keys_preserved(self):
        """Dict keys must not be redacted — only values."""
        obj = {"ordinary_business_income": "12345"}
        result = _redact_struct(obj, self._upper_redact)
        assert "ordinary_business_income" in result

    def test_list_items_are_redacted(self):
        obj = ["alpha", "beta", "gamma"]
        result = _redact_struct(obj, self._upper_redact)
        assert result == ["ALPHA", "BETA", "GAMMA"]

    def test_nested_structure(self):
        obj = {"a": {"b": ["x", "y"]}, "c": "z"}
        result = _redact_struct(obj, self._upper_redact)
        assert result == {"a": {"b": ["X", "Y"]}, "c": "Z"}

    def test_non_string_scalars_pass_through(self):
        obj = {"amount": 1234.56, "count": 5, "active": True, "note": None}
        result = _redact_struct(obj, self._upper_redact)
        assert result["amount"] == 1234.56
        assert result["count"] == 5
        assert result["active"] is True
        assert result["note"] is None

    def test_dict_order_preserved(self):
        """Python 3.7+ dict insertion order must be maintained."""
        keys = ["z", "a", "m", "b"]
        obj = {k: k for k in keys}
        result = _redact_struct(obj, self._identity_redact)
        assert list(result.keys()) == keys

    def test_empty_containers_unchanged(self):
        assert _redact_struct({}, self._upper_redact) == {}
        assert _redact_struct([], self._upper_redact) == []

    def test_string_with_embedded_quotes_survives(self):
        """
        SMELL-M4 core scenario: a string value containing `"` would corrupt
        JSON if processed via json.dumps → regex → json.loads.  The structural
        walk handles it natively without serialization.
        """
        problematic = 'value with "embedded" quotes and \\backslash'
        obj = {"description": problematic}

        def replace_with_quoted(text, scope="mcp_response"):
            # Simulate a redaction token that itself contains JSON-unsafe chars
            return text.replace("embedded", '"REDACTED"'), {}

        result = _redact_struct(obj, replace_with_quoted)
        # The structural walk returns a Python dict — no serialization corruption
        assert isinstance(result, dict)
        assert '"REDACTED"' in result["description"]
        # Verify it can still be serialized to valid JSON
        serialized = json.dumps(result)
        restored = json.loads(serialized)
        assert '"REDACTED"' in restored["description"]


# ---------------------------------------------------------------------------
# SMELL-M4: _redact_response integration (real privacy module)
# ---------------------------------------------------------------------------

class TestRedactResponseStructural:
    """Verify _redact_response uses the structural walk, not json.dumps→regex."""

    def test_allow_pii_returns_payload_unchanged(self):
        from ledger_agent.mcp.server import _redact_response
        payload = {"ordinary_business_income": 18732.00, "note": "clean"}
        result = _redact_response(payload, allow_pii=True)
        assert result == payload

    def test_clean_payload_passes_through(self):
        """A payload with no PII should pass through the structural walk intact."""
        from ledger_agent.mcp.server import _redact_response
        import config as _cfg
        _cfg.AI_EGRESS_MODE = "redact"
        _cfg.PRIVACY_ENTITY_NAME = ""

        payload = {
            "ordinary_business_income": 18732.00,
            "interest_income": 847.23,
            "status": "computed",
        }
        result = _redact_response(payload, allow_pii=False)
        # Numeric values must survive the structural walk unchanged
        assert result["ordinary_business_income"] == 18732.00
        assert result["interest_income"] == 847.23

    def test_nested_string_with_quotes_survives_redaction(self):
        """
        SMELL-M4: payload with a string containing `"` must not produce
        corrupted JSON after _redact_response.
        """
        from ledger_agent.mcp.server import _redact_response
        import config as _cfg
        _cfg.AI_EGRESS_MODE = "redact"
        _cfg.PRIVACY_ENTITY_NAME = ""

        payload = {
            "description": 'Transaction: "clean" description, no PII here.',
            "amount": 99.99,
        }
        result = _redact_response(payload, allow_pii=False)
        # Must still be a valid dict (not corrupted by regex+json.loads)
        assert isinstance(result, dict)
        assert result["amount"] == 99.99
        # Can be re-serialized cleanly
        assert json.loads(json.dumps(result))["amount"] == 99.99
