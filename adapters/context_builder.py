"""
adapters/context_builder.py  –  AI-consumable context builder
───────────────────────────────────────────────────────────────
Packages financial data from the database into structured JSON
that can be passed to any LLM (Perplexity, Claude, GPT-4, etc.)
for analysis, Q&A, or further intelligence.

Usage:
    from adapters.context_builder import build_context, save_context
    ctx = build_context(entity_id, period="2025-01")
    save_context(ctx, Path("context_2025-01.json"))

The JSON is also what the GitHub/AI consumption mode uses:
    "Pass this file to any AI: https://github.com/your-org/fi/blob/main/context.json"

Privacy (P-06) — the export is egress, so it fails closed:
  * The entity name is never exported; it is the fixed placeholder ``<ENTITY_NAME>``.
  * Account and institution names are tokenised exactly like the legacy MCP server
    (``mcp_server.server._account_tokens`` / ``_account_label_map``):
    ``<INSTITUTION_n> – <ACCOUNT_n>``.  An account-built balance-sheet line whose
    label shape is unknown becomes ``<ACCOUNT>``.  Ticker symbols are left as-is
    (owner decision D6 pending).
  * Every other free-text field (descriptions, line labels, ``ai_instructions``,
    entity type/state) goes through ``privacy.redact(scope="ai_context")``.  If
    redaction raises, the field becomes ``<REDACTED>`` — never the raw text.
  * ``build_context``, ``save_context`` and ``context_to_prompt`` run
    ``privacy.audit_egress`` on their output; a failure raises
    ``ContextExportBlocked`` (categories only, no values) and nothing is written.
  * Figures (balances, tax numbers) are never passed through redaction.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from ledger_agent.core import privacy

ENTITY_PLACEHOLDER = "<ENTITY_NAME>"
REDACTED = "<REDACTED>"


class ContextExportBlocked(privacy.PrivacyLeakError):
    """The AI context failed the egress audit; it must not be returned, written or printed."""


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def build_context(
        entity_id: str,
        period: str | None = None,
        include_transactions: bool = True,
        include_positions: bool = True,
) -> dict[str, Any]:
    """
    Build a rich AI-consumable context dict for one entity + period.

    Returns a dict with:
      - entity: entity metadata
      - period: statement period
      - balance_sheet: full balance sheet breakdown
      - transactions: list of transactions (if include_transactions=True)
      - positions: brokerage positions (if include_positions=True)
      - tax_estimate: the fiscal year's entity-level PTET estimate from
        ``api.pte_estimate`` (basis-labelled; numbers only), or None when there is
        none for this entity/year.  A single month is never projected ×12 (card Q-02).
      - summary: high-level KPIs
      - ai_instructions: system prompt snippet for LLMs
    """
    from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
    from ledger_agent.core.database import EntityRepo, SnapshotRepo, TransactionRepo

    # Resolve entity
    entities = EntityRepo.list_all()
    entity = next((e for e in entities if e.id == entity_id), None)
    if entity is None:
        raise ValueError(f"Entity not found: {entity_id}")

    # Resolve period
    snapshots = SnapshotRepo.list_for_entity(entity_id)
    available = sorted({s.statement_period for s in snapshots}, reverse=True)
    if not available:
        raise ValueError("No imported statements found for this entity.")
    period = period or available[0]

    # Build balance sheet
    bs = BalanceSheetBuilder(entity_id, period).build()

    # Tax: the fiscal year's api.pte_estimate — never this month ×12 (card Q-02)
    tax_estimate = _year_tax_estimate(entity_id, int(period[:4]))

    # Account / institution labels → stable tokens (shared with the legacy MCP server).
    label_map, acct_tokens = _account_label_tokens(entity_id)

    # Build context — names never exported; every free-text field redacted (P-06).
    ctx: dict[str, Any] = {
        "schema_version": "2.0",
        "generated_at": _now_iso(),
        "entity": {
            "name": ENTITY_PLACEHOLDER,
            "type": _redact_text(entity.entity_type),
            "state": _redact_text(entity.state),
        },
        "period": period,
        "available_periods": available,
        "balance_sheet": {
            "total_assets": float(bs.total_assets),
            "total_liabilities": float(bs.total_liabilities),
            "total_equity": float(bs.total_equity),
            "net_income": float(bs.net_income),
            "is_balanced": bs.is_balanced,
            "lines": [
                {
                    "coa_code": line.coa_code,
                    "label": _line_label(line, label_map),
                    "amount": float(line.amount),
                    "type": line.coa_type.value,
                    "is_subtotal": line.is_subtotal,
                    "indent": line.indent,
                }
                for line in bs.lines
            ],
        },
        "tax_estimate": tax_estimate,
        "summary": {
            "net_income": float(bs.net_income),
            "total_assets": float(bs.total_assets),
            "total_liabilities": float(bs.total_liabilities),
            "debt_to_equity": _safe_div(bs.total_liabilities, bs.total_equity),
        },
        "ai_instructions": _redact_text(
            f"You are analyzing financial data for {ENTITY_PLACEHOLDER} "
            f"({entity.entity_type}), "
            f"period {period}. "
            f"Net income: ${float(bs.net_income):,.2f}. "
            f"Total assets: ${float(bs.total_assets):,.2f}. "
            f"This data was generated by FinancialIntelligence. "
            f"Answer questions accurately using the data provided."
        ),
    }

    # Optionally include transactions — descriptions redacted for AI export (R-46)
    if include_transactions:
        txns = TransactionRepo.list_for_period(period)
        ctx["transactions"] = [
            {
                "date": t.date.isoformat(),
                "description": _redact_text(t.description),
                "amount": float(t.amount),
                "coa_code": t.coa_code,
                "type": t.transaction_type.value if t.transaction_type else None,
                "is_transfer": t.is_transfer,
            }
            for t in txns
        ]

    # Optionally include positions — account label tokenised, ticker kept (D6 pending)
    if include_positions:
        from ledger_agent.core.database import AccountRepo, PositionRepo
        accounts = AccountRepo.list_for_entity(entity_id)
        all_positions = []
        for acct in accounts:
            positions = PositionRepo.list_for_period(acct.id, period)
            for p in positions:
                all_positions.append({
                    "account": _position_account_label(acct.id, acct_tokens),
                    "symbol": p.symbol,
                    "quantity": float(p.quantity),
                    "price": float(p.price_per_unit),
                    "market_value": float(p.market_value),
                })
        ctx["positions"] = all_positions

    _audit_or_block(ctx)
    return ctx


def save_context(ctx: dict[str, Any], path: Path) -> Path:
    """Save context dict as pretty-printed JSON. Returns the file path.

    Audits the exact text first; on failure raises ContextExportBlocked and writes
    nothing (no directory, no file)."""
    text = json.dumps(ctx, indent=2, cls=_DecimalEncoder)
    _audit_or_block(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def context_to_prompt(ctx: dict[str, Any]) -> str:
    """
    Convert context to a compact text prompt suitable for LLM input.
    Useful for copy-paste to Perplexity, Claude.ai, etc.
    """
    bs = ctx["balance_sheet"]
    tax = ctx.get("tax_estimate")
    entity = ctx["entity"]
    lines = [
        f"# Financial Report: {entity['name']} ({ctx['period']})",
        f"Entity type: {entity['type']} | State: {entity['state']}",
        "",
        "## Balance Sheet",
        f"  Total Assets:      ${bs['total_assets']:>12,.2f}",
        f"  Total Liabilities: ${bs['total_liabilities']:>12,.2f}",
        f"  Total Equity:      ${bs['total_equity']:>12,.2f}",
        f"  Net Income:        ${bs['net_income']:>12,.2f}",
        f"  Balanced:          {bs['is_balanced']}",
    ]
    lines += _tax_prompt_lines(tax)

    if "transactions" in ctx:
        lines += ["", f"## Transactions ({len(ctx['transactions'])} total)"]
        for t in ctx["transactions"][:20]:
            lines.append(
                f"  {t['date']}  {t['description'][:40]:<40}  "
                f"${t['amount']:>10,.2f}  [{t['coa_code']}]"
            )
        if len(ctx["transactions"]) > 20:
            lines.append(f"  … and {len(ctx['transactions']) - 20} more")

    prompt = "\n".join(lines)
    _audit_or_block(prompt)
    return prompt


# ── Privacy helpers (P-06) ─────────────────────────────────────────────────────

def _redact_text(text: str | None) -> str | None:
    """``privacy.redact(scope="ai_context")``; any failure → ``<REDACTED>``, never raw."""
    if text is None or text == "":
        return text
    try:
        safe, _ = privacy.redact(str(text), scope="ai_context")
    except Exception:  # noqa: BLE001 — fail closed: drop the field, never the raw text
        return REDACTED
    return safe if isinstance(safe, str) else REDACTED


def _account_label_tokens(
        entity_id: str,
) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """(raw account label → token label, account_id → (account, institution) token).

    Reuses the legacy MCP server's tokenisation so both egress paths agree.  On any
    failure both maps are empty, so every account label falls back to a fixed token."""
    try:
        from mcp_server.server import _account_label_map, _account_tokens

        return _account_label_map(entity_id), _account_tokens(entity_id)
    except Exception:  # noqa: BLE001 — fail closed
        return {}, {}


def _line_label(line: Any, label_map: dict[str, str]) -> str | None:
    """Balance-sheet line label: account-built labels tokenised, the rest redacted."""
    from mcp_server.server import _ACCOUNT_FALLBACK, _ACCOUNT_LINE_CODES

    if line.label in label_map:
        return label_map[line.label]
    if line.coa_code in _ACCOUNT_LINE_CODES:
        return _ACCOUNT_FALLBACK  # unknown account label shape → never pass it through
    return _redact_text(line.label)


def _position_account_label(account_id: str, acct_tokens: dict[str, tuple[str, str]]) -> str:
    tokens = acct_tokens.get(account_id)
    if tokens is None:
        return REDACTED
    acct_tok, inst_tok = tokens
    return f"{inst_tok} – {acct_tok}"


def _audit_or_block(payload: Any) -> None:
    """``privacy.audit_egress``; any failure → ContextExportBlocked (no values echoed)."""
    try:
        privacy.audit_egress(payload)
    except Exception as exc:  # noqa: BLE001 — fail closed
        raise ContextExportBlocked(
            "AI context export aborted: the privacy audit did not pass "
            f"({type(exc).__name__}); nothing was written or printed."
        ) from exc


def _year_tax_estimate(entity_id: str, year: int) -> dict[str, Any] | None:
    """Numbers-only view of ``api.pte_estimate(year)`` for the context export.

    Returns None when there is no estimate (not enough statements) or when the year's
    estimate belongs to a different entity than the one being exported.  Carries no
    names, account ids or free-text notes — only the basis and the figures.
    """
    from ledger_agent.core import api

    try:
        # pte_estimate covers the entity with the most activity that year; never attach
        # another entity's figures to this entity's export.
        year_entity, _ = api._entity_and_periods(year)
        if year_entity.id != entity_id:
            return None
        est = api.pte_estimate(year)
    except ValueError:
        return None
    return {
        "source": "api.pte_estimate",
        "scope": "entity_level_state_ptet",
        "fiscal_year": est.fiscal_year,
        "basis": est.basis,
        "through_period": est.through_period,
        "months_covered": est.months_covered,
        "missing_statement_count": sum(len(m or []) for m in est.missing_statements.values()),
        "ytd_entity_taxable_income": float(est.ytd_net_income),
        "annual_entity_taxable_income": float(est.net_income),
        "total_annual_tax": float(est.total_annual_tax),
        "effective_rate_pct": float(est.effective_rate),
        "quarterly_payment": float(est.quarterly_payment),
        "required_to_date": float(est.required_to_date),
        "paid_ytd": float(est.paid_ytd),
        "remaining_balance": float(est.remaining_balance),
        "quarterly_payments": [
            {
                "quarter": pmt["quarter"],
                "due_date": pmt["due_date"],
                "amount": float(pmt["amount"]),
                "paid_amount": float(pmt["paid_amount"]),
                "status": pmt["status"],
            }
            for pmt in est.quarterly_payments
        ],
    }


def _tax_prompt_lines(tax: dict[str, Any] | None) -> list[str]:
    """Prompt section for the year's PTET estimate (basis line first)."""
    if not tax:
        return ["", "## Tax Estimate", "  Not available for this entity/year "
                "(not enough statements)."]
    lines = [
        "",
        f"## Tax Estimate — entity-level state PTET, FY{tax['fiscal_year']}",
        f"  Basis: {tax['basis']} through {tax['through_period']} "
        f"({tax['months_covered']}/12 months complete)",
    ]
    if tax["basis"] != "full_year":
        lines.append(f"  Missing statements: {tax['missing_statement_count']}")
    lines += [
        f"  Entity taxable income (to date): ${tax['ytd_entity_taxable_income']:>12,.2f}",
        f"  Entity taxable income (annual):  ${tax['annual_entity_taxable_income']:>12,.2f}",
        f"  Annual PTET:                     ${tax['total_annual_tax']:>12,.2f}",
        f"  Effective Rate:                  {tax['effective_rate_pct']:.2f}%",
        f"  Paid YTD:                        ${tax['paid_ytd']:>12,.2f}",
        f"  Remaining:                       ${tax['remaining_balance']:>12,.2f}",
        "",
        "## Quarterly Payments",
    ]
    for pmt in tax["quarterly_payments"]:
        lines.append(
            f"  {pmt['quarter']} – {pmt['due_date']}: ${pmt['amount']:,.2f} ({pmt['status']})"
        )
    return lines


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _safe_div(a, b) -> float | None:
    try:
        return round(float(a) / float(b), 4) if b else None
    except Exception:
        return None
