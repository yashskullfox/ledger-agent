"""
cli/quick_scan.py  –  Quick Scan mode
────────────────────────────────────────────────────────────────────
One command to go from a folder of PDFs to a full balance sheet
plus tax obligations.

Usage:
    python main.py scan [FOLDER_PATH]
    ./run.sh scan ~/Documents/statements/2025-01/

What it does:
  1. Discovers all PDF files in FOLDER_PATH (recursively optional)
  2. Auto-detects the correct parser for each PDF
  3. Imports any new statements (skips duplicates unless --force)
  4. Runs interactive multiple-choice classification for unclassified txns
  5. Displays the balance sheet for all imported periods
  6. Displays the tax obligation estimate (annualized)
  7. Offers to export context JSON for AI/GitHub consumption
  8. Offers to save exports (CSV / Excel / JSON)
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from core.logging_setup import get_logger

log = get_logger(__name__)


def cmd_quick_scan(folder: Optional[str] = None, force: bool = False) -> None:
    """
    Auto-import all PDFs from a folder, then generate balance sheet + tax report.

    Args:
        folder: Path to folder containing PDF statements. Prompts if None.
        force:  Re-import even if statement was already imported.
    """
    from core.database import init_db, SnapshotRepo
    from cli.prompts import (
        ask_select, ask_text,
        print_error, print_info, print_warning,
        prompt_classify,
    )
    from config import STATEMENTS_DIR

    init_db()

    try:
        from rich.console import Console
        from rich.panel import Panel
        Console().print(Panel(
            "[bold green]⚡ Quick Scan Mode[/bold green]\n"
            "[dim]Auto-import all PDFs → Balance Sheet + Tax Estimate[/dim]",
            border_style="green",
        ))
    except ImportError:
        print("\n=== Quick Scan Mode ===\n")

    from cli.commands import _get_or_setup_entity
    entity = _get_or_setup_entity()
    if not entity:
        print_error("No entity configured.")
        return

    if not folder:
        folder = ask_text(
            "Path to folder containing PDF statements:",
            default=str(STATEMENTS_DIR),
        )
    scan_dir = Path(folder).expanduser().resolve()
    if not scan_dir.is_dir():
        print_error(f"Directory not found: {scan_dir}")
        return

    pdf_files = sorted(scan_dir.rglob("*.pdf")) + sorted(scan_dir.rglob("*.PDF"))
    if not pdf_files:
        print_warning(f"No PDF files found in: {scan_dir}")
        return

    print_info(f"Found {len(pdf_files)} PDF file(s) in {scan_dir}")

    _load_all_parsers()

    from core.exceptions import ParserNotFoundError

    imported_count = 0
    skipped_count = 0
    failed: List[str] = []

    for pdf_path in pdf_files:
        print_info(f"\n📄 Processing: [bold]{pdf_path.name}[/bold]")
        try:
            _import_single(
                pdf_path=pdf_path,
                entity=entity,
                force=force,
                prompt_classify_fn=prompt_classify,
            )
            imported_count += 1
        except _AlreadyImportedSkip:
            print_warning(f"  ⏭  Already imported – skipping ({pdf_path.name})")
            skipped_count += 1
        except ParserNotFoundError as exc:
            print_warning(f"  ⚠  No parser: {exc}")
            failed.append(pdf_path.name)
        except Exception as exc:
            log.exception("Import failed", extra={"file": str(pdf_path)})
            print_error(f"  ✗  Failed: {exc}")
            failed.append(pdf_path.name)

    print_info(
        f"\n✅ Imported: {imported_count}  |  "
        f"⏭ Skipped: {skipped_count}  |  "
        f"❌ Failed: {len(failed)}"
    )
    if failed:
        print_warning("Failed files: " + ", ".join(failed))

    if imported_count == 0 and skipped_count == 0:
        print_error("No statements processed. Aborting report generation.")
        return

    snapshots = SnapshotRepo.list_for_entity(entity.id)
    periods = sorted({s.statement_period for s in snapshots}, reverse=True)
    if not periods:
        print_warning("No data available for reporting.")
        return

    all_label = f"All periods ({', '.join(periods)})"
    period_choice = ask_select(
        "Generate report for:",
        choices=[periods[0], all_label] + [p for p in periods[1:]],
        default=periods[0],
    )
    report_periods = periods if all_label in period_choice else [period_choice]

    from accounting.balance_sheet import BalanceSheetBuilder
    from reports.renderer import render_balance_sheet

    for p in report_periods:
        bs = BalanceSheetBuilder(entity.id, p).build()
        render_balance_sheet(bs)

        # Tax estimate for this period
        _show_tax_estimate(entity.name, bs, int(p[:4]))

    if len(report_periods) > 1:
        from cli.commands import cmd_mom_summary
        cmd_mom_summary()

    _offer_exports(entity, report_periods[-1], periods)


class _AlreadyImportedSkip(Exception):
    pass


def _load_all_parsers() -> None:
    """Import parsers package — auto-discovery in parsers/__init__.py handles the rest."""
    import parsers  # noqa: F401


def _import_single(pdf_path: Path, entity, force: bool, prompt_classify_fn) -> None:
    """Import one PDF. Raises _AlreadyImportedSkip or ParserNotFoundError."""
    from parsers.registry import ParserRegistry
    from parsers.base import BaseStatementParser
    from core.database import (
        AccountRepo, ImportRegistry, PositionRepo, SnapshotRepo, TransactionRepo,
    )
    from core.models import Account
    from cli.commands import _infer_account_type, _default_account_name
    from intelligence.classifier import classify_batch

    raw_text = BaseStatementParser.extract_text(pdf_path)
    parser_cls = ParserRegistry.detect_or_raise(raw_text)

    parser = parser_cls()
    stmt = parser.parse(pdf_path)

    acct = AccountRepo.find(stmt.institution, stmt.account_number_masked)
    if acct is None:
        acct = Account(
            entity_id=entity.id,
            name=_default_account_name(stmt.statement_type),
            institution=stmt.institution,
            account_type=_infer_account_type(stmt.statement_type),
            account_number_masked=stmt.account_number_masked,
        )
        AccountRepo.upsert(acct)

    # Check duplicate
    if ImportRegistry.already_imported(acct.id, stmt.statement_period) and not force:
        raise _AlreadyImportedSkip()

    # Persist
    for t in stmt.transactions:
        t.account_id = acct.id
    TransactionRepo.bulk_insert(stmt.transactions)

    if stmt.positions:
        for p in stmt.positions:
            p.account_id = acct.id
        PositionRepo.upsert_period(stmt.positions)

    if stmt.snapshot:
        stmt.snapshot.account_id = acct.id
        SnapshotRepo.upsert(stmt.snapshot)

    # Classify
    unclassified = [t for t in stmt.transactions if not t.coa_code]
    if unclassified:
        from cli.prompts import print_info
        print_info(f"  → Classifying {len(unclassified)} transactions…")
        _, auto, prompted = classify_batch(unclassified, prompt_fn=prompt_classify_fn)
        print_info(f"  → {auto} auto-classified, {prompted} prompted.")

    # Register
    ImportRegistry.record(str(pdf_path), stmt.parser_id, acct.id, stmt.statement_period)
    from cli.prompts import print_success
    print_success(
        f"  ✓ {stmt.institution} | {stmt.statement_period} | "
        f"{len(stmt.transactions)} txns | ${stmt.snapshot.ending_balance:,.2f}"
        if stmt.snapshot else
        f"  ✓ {stmt.institution} | {stmt.statement_period} | {len(stmt.transactions)} txns"
    )


def _show_tax_estimate(entity_name: str, bs, year: int) -> None:
    """Print tax estimate for a balance sheet."""
    try:
        from accounting.tax_estimator import TaxEstimator, render_tax_estimate
        est = TaxEstimator(entity_name, year).estimate_from_balance_sheet(bs)
        render_tax_estimate(est)
    except Exception as exc:
        log.warning("Tax estimate failed", extra={"error": str(exc)})


def _offer_exports(entity, latest_period: str, all_periods: List[str]) -> None:
    """Offer export options after the scan."""
    from cli.prompts import ask_select, print_success, print_info
    from accounting.balance_sheet import BalanceSheetBuilder

    export = ask_select(
        "Export options:",
        choices=[
            "CSV",
            "Excel (.xlsx)",
            "JSON",
            "AI Context JSON (for Claude / GPT / Perplexity)",
            "No thanks",
        ],
        default="No thanks",
    )

    if export == "No thanks":
        return

    bs = BalanceSheetBuilder(entity.id, latest_period).build()

    if export == "CSV":
        from reports.renderer import export_balance_sheet_csv
        path = export_balance_sheet_csv(bs)
        print_success(f"Saved: {path}")

    elif export == "Excel (.xlsx)":
        from reports.renderer import export_balance_sheet_excel
        path = export_balance_sheet_excel(bs)
        if path:
            print_success(f"Saved: {path}")

    elif export == "JSON":
        from reports.renderer import export_balance_sheet_json
        path = export_balance_sheet_json(bs)
        print_success(f"Saved: {path}")

    elif "AI Context" in export:
        from adapters.context_builder import build_context, save_context
        from config import EXPORTS_DIR
        ctx_path = EXPORTS_DIR / f"ai_context_{latest_period}.json"
        ctx = build_context(entity.id, latest_period)
        save_context(ctx, ctx_path)
        print_success(f"AI context saved: {ctx_path}")
        print_info(
            "[dim]Pass this file to any AI assistant for financial Q&A:\n"
            "  'Here is my financial data: [paste content] – please analyze it.'[/dim]"
        )
