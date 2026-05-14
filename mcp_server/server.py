"""
mcp_server/server.py  –  MCP stdio server for FinancialIntelligence

Implements the Model Context Protocol (JSON-RPC 2.0 over stdio) without
any external dependencies. Uses MCP-spec newline-delimited JSON framing
(one JSON object per line), compatible with Claude Desktop, Cursor, Cline,
Continue, and the reference MCP Python SDK.

Exposed tools:
  get_balance_sheet       – Balance sheet for a statement period
  list_transactions       – Transactions (optionally filtered by period)
  get_tax_estimate        – Quarterly tax obligation estimate
  classify_transaction    – Classify a single transaction description
  list_periods            – Available statement periods in the database
  get_entity_summary      – Entity name, account list, period coverage

Usage:
  python -m mcp_server.server

Claude Desktop config (~/.claude/claude_desktop_config.json):
  {
    "mcpServers": {
      "financial-intelligence": {
        "command": "python",
        "args": ["-m", "mcp_server.server"],
        "cwd": "/path/to/FinancialIntelligence"
      }
    }
  }
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

SERVER_NAME = "financial-intelligence"
SERVER_VERSION = "1.0.0"

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "get_balance_sheet",
        "description": (
            "Returns the full GAAP-style balance sheet for a given statement period. "
            "Includes total assets, liabilities, members equity, and net income."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "description": "Statement period in YYYY-MM format, e.g. '2025-01'.",
                },
            },
            "required": ["period"],
        },
    },
    {
        "name": "list_transactions",
        "description": (
            "Lists all transactions for a given period, or all periods if none specified. "
            "Returns date, description, amount, COA code, and category."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "description": "Optional YYYY-MM period filter.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of transactions to return (default 100).",
                },
            },
        },
    },
    {
        "name": "get_tax_estimate",
        "description": (
            "Computes quarterly estimated tax obligations (SE tax + federal + state + QBI) "
            "based on the balance sheet net income for a given period."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "description": "Statement period in YYYY-MM format.",
                },
            },
            "required": ["period"],
        },
    },
    {
        "name": "classify_transaction",
        "description": (
            "Suggests a Chart of Accounts classification for a transaction description "
            "using the local rule engine + learned memory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Transaction description to classify.",
                },
                "amount": {
                    "type": "number",
                    "description": "Optional transaction amount (negative = debit).",
                },
            },
            "required": ["description"],
        },
    },
    {
        "name": "list_periods",
        "description": "Returns all available statement periods present in the database.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_entity_summary",
        "description": (
            "Returns entity name, registered accounts, and period coverage summary."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "privacy_status",
        "description": (
            "Returns current privacy / egress firewall status: active mode, "
            "detector category count, session tokens issued, allowlist size, "
            "and last 10 redaction audit events (token types only — no original values)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


def _bootstrap() -> None:
    """Add project root to sys.path so FI modules are importable."""
    root = Path(__file__).parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _handle_get_balance_sheet(args: Dict[str, Any]) -> Dict[str, Any]:
    period = args.get("period", "")
    if not period:
        return {"error": "period is required"}

    from ledger_agent.core.database import init_db, EntityRepo
    from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder

    init_db()
    entities = EntityRepo.list_all()
    if not entities:
        return {"error": "No entity configured. Run setup first."}

    entity = entities[0]
    bs = BalanceSheetBuilder(entity.id, period).build()

    return {
        "entity": bs.entity_name,
        "period": bs.period,
        "total_assets": float(bs.total_assets),
        "total_liabilities": float(bs.total_liabilities),
        "total_equity": float(bs.total_equity),
        "net_income": float(bs.net_income),
        "balanced": abs((bs.total_liabilities + bs.total_equity) - bs.total_assets) < 0.02,
        "lines": [
            {
                "label": line.label,
                "amount": float(line.amount) if line.amount is not None else None,
                "type": line.coa_type.value if hasattr(line.coa_type, "value") else str(line.coa_type),
                "indent": line.indent,
                "is_subtotal": line.is_subtotal,
            }
            for line in bs.lines
        ],
    }


def _handle_list_transactions(args: Dict[str, Any]) -> Dict[str, Any]:
    period: Optional[str] = args.get("period")
    limit: int = int(args.get("limit", 100))

    from ledger_agent.core.database import init_db, TransactionRepo
    init_db()

    if period:
        txns = TransactionRepo.list_for_period(period)
    else:
        txns = TransactionRepo.list_unclassified() or []
        if not txns:
            from ledger_agent.core.database import get_conn
            with get_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM transactions ORDER BY date DESC LIMIT ?", (limit,)
                ).fetchall()
                txns = rows

    # Redact transaction descriptions before returning to MCP client (R-46)
    # An MCP client may be Claude Desktop tunneled to a cloud model.
    try:
        from ledger_agent.core.privacy import redact as _redact
    except Exception:
        _redact = None  # type: ignore[assignment]

    records = []
    for t in txns[:limit]:
        if hasattr(t, "date"):
            desc = t.description
            if _redact is not None:
                try:
                    desc, _ = _redact(desc, scope="mcp_response")
                except Exception:
                    pass
            records.append({
                "date": str(t.date),
                "description": desc,
                "amount": float(t.amount),
                "type": t.transaction_type.value if hasattr(t.transaction_type, "value") else str(t.transaction_type),
                "coa_code": t.coa_code,
                "coa_name": t.coa_name,
                "period": t.statement_period,
            })
        else:
            records.append(dict(t))

    return {"transactions": records, "count": len(records)}


def _handle_get_tax_estimate(args: Dict[str, Any]) -> Dict[str, Any]:
    period = args.get("period", "")
    if not period:
        return {"error": "period is required"}

    from ledger_agent.core.database import init_db, EntityRepo
    from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
    from ledger_agent.core.accounting.tax_estimator import TaxEstimator

    init_db()
    entities = EntityRepo.list_all()
    if not entities:
        return {"error": "No entity configured."}

    entity = entities[0]
    year = int(period[:4])
    bs = BalanceSheetBuilder(entity.id, period).build()
    est = TaxEstimator(entity.name, year).estimate_from_balance_sheet(bs)

    return {
        "entity": entity.name,
        "period": period,
        "year": year,
        "net_income_annual": float(est.net_income),
        "se_tax": float(est.se_tax),
        "federal_income_tax": float(est.federal_income_tax),
        "state_income_tax": float(est.state_income_tax),
        "total_annual_tax": float(est.total_annual_tax),
        "quarterly_payment": float(est.total_annual_tax / 4),
        "effective_rate_pct": float(est.effective_rate),
        "quarterly_payments": [
            {
                "quarter": p.quarter,
                "due_date": p.due_date,
                "amount": float(p.amount),
            }
            for p in est.quarterly_payments
        ],
        "notes": est.notes
    }


def _handle_classify_transaction(args: Dict[str, Any]) -> Dict[str, Any]:
    description = args.get("description", "")
    if not description:
        return {"error": "description is required"}

    from ledger_agent.core.database import init_db
    from ledger_agent.core.intelligence.classifier import suggest_classification

    init_db()
    result = suggest_classification(description)

    return {
        "description": description,
        "coa_code": result.get("coa_code"),
        "coa_name": result.get("coa_name"),
        "confidence": result.get("confidence"),
        "source": result.get("source"),
    }


def _handle_list_periods(args: Dict[str, Any]) -> Dict[str, Any]:
    from ledger_agent.core.database import init_db, SnapshotRepo, EntityRepo
    init_db()

    entities = EntityRepo.list_all()
    if not entities:
        return {"periods": [], "error": "No entity configured."}

    entity = entities[0]
    snapshots = SnapshotRepo.list_for_entity(entity.id)
    periods = sorted({s.statement_period for s in snapshots}, reverse=True)
    return {"periods": periods, "entity": entity.name}


def _handle_get_entity_summary(args: Dict[str, Any]) -> Dict[str, Any]:
    from ledger_agent.core.database import init_db, EntityRepo, AccountRepo, SnapshotRepo
    init_db()

    entities = EntityRepo.list_all()
    if not entities:
        return {"error": "No entity configured. Run setup first."}

    entity = entities[0]
    accounts = AccountRepo.list_for_entity(entity.id)
    snapshots = SnapshotRepo.list_for_entity(entity.id)
    periods = sorted({s.statement_period for s in snapshots})

    return {
        "entity": {
            "name": entity.name,
            "type": entity.entity_type,
            "state": entity.state,
        },
        "accounts": [
            {
                "name": a.name,
                "institution": a.institution,
                "type": a.account_type.value if hasattr(a.account_type, "value") else str(a.account_type),
                "masked": a.account_number_masked,
            }
            for a in accounts
        ],
        "periods": periods,
        "period_count": len(periods),
    }


def _handle_privacy_status(_args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from ledger_agent.core.privacy import privacy_status
        return privacy_status()
    except Exception as exc:
        return {"error": str(exc)}


_HANDLERS = {
    "get_balance_sheet": _handle_get_balance_sheet,
    "list_transactions": _handle_list_transactions,
    "get_tax_estimate": _handle_get_tax_estimate,
    "classify_transaction": _handle_classify_transaction,
    "list_periods": _handle_list_periods,
    "get_entity_summary": _handle_get_entity_summary,
    "privacy_status": _handle_privacy_status,
}


def _respond(msg_id: Any, result: Any) -> None:
    payload = json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result})
    sys.stdout.write(payload + "\n")
    sys.stdout.flush()


def _error(msg_id: Any, code: int, message: str) -> None:
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    })
    sys.stdout.write(payload + "\n")
    sys.stdout.flush()


def _read_message() -> Optional[Dict[str, Any]]:
    """Read one JSON-RPC message from stdin (MCP newline-delimited JSON)."""
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    return json.loads(line)


def _dispatch(msg: Dict[str, Any]) -> None:
    msg_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params", {})

    if method == "initialize":
        _respond(msg_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
        return

    if method == "notifications/initialized":
        return

    if method == "tools/list":
        _respond(msg_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        handler = _HANDLERS.get(tool_name)
        if handler is None:
            _error(msg_id, -32601, f"Unknown tool: {tool_name}")
            return
        try:
            result = handler(tool_args)
            _respond(msg_id, {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            })
        except Exception:
            tb = traceback.format_exc()
            _respond(msg_id, {
                "content": [{"type": "text", "text": f"Error:\n{tb}"}],
                "isError": True,
            })
        return

    if method == "ping":
        _respond(msg_id, {})
        return

    if msg_id is not None:
        _error(msg_id, -32601, f"Method not found: {method}")


def main() -> None:
    _bootstrap()
    while True:
        try:
            msg = _read_message()
            if msg is None:
                break
            _dispatch(msg)
        except EOFError:
            break
        except json.JSONDecodeError:
            _error(None, -32700, "Parse error")
        except Exception:
            _error(None, -32603, traceback.format_exc())


if __name__ == "__main__":
    main()
