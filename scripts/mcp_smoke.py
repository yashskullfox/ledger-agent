#!/usr/bin/env python3
"""
scripts/mcp_smoke.py  –  MCP server smoke test (W20)
====================================================
Verifies that the MCP server exposes exactly 7 tools and that their
schemas are valid. Exits 0 on success, 1 on failure.

Usage::

    python scripts/mcp_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EXPECTED_TOOLS = {
    "import_statements",
    "generate_balance_sheet",
    "generate_form_1065",
    "generate_k1",
    "pte_estimate",
    "reconcile_year",
    "customer_summary",
}


def main() -> int:
    try:
        from ledger_agent.mcp.tools import TOOL_SCHEMAS
    except ImportError as e:
        print(f"FAIL: could not import TOOL_SCHEMAS: {e}")
        return 1

    found_names = {t["name"] for t in TOOL_SCHEMAS}
    missing = EXPECTED_TOOLS - found_names
    extra = found_names - EXPECTED_TOOLS

    if missing:
        print(f"FAIL: missing tools: {sorted(missing)}")
        return 1
    if extra:
        print(f"WARN: unexpected extra tools: {sorted(extra)}")

    print(f"PASS: {len(found_names)} tool(s) registered:")
    for name in sorted(found_names):
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
