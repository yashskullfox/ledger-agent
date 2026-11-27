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
  privacy_status          – Egress firewall status

Privacy (P-02): every response is redacted exactly like the new
``ledger_agent.mcp`` server (see _privatize below) — entity, account and
institution names and masked account numbers are tokenised, transaction
descriptions are regex-redacted, input descriptions are never echoed, and
errors carry only an opaque ``ref:`` id.  Redaction failure withholds the
response (fail closed).  There is no allow_pii opt-in here.

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
import logging
import secrets
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SERVER_NAME = "financial-intelligence"
SERVER_VERSION = "1.0.0"

TOOLS: list[dict[str, Any]] = [
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
            "Returns date, redacted description, amount, COA code, and category."
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
            "Computes the pass-through entity's estimated tax for the fiscal year of the "
            "given period (same figures as ledger_agent.core.api.pte_estimate): annual "
            "tax, effective rate, and the quarterly payment schedule.  Not tax advice."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "description": "Statement period in YYYY-MM format; its year is used.",
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


# ── Privacy (P-02) ────────────────────────────────────────────────────────────
#
# Every tool result passes through _privatize() before it is written to stdout:
#   1. ledger_agent.mcp.server._redact_response — API-key guard + regex redaction
#      of every string value (scope "mcp_response"), exactly as the new server.
#   2. ledger_agent.mcp.tools._sanitise_pii_fields — field-name redaction
#      (entity_name / ein / partner_name), with this server's legacy key
#      spellings mapped onto those field names (_LEGACY_FIELD_ALIASES).
# Account and institution names and masked account numbers are replaced with
# stable tokens by the handlers themselves (_account_tokens), because only they
# know which labels refer to which account.  Any failure in this pipeline blocks
# the response (fail closed); there is no allow_pii opt-in on this server.

# Legacy key path (dict keys from the root, list levels skipped) → tools.py field name.
_LEGACY_FIELD_ALIASES: dict[tuple[str, ...], str] = {
    ("entity",): "entity_name",          # get_balance_sheet / get_tax_estimate / list_periods
    ("entity", "name"): "entity_name",   # get_entity_summary
}

# Balance-sheet line codes whose label is built from institution + account name.
_ACCOUNT_LINE_CODES = frozenset({"1010", "1100_acct", "2030"})
_ACCOUNT_FALLBACK = "<ACCOUNT>"
_MASKED_TOKEN = "<ACCT_MASKED>"


class LegacyRedactionError(RuntimeError):
    """The response could not be redacted; it must not be sent."""


def _sanitise_legacy_fields(obj: Any, path: tuple[str, ...] = ()) -> Any:
    from ledger_agent.mcp.tools import _sanitise_pii_fields

    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            alias = _LEGACY_FIELD_ALIASES.get((*path, key))
            if alias and isinstance(value, str):
                out[key] = _sanitise_pii_fields({alias: value})[alias]
            else:
                out[key] = _sanitise_legacy_fields(value, (*path, key))
        return _sanitise_pii_fields(out)
    if isinstance(obj, list):
        return [_sanitise_legacy_fields(item, path) for item in obj]
    return obj


def _privatize(result: dict[str, Any]) -> dict[str, Any]:
    """Redact a tool result for egress; raise LegacyRedactionError on any failure."""
    try:
        from ledger_agent.mcp.server import _redact_response

        return _sanitise_legacy_fields(_redact_response(result, allow_pii=False))
    except Exception as exc:
        raise LegacyRedactionError(type(exc).__name__) from exc


def _ref_id() -> str:
    return secrets.token_hex(4)


def _account_tokens(entity_id: str) -> dict[str, tuple[str, str]]:
    """account_id → (account token, institution token), stable for one DB state."""
    from ledger_agent.core.database import AccountRepo

    accounts = sorted(AccountRepo.list_for_entity(entity_id), key=lambda a: a.id)
    institutions = sorted({a.institution for a in accounts})
    inst_token = {name: f"<INSTITUTION_{i}>" for i, name in enumerate(institutions, 1)}
    return {
        a.id: (f"<ACCOUNT_{i}>", inst_token[a.institution])
        for i, a in enumerate(accounts, 1)
    }


def _account_label_map(entity_id: str) -> dict[str, str]:
    """Raw balance-sheet account labels (see BalanceSheetBuilder) → tokenised labels."""
    from ledger_agent.core.database import AccountRepo

    tokens = _account_tokens(entity_id)
    labels: dict[str, str] = {}
    for a in AccountRepo.list_for_entity(entity_id):
        acct_tok, inst_tok = tokens[a.id]
        labels[f"{a.institution} – {a.name}"] = f"{inst_tok} – {acct_tok}"
        labels[f"Credit Card Payable – {a.institution} ({a.name})"] = (
            f"Credit Card Payable – {inst_tok} ({acct_tok})"
        )
    return labels


# ── Tool handlers ─────────────────────────────────────────────────────────────

def _handle_get_balance_sheet(args: dict[str, Any]) -> dict[str, Any]:
    period = args.get("period", "")
    if not period:
        return {"error": "period is required"}

    from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
    from ledger_agent.core.database import EntityRepo, init_db

    init_db()
    entities = EntityRepo.list_all()
    if not entities:
        return {"error": "No entity configured. Run setup first."}

    entity = entities[0]
    bs = BalanceSheetBuilder(entity.id, period).build()
    label_map = _account_label_map(entity.id)

    def _label(line: Any) -> str:
        if line.label in label_map:
            return label_map[line.label]
        if line.coa_code in _ACCOUNT_LINE_CODES:
            return _ACCOUNT_FALLBACK  # unknown account label shape → never pass it through
        return line.label

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
                "label": _label(line),
                "amount": float(line.amount) if line.amount is not None else None,
                "type": line.coa_type.value if hasattr(line.coa_type, "value")
                else str(line.coa_type),
                "indent": line.indent,
                "is_subtotal": line.is_subtotal,
            }
            for line in bs.lines
        ],
    }


