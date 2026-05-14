"""
tests/test_bridge.py  –  Unit tests for ledger_agent/bridge/jsonrpc_stdio.py

Covers:
  BUG-B1 — bridge.tool_error audit must record the PRIMARY exception's
            error_type even when run_cycle.__exit__ (cycle_cleanup) raises a
            secondary exception that would otherwise replace the original.
"""
from __future__ import annotations

import sys
import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# BUG-B1: primary error_type survives a secondary from run_cycle.__exit__
# ---------------------------------------------------------------------------

class TestBugB1ErrorTypeCapture:
    """
    The fix (BUG-B1) audits bridge.tool_error with the primary exception's type
    INSIDE the `with run_cycle()` body — before run_cycle.__exit__ executes.

    If cycle_cleanup (inside __exit__) raises a secondary exception, the outer
    except clause captures that secondary, but bridge.tool_error was already
    correctly audited with the primary type.
    """

    def _make_dispatch(self):
        """Return the _dispatch function from the bridge module (fresh import)."""
        # Force re-import so module-level state is clean
        import importlib
        bridge = importlib.import_module("ledger_agent.bridge.jsonrpc_stdio")
        return bridge._dispatch

    def test_primary_error_type_audited_before_cleanup_secondary(self, monkeypatch):
        """
        Scenario:
          1. call_tool raises ValueError (the PRIMARY error).
          2. cycle_cleanup inside run_cycle.__exit__ raises OSError (SECONDARY).
          3. Assert that audit was called with error_type='ValueError', not 'OSError'.
        """
        audit_calls = []

        def fake_audit(event, **kwargs):
            audit_calls.append((event, kwargs))

        def fake_call_tool(method, params, allow_pii=False):
            raise ValueError("primary error from tool")

        # cycle_cleanup that raises a secondary exception
        def fake_cycle_cleanup(label="cycle"):
            raise OSError("secondary error from cleanup")

        # Patch at the bridge's import sites
        import ledger_agent.bridge.jsonrpc_stdio as bridge_mod
        monkeypatch.setattr(bridge_mod, "_dispatch", bridge_mod._dispatch)

        # We patch the imports that _dispatch performs via `from core.audit import audit`
        # by injecting into sys.modules
        fake_core_audit = MagicMock()
        fake_core_audit.audit = fake_audit

        fake_core_cleanup = MagicMock()

        # Patch run_cycle to be a real context manager that calls fake_cycle_cleanup on exit
        from contextlib import contextmanager

        @contextmanager
        def fake_run_cycle(label):
            try:
                yield
            except Exception as e:
                raise
            finally:
                fake_cycle_cleanup(label)

        fake_core_cleanup.run_cycle = fake_run_cycle

        # Patch the mcp.tools import
        fake_mcp_tools = MagicMock()
        fake_mcp_tools.call_tool = fake_call_tool

        with patch.dict("sys.modules", {
            "core.audit": fake_core_audit,
            "core.cleanup": fake_core_cleanup,
            "ledger_agent.mcp.tools": fake_mcp_tools,
            "ledger_agent.mcp.server": MagicMock(),
        }):
            import importlib
            import ledger_agent.bridge.jsonrpc_stdio as bridge
            importlib.reload(bridge)

            msg = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "generate_form_1065",
                "params": {"fiscal_year": 2024},
            }

            # Capture stdout to prevent actual writing
            import io
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                bridge._dispatch(msg)
            finally:
                sys.stdout = old_stdout

        # Find bridge.tool_error audit calls
        tool_error_calls = [
            kwargs for event, kwargs in audit_calls
            if event == "bridge.tool_error"
        ]
        assert tool_error_calls, (
            "BUG-B1: bridge.tool_error audit event was never emitted"
        )
        assert tool_error_calls[0]["error_type"] == "ValueError", (
            f"BUG-B1: expected error_type='ValueError' (primary), "
            f"got {tool_error_calls[0]['error_type']!r}. "
            "Secondary OSError from cycle_cleanup must not override the primary."
        )

    def test_no_double_audit_on_clean_success(self, monkeypatch):
        """
        When call_tool succeeds, bridge.tool_error must NOT be emitted.
        """
        audit_calls = []

        def fake_audit(event, **kwargs):
            audit_calls.append(event)

        fake_core_audit = MagicMock()
        fake_core_audit.audit = fake_audit

        from contextlib import contextmanager

        @contextmanager
        def fake_run_cycle(label):
            yield

        fake_core_cleanup = MagicMock()
        fake_core_cleanup.run_cycle = fake_run_cycle

        fake_mcp_tools = MagicMock()
        fake_mcp_tools.call_tool = MagicMock(return_value='{"ordinary_business_income": 1000}')

        fake_mcp_server = MagicMock()
        fake_mcp_server._redact_response = lambda payload, allow_pii=False: payload

        with patch.dict("sys.modules", {
            "core.audit": fake_core_audit,
            "core.cleanup": fake_core_cleanup,
            "ledger_agent.mcp.tools": fake_mcp_tools,
            "ledger_agent.mcp.server": fake_mcp_server,
        }):
            import importlib
            import ledger_agent.bridge.jsonrpc_stdio as bridge
            importlib.reload(bridge)

            msg = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "generate_form_1065",
                "params": {"fiscal_year": 2024},
            }

            import io
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                bridge._dispatch(msg)
            finally:
                sys.stdout = old_stdout

        assert "bridge.tool_error" not in audit_calls, (
            "bridge.tool_error must not be emitted when call_tool succeeds"
        )
