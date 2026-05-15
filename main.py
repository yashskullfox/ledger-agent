#!/usr/bin/env python3
"""
main.py  –  FinancialIntelligence  CLI entry point
────────────────────────────────────────────────────
Usage:
    python main.py                        # interactive menu
    python main.py import [PDF_PATH]      # import a statement PDF
    python main.py scan   [FOLDER_PATH]   # ⚡ coverage wizard + import all PDFs
    python main.py onboard [FOLDER]       # alias for scan (R-45 coverage wizard)
    python main.py balance [PERIOD]       # show balance sheet  (e.g. 2025-01)
    python main.py transactions [PERIOD]
    python main.py classify               # classify pending transactions
    python main.py memory                 # view / manage learned rules
    python main.py summary                # month-over-month comparison
    python main.py tax     [PERIOD]       # show tax obligation estimate
    python main.py context [PERIOD]       # export AI-consumable context JSON
    python main.py setup                  # re-run entity setup wizard

Flags (scan / onboard):
    --force       Re-import already-imported statements
    --no-prompt   Non-interactive / CI mode (emit JSON coverage matrix)
    --window YYYY-MM:YYYY-MM   Override the default 12-month rolling window
    --report      Show balance sheet + tax after ingestion

Modes (set via FI_AI_BACKEND env var):
    local   – Rule-based classifier, no API key required (default)
    openai  – OpenAI Chat Completions (requires FI_OPENAI_API_KEY)
    gemini  – Google Gemini (requires FI_GEMINI_API_KEY)

FinancialIntelligence – generic, extensible financial statement aggregator
for small business entities (LLCs, etc.).

Add a new institution parser:
    1. Create parsers/<institution>.py
    2. Subclass BaseStatementParser
    3. Decorate with @ParserRegistry.register
    → Done.  No changes needed anywhere else.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ledger_agent.core.database import init_db
from ledger_agent.core.logging_setup import configure_logging
from ledger_agent.cli.prompts import ask_select, print_error, print_info


def _boot() -> None:
    """Ensure DB, directories, and logging are initialised before any command runs."""
    configure_logging()
    init_db()
    # Auto-load .env if python-dotenv is installed
    try:
        from dotenv import load_dotenv
        load_dotenv(override=False)  # don't override already-set env vars
    except ImportError:
        pass


MENU_CHOICES = [
    "⚡  Quick Scan (import folder → balance sheet + tax)",
    "📥  Import statement PDF",
    "📊  View balance sheet",
    "💰  Tax obligation estimate",
    "📋  View transactions",
    "🏷   Classify unclassified transactions",
    "📈  Month-over-month summary",
    "🤖  Export AI context (for Claude / GPT / Perplexity)",
    "🧠  View / manage memory rules",
    "⚙️   Entity setup",
    "🚪  Exit",
]


def interactive_menu() -> None:
    """Main interactive menu loop."""
    try:
        from rich.console import Console
        from rich.panel import Panel
        Console().print(Panel(
            "[bold cyan]FinancialIntelligence[/bold cyan]\n"
            "[dim]Generic financial statement aggregator & balance sheet generator[/dim]",
            border_style="cyan",
        ))
    except ImportError:
        print("\n=== FinancialIntelligence ===\n")

    while True:
        choice = ask_select("\nWhat would you like to do?", choices=MENU_CHOICES)

        if choice and "Quick Scan" in choice:
            from ledger_agent.cli.onboarding import cmd_onboard
            cmd_onboard(show_report=True)

        elif choice and "Import" in choice:
            from ledger_agent.cli.commands import cmd_import
            cmd_import()

        elif choice and "balance sheet" in choice:
            from ledger_agent.cli.commands import cmd_balance_sheet
            cmd_balance_sheet()

        elif choice and "Tax obligation" in choice:
            _cmd_tax()

        elif choice and "transactions" in choice:
            from ledger_agent.cli.commands import cmd_transactions
            cmd_transactions()

        elif choice and "Classify" in choice:
            from ledger_agent.cli.commands import cmd_classify
            cmd_classify()

        elif choice and "Month-over-month" in choice:
            from ledger_agent.cli.commands import cmd_mom_summary
            cmd_mom_summary()

        elif choice and "AI context" in choice:
            _cmd_context()

        elif choice and "memory" in choice:
            from ledger_agent.cli.commands import cmd_memory
            cmd_memory()

        elif choice and "setup" in choice.lower():
            from ledger_agent.cli.commands import cmd_setup
            cmd_setup()

        elif choice and ("Exit" in choice or "exit" in choice):
            print_info("Goodbye! 👋")
            sys.exit(0)


def _cmd_tax(period: str | None = None) -> None:
    """Show tax obligation estimate for a period."""
    init_db()
    from ledger_agent.cli.commands import _get_or_setup_entity
    from ledger_agent.core.database import SnapshotRepo
    entity = _get_or_setup_entity()
    if not entity:
        return
    snapshots = SnapshotRepo.list_for_entity(entity.id)
    periods = sorted({s.statement_period for s in snapshots}, reverse=True)
    if not periods:
        print_error("No imported statements found. Import a statement first.")
        return
    if not period:
        period = ask_select("Select period for tax estimate:", choices=periods, default=periods[0])
    from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
    from ledger_agent.core.accounting.tax_estimator import TaxEstimator, render_tax_estimate
    bs = BalanceSheetBuilder(entity.id, period).build()
    est = TaxEstimator(entity.name, int(period[:4])).estimate_from_balance_sheet(bs)
    render_tax_estimate(est)


def _cmd_context(period: str | None = None) -> None:
    """Export AI-consumable context JSON for a period."""
    init_db()
    from ledger_agent.cli.commands import _get_or_setup_entity
    from ledger_agent.core.database import SnapshotRepo
    from ledger_agent.cli.prompts import print_success
    entity = _get_or_setup_entity()
    if not entity:
        return
    snapshots = SnapshotRepo.list_for_entity(entity.id)
    periods = sorted({s.statement_period for s in snapshots}, reverse=True)
    if not periods:
        print_error("No imported statements found.")
        return
    if not period:
        period = ask_select("Select period for AI context:", choices=periods, default=periods[0])
    from adapters.context_builder import build_context, save_context, context_to_prompt
    from config import EXPORTS_DIR
    ctx = build_context(entity.id, period)
    path = save_context(ctx, EXPORTS_DIR / f"ai_context_{period}.json")
    print_success(f"AI context saved: {path}")
    print_info("\n[bold]Compact text prompt (paste into any AI):[/bold]")
    print(context_to_prompt(ctx))


def main() -> None:
    _boot()
    args = sys.argv[1:]

    if not args:
        interactive_menu()
        return

    cmd = args[0].lower()
    rest = args[1:]

    if cmd in ("scan", "s", "onboard", "o"):
        from ledger_agent.cli.onboarding import cmd_onboard
        force = "--force" in rest or "-f" in rest
        no_prompt = "--no-prompt" in rest
        show_report = "--report" in rest
        window_arg = next(
            (rest[i + 1] for i, r in enumerate(rest) if r == "--window" and i + 1 < len(rest)),
            None,
        )
        folder = next((r for r in rest if not r.startswith("-")), None)
        code = cmd_onboard(
            folder=folder,
            window_arg=window_arg,
            no_prompt=no_prompt,
            force=force,
            show_report=show_report,
        )
        sys.exit(code)

    elif cmd in ("import", "i"):
        from ledger_agent.cli.commands import cmd_import
        cmd_import(rest[0] if rest else None)

    elif cmd in ("balance", "bs", "b"):
        from ledger_agent.cli.commands import cmd_balance_sheet
        cmd_balance_sheet(rest[0] if rest else None)

    elif cmd in ("transactions", "tx", "t"):
        from ledger_agent.cli.commands import cmd_transactions
        cmd_transactions(rest[0] if rest else None)

    elif cmd in ("classify", "c"):
        from ledger_agent.cli.commands import cmd_classify
        cmd_classify()

    elif cmd in ("memory", "m"):
        from ledger_agent.cli.commands import cmd_memory
        cmd_memory()

    elif cmd in ("summary", "mom"):
        from ledger_agent.cli.commands import cmd_mom_summary
        cmd_mom_summary()

    elif cmd in ("tax", "taxes"):
        _cmd_tax(rest[0] if rest else None)

    elif cmd in ("context", "ctx"):
        _cmd_context(rest[0] if rest else None)

    elif cmd in ("setup", "init"):
        from ledger_agent.cli.commands import cmd_setup
        cmd_setup()

    elif cmd in ("mcp",):
        from mcp_server.server import main as mcp_main
        mcp_main()

    else:
        print_error(f"Unknown command: '{cmd}'")
        print_info(
            "Usage: python main.py "
            "[scan|import|balance|transactions|classify|memory|summary|tax|context|setup]"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
