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
  * Account and institution names are tokenised exactly like the legacy MCP server,
    by the shared core tokeniser (``ledger_agent.core.labels.account_tokens`` /
    ``account_label_map``, P-12):
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

Storage (X-06) — ``save_context`` writes only inside the configured data directory
(``ledger_agent.core.config.DATA_DIR``, which holds ``EXPORTS_DIR``; gitignored):
  * the context is first rebuilt from an explicit field allow-list (``_EXPORT_SCHEMA``):
    known keys only, numbers coerced to numbers, text fields must already be strings —
    an unexpected field blocks the export instead of being written;
  * the exact UTF-8 text is audited before any directory or file is created;
  * the file is created owner-only (0600; advisory on Windows) and re-chmodded if it
    already existed; a target that resolves outside ``DATA_DIR`` is refused.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from ledger_agent.core import config as _core_config
from ledger_agent.core import privacy

ENTITY_PLACEHOLDER = "<ENTITY_NAME>"
REDACTED = "<REDACTED>"


class ContextExportBlocked(privacy.PrivacyLeakError):
    """The AI context failed the egress audit; it must not be returned, written or printed."""


class ContextExportPathError(ValueError):
    """The export target resolves outside the configured data directory; nothing written."""


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
    from ledger_agent.core.database import (
        AccountRepo,
        EntityRepo,
        PositionRepo,
        SnapshotRepo,
        TransactionRepo,
    )

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

    # Every data section is scoped to this entity's own accounts (ARCH-30 / card R-07):
    # the balance sheet, snapshots and label tokens are built per entity; transactions
    # and positions are read only for these account ids.
    accounts = AccountRepo.list_for_entity(entity_id)
    account_ids = [a.id for a in accounts]

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

    # Optionally include transactions — descriptions redacted for AI export (R-46).
    # Only this entity's accounts: another entity's rows must never enter (R-07).
    if include_transactions:
        txns = TransactionRepo.list_for_periods([period], account_ids=account_ids)
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
    """Save context dict as pretty-printed UTF-8 JSON. Returns the file path.

    Nothing is created (no directory, no file) unless every check passes:
      1. ``path`` resolves inside ``config.DATA_DIR`` → else ContextExportPathError;
      2. ``ctx`` rebuilds from the field allow-list → else ContextExportBlocked;
      3. the exact text passes ``privacy.audit_egress`` → else ContextExportBlocked.
    The file is then created owner-only (0600)."""
    target = _export_target(path)
    # ensure_ascii=False: the audit scans the same characters that are written, so a
    # non-ASCII name cannot hide behind a "\uXXXX" escape.
    text = json.dumps(_export_projection(ctx), indent=2, ensure_ascii=False)
    _audit_or_block(text)
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_owner_only(target, text)
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

    Uses the core tokeniser shared with the legacy MCP server so both egress paths
    agree.  On any failure both maps are empty, so every account label falls back to a
    fixed token."""
    try:
        from ledger_agent.core import labels

        return labels.account_label_map(entity_id), labels.account_tokens(entity_id)
    except Exception:  # noqa: BLE001 — fail closed
        return {}, {}


def _line_label(line: Any, label_map: dict[str, str]) -> str | None:
    """Balance-sheet line label: account-built labels tokenised, the rest redacted."""
    from ledger_agent.core.labels import account_line_label

    token = account_line_label(line, label_map)
    return _redact_text(line.label) if token is None else token


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


# ── Storage helpers (X-06) ─────────────────────────────────────────────────────

_STR, _NUM, _INT, _BOOL = "str", "num", "int", "bool"

_QUARTERLY_PAYMENT = {
    "quarter": _STR, "due_date": _STR, "amount": _NUM, "paid_amount": _NUM, "status": _STR,
}
_TAX_ESTIMATE = {
    "source": _STR, "scope": _STR, "fiscal_year": _INT, "basis": _STR,
    "through_period": _STR, "months_covered": _INT, "missing_statement_count": _INT,
    "ytd_entity_taxable_income": _NUM, "annual_entity_taxable_income": _NUM,
    "total_annual_tax": _NUM, "effective_rate_pct": _NUM, "quarterly_payment": _NUM,
    "required_to_date": _NUM, "paid_ytd": _NUM, "remaining_balance": _NUM,
    "quarterly_payments": [_QUARTERLY_PAYMENT],
}
# Every field ``build_context`` emits — and nothing else — may be written to disk.
_EXPORT_SCHEMA: dict[str, Any] = {
    "schema_version": _STR,
    "generated_at": _STR,
    "entity": {"name": _STR, "type": _STR, "state": _STR},
    "period": _STR,
    "available_periods": [_STR],
    "balance_sheet": {
        "total_assets": _NUM, "total_liabilities": _NUM, "total_equity": _NUM,
        "net_income": _NUM, "is_balanced": _BOOL,
        "lines": [{
            "coa_code": _STR, "label": _STR, "amount": _NUM, "type": _STR,
            "is_subtotal": _BOOL, "indent": _INT,
        }],
    },
    "tax_estimate": _TAX_ESTIMATE,
    "summary": {
        "net_income": _NUM, "total_assets": _NUM, "total_liabilities": _NUM,
        "debt_to_equity": _NUM,
    },
    "ai_instructions": _STR,
    "transactions": [{
        "date": _STR, "description": _STR, "amount": _NUM, "coa_code": _STR,
        "type": _STR, "is_transfer": _BOOL,
    }],
    "positions": [{
        "account": _STR, "symbol": _STR, "quantity": _NUM, "price": _NUM,
        "market_value": _NUM,
    }],
}


def _export_projection(value: Any, spec: Any = None) -> Any:
    """Rebuild ``value`` from the ``_EXPORT_SCHEMA`` allow-list (fresh containers).

    Leaves are type-checked, numbers coerced (Decimal → float); ``None`` is kept.  An
    unknown key or an unexpected type raises ContextExportBlocked (no values echoed)."""
    spec = _EXPORT_SCHEMA if spec is None else spec
    if value is None:
        return None
    if isinstance(spec, dict):
        if not isinstance(value, dict) or not set(value) <= set(spec):
            raise _blocked("an unexpected field")
        return {key: _export_projection(item, spec[key]) for key, item in value.items()}
    if isinstance(spec, list):
        if not isinstance(value, list):
            raise _blocked("an unexpected field")
        return [_export_projection(item, spec[0]) for item in value]
    if spec == _STR and isinstance(value, str):
        return str(value)
    if spec == _BOOL and isinstance(value, bool):
        return bool(value)
    if not isinstance(value, bool):
        if spec == _INT and isinstance(value, int):
            return int(value)
        if spec == _NUM and isinstance(value, (int, float, Decimal)):
            return float(value)
    raise _blocked("an unexpected value type")


def _blocked(reason: str) -> ContextExportBlocked:
    return ContextExportBlocked(
        f"AI context export aborted: the context carries {reason}; nothing was written."
    )


def _export_target(path: Path) -> Path:
    """Resolved ``path`` if it lies inside ``config.DATA_DIR``; else ContextExportPathError.

    Symlinks are resolved first, so a link cannot point the write outside the root.
    The root is read at call time (tests point ``DATA_DIR`` at a tmp dir)."""
    root = Path(_core_config.DATA_DIR).resolve()
    target = Path(path).resolve()
    if target == root or not target.is_relative_to(root):
        raise ContextExportPathError(
            "AI context export refused: the target is outside the configured data "
            "directory (FI_DATA_DIR); nothing was written."
        )
    return target


def _write_owner_only(target: Path, text: str) -> None:
    """Write ``text`` as UTF-8 to ``target``, created 0600 (chmod 0600 if it existed)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)  # O_CREAT's mode does not apply to an existing file
        else:  # Windows: mode bits are advisory
            os.chmod(target, 0o600)
        handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
    except Exception:
        os.close(fd)
        raise
    with handle:
        # CodeQL py/clear-text-storage-sensitive-data flags this write: its "private"
        # sources are the redactor's own detector calls (privacy._detect_ssn /
        # _detect_credit_cards / _detect_account_numbers), whose replacement tokens
        # flow into the redacted strings.  ``text`` is the allow-listed context after
        # privacy.redact(scope="ai_context"), audited by privacy.audit_egress above;
        # the file is owner-only under the gitignored data/ directory.
        handle.write(text)


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
        return [
            "",
            "## Tax Estimate",
            "  Not available for this entity/year (not enough statements).",
        ]
    lines = [
        "",
        f"## Tax Estimate — entity-level state PTET, FY{tax['fiscal_year']}",
        (
            f"  Basis: {tax['basis']} through {tax['through_period']} "
            f"({tax['months_covered']}/12 months complete)"
        ),
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
