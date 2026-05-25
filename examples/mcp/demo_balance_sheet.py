#!/usr/bin/env python3
"""
examples/mcp/demo_balance_sheet.py
───────────────────────────────────
Demonstrates a balance-sheet round-trip using the MCP tool schemas directly
(without a live MCP server connection — calls the Python API directly).

This is the simplest way to test the full pipeline without a real MCP client.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        from ledger_agent.core.api import build_customer_summary
        from ledger_agent.core.database import init_db, EntityRepo
        init_db()
        entities = EntityRepo.list_all()
        if not entities:
            print("No entities found — run import_statements() first.")
            return 1
        # Use the most recent year with data
        from ledger_agent.core.database import get_conn
        with get_conn() as conn:
            row = conn.execute(
                "SELECT SUBSTR(statement_period,1,4) as yr, COUNT(*) "
                "FROM transactions GROUP BY yr ORDER BY yr DESC LIMIT 1"
            ).fetchone()
        if not row:
            print("No transaction data found.")
            return 1
        year = int(row[0])
        print(f"Building customer summary for year {year} ...")
        summary = build_customer_summary(year)
        output = {
            "fiscal_year": summary.fiscal_year,
            "period_covered": summary.period_covered,
            "profit_or_loss": summary.profit_or_loss,
            "balance_sheet_health": summary.balance_sheet_health,
            "confidence_flags": summary.confidence_flags,
        }
        print(json.dumps(output, indent=2))
        return 0
    except Exception as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
