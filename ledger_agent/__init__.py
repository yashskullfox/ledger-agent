"""
ledger_agent  –  partnership financial intelligence engine
==========================================================

Four deployment forms, one source tree:
  A. ledger_agent.core   — importable pure-Python library (this package)
  B. ledger CLI          — local command-line runner  (ledger_agent.cli)
  C. MCP server          — spec-compliant stdio + HTTP (ledger_agent.mcp)
  D. Spring Boot webapp  — Java fat-jar wrapping Form A via JSON-RPC bridge

All financial logic lives in ledger_agent.core.  Forms B/C/D are thin
wrappers that never reimplement parsing, classification, or accounting.

Public surface
--------------
>>> import ledger_agent.core.api as api
>>> report = api.import_statements(Path("~/statements").expanduser())
>>> bs     = api.generate_balance_sheet(2024)
>>> f1065  = api.generate_form_1065(2024)
"""

__version__ = "2.1.0"
__all__ = ["__version__"]
