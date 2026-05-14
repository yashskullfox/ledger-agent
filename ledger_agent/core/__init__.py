"""
ledger_agent.core  –  Pure-Python financial intelligence library
================================================================

This sub-package contains all domain logic with zero CLI, UI, or
network dependencies.  It is the source-of-truth for all four
deployment forms (A/B/C/D).

Sub-modules
-----------
ledger_agent.core.api        Six public functions — the stable interface
ledger_agent.core.models     Dataclass models (re-exported from ledger_agent.core.models)
ledger_agent.core.db         SQLite repositories (re-exported from ledger_agent.core.database)
ledger_agent.core.parsers    PDF statement parser plugins
ledger_agent.core.accounting Balance sheet + tax estimation
ledger_agent.core.reports    Console / CSV / JSON rendering
ledger_agent.core.intelligence  Classification, memory, reconciliation
ledger_agent.core.privacy    PII egress firewall (R-46)
"""

from ledger_agent.core import api  # noqa: F401 — ensure api is importable
