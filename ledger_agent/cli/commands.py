"""
cli/commands.py  –  Command implementations (import, report, classify, etc.)
──────────────────────────────────────────────────────────────────────────────
Each function here is called by main.py's menu dispatcher.
All heavy logic lives in parsers / intelligence / accounting / reports.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ledger_agent.cli.prompts import (
    ask_confirm, ask_select, print_error, print_info, print_success, print_warning,
    prompt_classify, prompt_statement_file, wizard_entity,
)
from config import STATEMENTS_DIR
from ledger_agent.core.database import (
    AccountRepo, EntityRepo, ImportRegistry, PositionRepo,
    SnapshotRepo, TransactionRepo, init_db,
)
from ledger_agent.core.exceptions import ParserNotFoundError
from ledger_agent.core.models import Account, AccountType, Entity, StatementType


def cmd_setup() -> Entity:
    """First-run wizard: create entity record in DB."""
    init_db()
    existing = EntityRepo.list_all()
    if existing:
        print_info(f"Entity already configured: {existing[0].name}")
        if not ask_confirm("Re-run setup wizard?", default=False):
            return existing[0]

    data = wizard_entity()
    entity = Entity(
        name=data["name"],
        entity_type=data["entity_type"],
        state=data["state"],
        ein_masked=data["ein_masked"] or None,
        notes=data["notes"],
    )
    EntityRepo.upsert(entity)
    print_success(f"Entity '{entity.name}' saved.")
    return entity


def _get_or_setup_entity() -> Optional[Entity]:
    """Return first entity or run setup wizard if none exists."""
    entities = EntityRepo.list_all()
    if not entities:
        print_warning("No entity configured. Running setup wizard…")
        return cmd_setup()
    if len(entities) == 1:
        return entities[0]
    names = [e.name for e in entities]
    choice = ask_select("Select entity:", choices=names)
    return next((e for e in entities if e.name == choice), entities[0])


def cmd_import(pdf_path: Optional[str] = None) -> None:
    """
    Parse a PDF statement and persist all extracted data to the database.
    Runs interactive classification for any unrecognised transactions.
    """
    init_db()
    entity = _get_or_setup_entity()
    if not entity:
        print_error("No entity configured. Aborting.")
        return

    if not pdf_path:
        pdf_path = prompt_statement_file(STATEMENTS_DIR)
    if not pdf_path or not Path(pdf_path).exists():
        # Try statements dir
        candidate = STATEMENTS_DIR / Path(pdf_path).name if pdf_path else None
        if candidate and candidate.exists():
            pdf_path = str(candidate)
        else:
            print_error(f"File not found: {pdf_path}")
            return

    pdf_path = Path(pdf_path)
    print_info(f"Reading PDF: {pdf_path.name} …")

    import ledger_agent.core.parsers  # noqa: F401  – triggers auto-discovery in parsers/__init__.py
    from ledger_agent.core.parsers.registry import ParserRegistry

    from ledger_agent.core.parsers.base import BaseStatementParser
    raw_text = BaseStatementParser.extract_text(pdf_path)

    try:
        parser_cls = ParserRegistry.detect_or_raise(raw_text)
    except ParserNotFoundError as exc:
        print_error(str(exc))
        return

    print_info(f"Parser detected: [bold]{parser_cls.INSTITUTION}[/bold]")

    parser = parser_cls()
    stmt = parser.parse(pdf_path)
    print_success(
        f"Parsed {stmt.statement_period}  |  "
        f"{len(stmt.transactions)} transactions  |  "
        f"{len(stmt.positions)} positions"
    )

    acct = AccountRepo.find(stmt.institution, stmt.account_number_masked)
    if acct is None:
        acct_type = _infer_account_type(stmt.statement_type)
        acct = Account(
            entity_id=entity.id,
            name=_default_account_name(stmt.statement_type),
            institution=stmt.institution,
            account_type=acct_type,
            account_number_masked=stmt.account_number_masked,
        )
        AccountRepo.upsert(acct)
        print_success(f"New account registered: {acct}")

    if ImportRegistry.already_imported(acct.id, stmt.statement_period):
        print_warning(
            f"Statement for {stmt.statement_period} already imported for this account."
        )
        if not ask_confirm("Re-import and overwrite?", default=False):
            return

    for t in stmt.transactions:
        t.account_id = acct.id
    inserted = TransactionRepo.bulk_insert(stmt.transactions)
    print_info(f"  → {inserted} new transactions inserted.")

    if stmt.positions:
        for p in stmt.positions:
            p.account_id = acct.id
        PositionRepo.upsert_period(stmt.positions, )
        print_info(f"  → {len(stmt.positions)} positions updated.")

    if stmt.snapshot:
        stmt.snapshot.account_id = acct.id
        SnapshotRepo.upsert(stmt.snapshot)
        print_info(f"  → Snapshot saved (ending balance: ${stmt.snapshot.ending_balance:,.2f})")

    from ledger_agent.core.intelligence.classifier import classify_batch
    txns_to_classify = [t for t in stmt.transactions if not t.coa_code]
    if txns_to_classify:
        print_info(f"\nClassifying {len(txns_to_classify)} transactions…")
        _, auto, prompted = classify_batch(txns_to_classify, prompt_fn=prompt_classify)
        print_success(f"Classification complete: {auto} auto, {prompted} prompted.")
    else:
        print_info("  → All transactions pre-classified.")

    all_txns = TransactionRepo.list_for_period(stmt.statement_period)
    from ledger_agent.core.intelligence.reconciler import reconcile
    matches, unmatched = reconcile(all_txns)
    if matches:
        print_success(f"Reconciled {len(matches)} inter-account transfer(s).")
    if unmatched:
        print_warning(f"{len(unmatched)} unmatched transfer(s) – see reconciliation report.")

    ImportRegistry.record(str(pdf_path), stmt.parser_id, acct.id, stmt.statement_period)
    print_success(f"Import complete for {stmt.statement_period}!")


def cmd_balance_sheet(period: Optional[str] = None) -> None:
    """Build and display the balance sheet for a given period."""
    init_db()
    entity = _get_or_setup_entity()
    if not entity:
        return

    # Discover available periods
    snapshots = SnapshotRepo.list_for_entity(entity.id)
    periods = sorted({s.statement_period for s in snapshots}, reverse=True)
    if not periods:
        print_warning("No imported statements found. Import a statement first.")
        return

    if not period:
        period = ask_select(
            "Select period for balance sheet:",
            choices=periods,
            default=periods[0],
        )

    from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
    bs = BalanceSheetBuilder(entity.id, period).build()

    from ledger_agent.core.reports.renderer import render_balance_sheet
    render_balance_sheet(bs)

    # Export options
    export = ask_select(
        "Export balance sheet?",
        choices=["CSV", "Excel (.xlsx)", "JSON", "No thanks"],
        default="No thanks",
    )
    if export == "CSV":
        from ledger_agent.core.reports.renderer import export_balance_sheet_csv
        path = export_balance_sheet_csv(bs)
        print_success(f"Saved: {path}")
    elif export == "Excel (.xlsx)":
        from ledger_agent.core.reports.renderer import export_balance_sheet_excel
        path = export_balance_sheet_excel(bs)
        if path:
            print_success(f"Saved: {path}")
    elif export == "JSON":
        from ledger_agent.core.reports.renderer import export_balance_sheet_json
        path = export_balance_sheet_json(bs)
        print_success(f"Saved: {path}")


def cmd_transactions(period: Optional[str] = None) -> None:
    """Display and optionally export transactions for a period."""
    init_db()
    entity = _get_or_setup_entity()
    if not entity:
        return

    snapshots = SnapshotRepo.list_for_entity(entity.id)
    periods = sorted({s.statement_period for s in snapshots}, reverse=True)
    if not periods:
        print_warning("No imported statements found.")
        return

    if not period:
        period = ask_select("Select period:", choices=periods, default=periods[0])

    txns = TransactionRepo.list_for_period(period)
    from ledger_agent.core.reports.renderer import render_transactions
    render_transactions(txns, title=f"Transactions  –  {period}")

    if ask_confirm("Export to CSV?", default=False):
        from ledger_agent.core.reports.renderer import export_transactions_csv
        path = export_transactions_csv(txns, period)
        print_success(f"Saved: {path}")


def cmd_classify() -> None:
    """Interactively classify any unclassified transactions."""
    init_db()
    from ledger_agent.core.database import TransactionRepo
    unclassified = TransactionRepo.list_unclassified()
    if not unclassified:
        print_success("All transactions are classified!")
        return

    print_info(f"{len(unclassified)} unclassified transactions.")
    from ledger_agent.core.intelligence.classifier import classify_batch
    classify_batch(unclassified, prompt_fn=prompt_classify)
    print_success("Classification pass complete.")


def cmd_memory() -> None:
    """Show and manage the classification memory (learned rules)."""
    from ledger_agent.core.intelligence.memory import get_memory
    memory = get_memory()
    rules = memory.list_rules()
    if not rules:
        print_info("No rules in memory yet.")
        return

    try:
        from rich.table import Table
        from rich.console import Console
        _r = True
    except ImportError:
        _r = False

    if _r:
        tbl = Table(title="Classification Memory", show_lines=True)
        tbl.add_column("Pattern", style="cyan", max_width=35)
        tbl.add_column("COA Code", style="yellow", width=10)
        tbl.add_column("COA Name", style="white", max_width=30)
        tbl.add_column("Transfer?", width=10)
        tbl.add_column("Confirmed #", justify="right", width=12)
        for r in rules:
            tbl.add_row(
                r["pattern"][:35], r["coa_code"], r["coa_name"],
                "✓" if r.get("is_transfer") else "",
                str(r.get("confirmed_count", 0)),
            )
        Console().print(tbl)
    else:
        for r in rules:
            print(f"  {r['pattern']:<35}  {r['coa_code']}  {r['coa_name']}")

    if ask_confirm("Delete a rule?", default=False):
        patterns = [r["pattern"] for r in rules]
        sel = ask_select("Select rule to delete:", choices=patterns)
        if memory.remove_rule(sel):
            print_success(f"Rule deleted: {sel}")


def cmd_mom_summary() -> None:
    """Show month-over-month balance sheet comparison."""
    init_db()
    entity = _get_or_setup_entity()
    if not entity:
        return

    snapshots = SnapshotRepo.list_for_entity(entity.id)
    all_periods = sorted({s.statement_period for s in snapshots})
    if not all_periods:
        print_warning("No data available.")
        return

    from ledger_agent.core.accounting.balance_sheet import build_comparison
    sheets = build_comparison(entity.id, all_periods)

    try:
        from rich.table import Table
        from rich.console import Console
        tbl = Table(title=f"{entity.name}  –  Month-over-Month Summary")
        tbl.add_column("Metric", style="cyan", min_width=28)
        for p in all_periods:
            tbl.add_column(p, justify="right", min_width=14)
        rows_def = [
            ("Total Assets", "total_assets"),
            ("Total Liabilities", "total_liabilities"),
            ("Total Equity", "total_equity"),
            ("Net Income", "net_income"),
        ]
        for label, attr in rows_def:
            row = [label]
            for p in all_periods:
                bs = sheets.get(p)
                val = getattr(bs, attr) if bs else None
                row.append(f"${val:,.2f}" if val is not None else "N/A")
            tbl.add_row(*row)
        Console().print(tbl)
    except ImportError:
        for p, bs in sheets.items():
            print(f"\n{p}  Assets: ${bs.total_assets:,.2f}  "
                  f"Liabilities: ${bs.total_liabilities:,.2f}  "
                  f"Equity: ${bs.total_equity:,.2f}  "
                  f"NI: ${bs.net_income:,.2f}")


def _infer_account_type(stmt_type: StatementType) -> AccountType:
    if stmt_type == StatementType.BANK_CHECKING:
        return AccountType.CHECKING
    if stmt_type == StatementType.BANK_SAVINGS:
        return AccountType.SAVINGS
    if stmt_type == StatementType.BROKERAGE:
        return AccountType.BROKERAGE
    if stmt_type == StatementType.CREDIT_CARD:
        return AccountType.CREDIT_CARD
    return AccountType.OTHER


def _default_account_name(stmt_type: StatementType) -> str:
    mapping = {
        StatementType.BANK_CHECKING: "Business Checking",
        StatementType.BANK_SAVINGS: "Business Savings",
        StatementType.BROKERAGE: "Brokerage Account",
    }
    return mapping.get(stmt_type, "Account")
