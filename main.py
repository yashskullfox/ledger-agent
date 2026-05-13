#!/usr/bin/env python3
"""
main.py  –  FinancialIntelligence  CLI entry point
────────────────────────────────────────────────────
Usage:
    python main.py                     # interactive menu
    python main.py import [PDF_PATH]   # import a statement PDF
    python main.py balance [PERIOD]    # show balance sheet  (e.g. 2025-01)
    python main.py transactions [PERIOD]
    python main.py classify            # classify pending transactions
    python main.py memory              # view / manage learned rules
    python main.py summary             # month-over-month comparison
    python main.py setup               # re-run entity setup wizard

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

# ── Add project root to sys.path so all imports work when run directly ────────
sys.path.insert(0, str(Path(__file__).parent))

from config import DB_PATH
from core.database import init_db
from cli.prompts import ask_select, print_error, print_info


# ── Boot ──────────────────────────────────────────────────────────────────────

def _boot() -> None:
    """Ensure DB and directories exist before any command runs."""
    init_db()


# ── Menu ──────────────────────────────────────────────────────────────────────

MENU_CHOICES = [
    "📥  Import statement PDF",
    "📊  View balance sheet",
    "📋  View transactions",
    "🏷   Classify unclassified transactions",
    "📈  Month-over-month summary",
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

        if choice and "Import" in choice:
            from cli.commands import cmd_import
            cmd_import()

        elif choice and "balance sheet" in choice:
            from cli.commands import cmd_balance_sheet
            cmd_balance_sheet()

        elif choice and "transactions" in choice:
            from cli.commands import cmd_transactions

            cmd_transactions()

        elif choice and "Classify" in choice:
            from cli.commands import cmd_classify
            cmd_classify()

        elif choice and "Month-over-month" in choice:
            from cli.commands import cmd_mom_summary
            cmd_mom_summary()

        elif choice and "memory" in choice:
            from cli.commands import cmd_memory
            cmd_memory()

        elif choice and "setup" in choice.lower():
            from cli.commands import cmd_setup
            cmd_setup()

        elif choice and ("Exit" in choice or "exit" in choice):
            print_info("Goodbye! 👋")
            sys.exit(0)


# ── CLI argument dispatch ─────────────────────────────────────────────────────

def main() -> None:
    _boot()
    args = sys.argv[1:]

    if not args:
        interactive_menu()
        return

    cmd = args[0].lower()
    rest = args[1:]

    if cmd in ("import", "i"):
        from cli.commands import cmd_import
        cmd_import(rest[0] if rest else None)

    elif cmd in ("balance", "bs", "b"):
        from cli.commands import cmd_balance_sheet
        cmd_balance_sheet(rest[0] if rest else None)

    elif cmd in ("transactions", "tx", "t"):
        from cli.commands import cmd_transactions
        cmd_transactions(rest[0] if rest else None)

    elif cmd in ("classify", "c"):
        from cli.commands import cmd_classify
        cmd_classify()

    elif cmd in ("memory", "m"):
        from cli.commands import cmd_memory
        cmd_memory()

    elif cmd in ("summary", "mom"):
        from cli.commands import cmd_mom_summary
        cmd_mom_summary()

    elif cmd in ("setup", "init"):
        from cli.commands import cmd_setup
        cmd_setup()

    else:
        print_error(f"Unknown command: '{cmd}'")
        print_info("Usage: python main.py [import|balance|transactions|classify|memory|summary|setup]")
        sys.exit(1)


if __name__ == "__main__":
    main()