def _txn_record(t: Any) -> dict[str, Any]:
    """Whitelisted transaction fields only — never raw_description or raw rows.

    The description is redacted centrally by _privatize()."""
    ttype = t.transaction_type
    return {
        "date": str(t.date),
        "description": t.description,
        "amount": float(t.amount),
        "type": ttype.value if hasattr(ttype, "value") else str(ttype),
        "coa_code": t.coa_code,
        "coa_name": t.coa_name,
        "period": t.statement_period,
    }


def _handle_list_transactions(args: dict[str, Any]) -> dict[str, Any]:
    period: str | None = args.get("period")
    limit = max(0, int(args.get("limit", 100)))

    from ledger_agent.core.database import TransactionRepo, get_conn, init_db
    init_db()

    if period:
        txns = TransactionRepo.list_for_period(period)
    else:
        txns = TransactionRepo.list_unclassified()
        if not txns:
            with get_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM transactions ORDER BY date DESC LIMIT ?", (limit,)
                ).fetchall()
            txns = [TransactionRepo._row_to_model(r) for r in rows]

    records = [_txn_record(t) for t in txns[:limit]]
    return {"transactions": records, "count": len(records)}


def _handle_get_tax_estimate(args: dict[str, Any]) -> dict[str, Any]:
    period = args.get("period", "")
    if not period:
        return {"error": "period is required"}
    year = int(str(period)[:4])

    from ledger_agent.core import api
    from ledger_agent.core.database import EntityRepo, init_db

    init_db()
    entities = EntityRepo.list_all()
    if not entities:
        return {"error": "No entity configured."}
    entity = entities[0]

    try:
        est = api.pte_estimate(year)
    except ValueError:
        log.debug("legacy get_tax_estimate: no estimate for %s", year, exc_info=True)
        return {"error": f"No tax estimate for fiscal year {year}: not enough statement data."}

    tokens = _account_tokens(entity.id)
    missing = {
        tokens.get(acct_id, (_ACCOUNT_FALLBACK, ""))[0]: months
        for acct_id, months in (est.missing_statements or {}).items()
    }
    return {
        "entity": entity.name,
        "period": period,
        "year": year,
        "basis": est.basis,
        "through_period": est.through_period,
        "months_covered": est.months_covered,
        "net_income_annual": float(est.net_income),
        "ytd_net_income": float(est.ytd_net_income),
        "total_annual_tax": float(est.total_annual_tax),
        "quarterly_payment": float(est.quarterly_payment),
        "effective_rate_pct": float(est.effective_rate),
        "paid_ytd": float(est.paid_ytd),
        "remaining_balance": float(est.remaining_balance),
        "required_to_date": float(est.required_to_date),
        "quarterly_payments": [dict(p) for p in est.quarterly_payments],
        "missing_statements": missing,
        "notes": est.notes,
        "disclaimer": est.disclaimer,
    }


