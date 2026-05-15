from __future__ import annotations

import dataclasses
import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _serialise(obj: Any) -> Any:
    import enum
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _serialise(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, list):
        return [_serialise(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    if hasattr(obj, "__dict__") and not callable(obj):
        return {k: _serialise(v) for k, v in obj.__dict__.items()}
    return obj


def _sanitise_pii_fields(data: Any) -> Any:
    """Replace known structural PII fields with opaque tokens before egress.

    Acts as a first-pass layer over the regex-based R-46 firewall.
    Covers fields that the regex engine cannot reliably catch because
    their values do not follow a detectable pattern (e.g. masked EINs
    containing asterisks, or proper names without a preceding keyword).
    """
    if isinstance(data, dict):
        out: dict = {}
        for k, v in data.items():
            if k in ("entity_name", "ein", "ein_masked") and isinstance(v, str) and v:
                out[k] = "<ENTITY>"
            elif k == "partner_name" and isinstance(v, str) and v:
                out[k] = "<PARTNER>"
            else:
                out[k] = _sanitise_pii_fields(v)
        return out
    if isinstance(data, list):
        return [_sanitise_pii_fields(i) for i in data]
    return data


def _ok(data: Any, *, allow_pii: bool = False) -> str:
    serialised = _serialise(data)
    if not allow_pii:
        serialised = _sanitise_pii_fields(serialised)
    return json.dumps(serialised, indent=2, default=str)


TOOL_SCHEMAS: list[dict] = [
    {
        "name": "import_statements",
        "description": (
            "Scan a folder for PDF bank/brokerage statements, parse them, and persist "
            "all transactions into the ledger database.  Idempotent — re-importing the "
            "same file is a no-op.  Returns counts of imported, skipped, and failed files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "Absolute or home-relative path to the statements folder.",
                },
                "allow_partial": {
                    "type": "boolean",
                    "description": (
                        "If true, skip the R-45 12-month completeness gate warning. "
                        "Useful for mid-year imports.  Default: false."
                    ),
                    "default": False,
                },
            },
            "required": ["folder"],
        },
    },
    {
        "name": "generate_balance_sheet",
        "description": (
            "Build a GAAP-style balance sheet for the last available period in the "
            "given fiscal year.  Returns total assets, liabilities, members equity, "
            "net income, and a full line-item breakdown."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "fiscal_year": {
                    "type": "integer",
                    "description": "Four-digit fiscal year (e.g. 2024).",
                },
            },
            "required": ["fiscal_year"],
        },
    },
    {
        "name": "generate_form_1065",
        "description": (
            "Produce Form 1065 partnership return data for the given fiscal year. "
            "Aggregates ordinary business income/loss and Schedule K items "
            "(capital gains, dividends, interest).  Not a substitute for a "
            "professionally prepared return."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "fiscal_year": {
                    "type": "integer",
                    "description": "Four-digit fiscal year (e.g. 2024).",
                },
            },
            "required": ["fiscal_year"],
        },
    },
    {
        "name": "generate_k1",
        "description": (
            "Generate Schedule K-1 for a single partner. "
            "Partner IDs are canonical slugs configured on the entity (see api.PARTNERS). "
            "Returns the partner's distributive share of income, gains, and deductions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "fiscal_year": {
                    "type": "integer",
                    "description": "Four-digit fiscal year (e.g. 2024).",
                },
                "partner_id": {
                    "type": "string",
                    "description": "Canonical partner identifier slug (see api.PARTNERS keys).",
                    "enum": ["partner_1", "partner_2"],
                },
            },
            "required": ["fiscal_year", "partner_id"],
        },
    },
    {
        "name": "pte_estimate",
        "description": (
            "Compute quarterly estimated tax payments for the pass-through entity "
            "for the given fiscal year.  Returns net income, total annual tax, "
            "effective rate, and a quarterly payment schedule with due dates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "fiscal_year": {
                    "type": "integer",
                    "description": "Four-digit fiscal year (e.g. 2024).",
                },
            },
            "required": ["fiscal_year"],
        },
    },
    {
        "name": "reconcile_year",
        "description": (
            "Run inter-account transfer reconciliation for the fiscal year. "
            "Matches INTRA_BANK_XFER / Zelle / wire transfers between accounts "
            "and flags unmatched movements.  Returns matched/unmatched counts "
            "and a list of issues."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "fiscal_year": {
                    "type": "integer",
                    "description": "Four-digit fiscal year (e.g. 2024).",
                },
            },
            "required": ["fiscal_year"],
        },
    },
]


def call_tool(name: str, arguments: dict, *, allow_pii: bool = False) -> str:
    import ledger_agent.core.api as api

    if name == "import_statements":
        folder = Path(arguments["folder"]).expanduser().resolve()
        allow_partial = bool(arguments.get("allow_partial", False))
        result = api.import_statements(folder, allow_partial=allow_partial)
        return _ok(result, allow_pii=allow_pii)

    if name == "generate_balance_sheet":
        year = int(arguments["fiscal_year"])
        bs = api.generate_balance_sheet(year)
        return _ok(bs, allow_pii=allow_pii)

    if name == "generate_form_1065":
        year = int(arguments["fiscal_year"])
        f = api.generate_form_1065(year)
        return _ok(f, allow_pii=allow_pii)

    if name == "generate_k1":
        year = int(arguments["fiscal_year"])
        partner = str(arguments["partner_id"])
        k1 = api.generate_k1(year, partner)
        return _ok(k1, allow_pii=allow_pii)

    if name == "pte_estimate":
        year = int(arguments["fiscal_year"])
        est = api.pte_estimate(year)
        return _ok(est, allow_pii=allow_pii)

    if name == "reconcile_year":
        year = int(arguments["fiscal_year"])
        r = api.reconcile_year(year)
        return _ok(r, allow_pii=allow_pii)

    raise ValueError(f"Unknown tool: {name!r}")