def _handle_classify_transaction(args: dict[str, Any]) -> dict[str, Any]:
    description = args.get("description", "")
    if not description:
        return {"error": "description is required"}

    from ledger_agent.core.database import init_db
    from ledger_agent.core.intelligence.classifier import suggest_classification

    init_db()
    result = suggest_classification(description)

    # The input description is deliberately NOT echoed back (P-02).
    return {
        "coa_code": result.get("coa_code"),
        "coa_name": result.get("coa_name"),
        "confidence": result.get("confidence"),
        "source": result.get("source"),
    }


def _handle_list_periods(args: dict[str, Any]) -> dict[str, Any]:
    from ledger_agent.core.database import EntityRepo, SnapshotRepo, init_db
    init_db()

    entities = EntityRepo.list_all()
    if not entities:
        return {"periods": [], "error": "No entity configured."}

    entity = entities[0]
    snapshots = SnapshotRepo.list_for_entity(entity.id)
    periods = sorted({s.statement_period for s in snapshots}, reverse=True)
    return {"periods": periods, "entity": entity.name}


def _handle_get_entity_summary(args: dict[str, Any]) -> dict[str, Any]:
    from ledger_agent.core.database import AccountRepo, EntityRepo, SnapshotRepo, init_db
    init_db()

    entities = EntityRepo.list_all()
    if not entities:
        return {"error": "No entity configured. Run setup first."}

    entity = entities[0]
    accounts = AccountRepo.list_for_entity(entity.id)
    tokens = _account_tokens(entity.id)
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
                "name": tokens[a.id][0],
                "institution": tokens[a.id][1],
                "type": a.account_type.value if hasattr(a.account_type, "value")
                else str(a.account_type),
                "masked": _MASKED_TOKEN if a.account_number_masked else "",
            }
            for a in sorted(accounts, key=lambda a: a.id)
        ],
        "periods": periods,
        "period_count": len(periods),
    }


def _handle_privacy_status(_args: dict[str, Any]) -> dict[str, Any]:
    from ledger_agent.core.privacy import privacy_status
    return privacy_status()


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


def _read_message() -> dict[str, Any] | None:
    """Read one JSON-RPC message from stdin (MCP newline-delimited JSON)."""
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    return json.loads(line)


def _call_tool(msg_id: Any, tool_name: str, handler: Any, tool_args: dict[str, Any]) -> None:
    """Run one tool and write a redacted response — or an opaque error, never raw data."""
    try:
        result = handler(tool_args)
    except Exception as exc:
        ref = _ref_id()
        log.warning("legacy MCP tool %r failed (ref:%s, %s)", tool_name, ref,
                    type(exc).__name__)
        log.debug("legacy MCP tool %r failed (ref:%s)", tool_name, ref, exc_info=True)
        _respond(msg_id, {
            "content": [{"type": "text",
                         "text": f"Internal error (ref:{ref}) — see server logs"}],
            "isError": True,
        })
        return
    try:
        redacted = _privatize(result)
    except LegacyRedactionError as exc:
        ref = _ref_id()
        log.error("legacy MCP tool %r: redaction failed, response withheld (ref:%s, %s)",
                  tool_name, ref, exc)
        log.debug("legacy MCP redaction failure (ref:%s)", ref, exc_info=True)
        _error(msg_id, -32000, f"redaction_failed (ref:{ref})")
        return
    _respond(msg_id, {
        "content": [{"type": "text", "text": json.dumps(redacted, indent=2)}],
    })


def _dispatch(msg: dict[str, Any]) -> None:
    msg_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

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
        tool_args = params.get("arguments") or {}
        handler = _HANDLERS.get(tool_name)
        if handler is None:
            _error(msg_id, -32601, f"Unknown tool: {tool_name}")
            return
        _call_tool(msg_id, tool_name, handler, tool_args)
        return

    if method == "ping":
        _respond(msg_id, {})
        return

    if msg_id is not None:
        _error(msg_id, -32601, f"Method not found: {method}")


def main() -> None:
    _bootstrap()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
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
            ref = _ref_id()
            log.debug("legacy MCP dispatch failed (ref:%s)", ref, exc_info=True)
            _error(None, -32603, f"Internal error (ref:{ref})")


if __name__ == "__main__":
    main()
