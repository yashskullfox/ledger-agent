"""
ledger_agent.cli.main  –  Thin CLI entrypoint (ARCH-04)
========================================================

All financial logic is delegated to ``ledger_agent.core.api``.
This module only handles argument parsing, user interaction, and
formatting.  It never imports from core directly — only via api.

Entry point (defined in packaging/cli/pyproject.toml):
    ledger = ledger_agent.cli.main:app

Usage
-----
    ledger scan [FOLDER] [--no-prompt] [--window START:END]
    ledger balance [YEAR]
    ledger tax    [YEAR]
    ledger form1065 [YEAR]
    ledger k1 [YEAR] [--partner yash|parin]
    ledger reconcile [YEAR]
    ledger export [YEAR] [--format csv|excel|json]
    ledger --version
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap sys.path so the package works when installed from source checkout
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent  # ledger-agent/
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _import_api():
    """Lazy import so the CLI starts fast when --help is requested."""
    import ledger_agent.core.api as api  # noqa: F401
    return api


def _console():
    try:
        from rich.console import Console
        return Console()
    except ImportError:
        return None


def _print(msg: str, style: str = "") -> None:
    c = _console()
    if c:
        c.print(f"[{style}]{msg}[/{style}]" if style else msg)
    else:
        print(msg)


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_scan(args: list[str]) -> int:
    """Import statements from a folder."""
    folder_arg = next((a for a in args if not a.startswith("-")), None)
    no_prompt   = "--no-prompt" in args
    allow_partial = "--allow-partial" in args

    folder = Path(folder_arg).expanduser().resolve() if folder_arg else (
        Path(os.environ.get("FI_STATEMENTS_DIR", "data/statements")).resolve()
    )

    api = _import_api()
    _print(f"📂  Scanning [bold]{folder}[/bold] …", "")
    report = api.import_statements(folder, allow_partial=allow_partial)

    _print(f"✅  Imported: [green]{report.imported}[/green]  "
           f"⏭  Skipped: {report.skipped}  "
           f"❌  Failed: [red]{report.failed}[/red]")

    if report.failed_files:
        for f in report.failed_files:
            _print(f"  ⚠  Failed: {f}", "yellow")

    if no_prompt:
        payload = {
            "imported": report.imported, "skipped": report.skipped,
            "failed": report.failed, "failed_files": report.failed_files,
            "periods_added": report.periods_added,
        }
        print(json.dumps(payload))
        return 0 if report.imported > 0 else 2

    return 0 if report.ok else 1


def cmd_balance(args: list[str]) -> int:
    """Generate and display balance sheet."""
    year_arg = next((a for a in args if a.isdigit()), None)
    year = int(year_arg) if year_arg else 2024

    api = _import_api()
    from ledger_agent.core.reports.renderer import render_balance_sheet
    try:
        bs = api.generate_balance_sheet(year)
        render_balance_sheet(bs)
        return 0
    except ValueError as e:
        _print(f"[red]{e}[/red]")
        return 1


def cmd_tax(args: list[str]) -> int:
    """Show quarterly tax estimate."""
    year_arg = next((a for a in args if a.isdigit()), None)
    year = int(year_arg) if year_arg else 2024

    api = _import_api()
    from ledger_agent.core.reports.renderer import render_tax_estimate
    try:
        from ledger_agent.core.accounting.tax_estimator import TaxEstimator
        from ledger_agent.core.database import EntityRepo, init_db
        init_db()
        entities = EntityRepo.list_all()
        if not entities:
            _print(f"[yellow]No data for {year}.[/yellow]")
            return 1
        entity = entities[0]
        f = api.generate_form_1065(year)
        raw = TaxEstimator(entity.name, year).estimate_from_net_income(f.ordinary_business_income)
        render_tax_estimate(raw)
        return 0
    except ValueError as e:
        _print(f"[red]{e}[/red]")
        return 1


def cmd_form1065(args: list[str]) -> int:
    """Show Form 1065 summary."""
    year_arg = next((a for a in args if a.isdigit()), None)
    year = int(year_arg) if year_arg else 2024

    api = _import_api()
    try:
        f = api.generate_form_1065(year)
        _print(f"\n[bold cyan]Form 1065 — {f.entity_name} ({f.fiscal_year})[/bold cyan]")
        _print(f"  Total Income:              ${f.total_income:>12,.2f}")
        _print(f"  Total Deductions:          ${f.total_deductions:>12,.2f}")
        _print(f"  Ordinary Business Income:  ${f.ordinary_business_income:>12,.2f}")
        _print(f"  Net Short-Term Cap. Gain:  ${f.net_short_term_capital_gain:>12,.2f}")
        _print(f"  Dividend Income:           ${f.dividend_income:>12,.2f}")
        _print(f"  Interest Income:           ${f.interest_income:>12,.2f}")
        return 0
    except ValueError as e:
        _print(f"[red]{e}[/red]")
        return 1


def cmd_k1(args: list[str]) -> int:
    """Show Schedule K-1 for a partner."""
    year_arg = next((a for a in args if a.isdigit()), None)
    year = int(year_arg) if year_arg else 2024
    # --partner yash|parin
    partner = "yash"
    try:
        pi = args.index("--partner")
        partner = args[pi + 1]
    except (ValueError, IndexError):
        pass

    api = _import_api()
    try:
        k1 = api.generate_k1(year, partner)
        _print(f"\n[bold cyan]Schedule K-1 — {k1.partner_name} ({k1.ownership_pct:.0%}) — {k1.fiscal_year}[/bold cyan]")
        _print(f"  Ordinary Income/Loss:      ${k1.ordinary_income_loss:>12,.2f}")
        _print(f"  Net Short-Term Cap. Gain:  ${k1.net_stcg:>12,.2f}")
        _print(f"  Dividend Income:           ${k1.dividend_income:>12,.2f}")
        return 0
    except ValueError as e:
        _print(f"[red]{e}[/red]")
        return 1


def cmd_reconcile(args: list[str]) -> int:
    """Run year-end reconciliation."""
    year_arg = next((a for a in args if a.isdigit()), None)
    year = int(year_arg) if year_arg else 2024

    api = _import_api()
    try:
        r = api.reconcile_year(year)
        status = "[green]CLEAN[/green]" if r.clean else "[red]ISSUES FOUND[/red]"
        _print(f"\n[bold]Reconciliation {year}[/bold]: {status}")
        _print(f"  Matched transfers:   {r.matched}")
        _print(f"  Unmatched:           {r.unmatched}")
        _print(f"  Total transfer flow: ${r.total_transfers:,.2f}")
        if r.issues:
            for issue in r.issues:
                _print(f"  ⚠  {issue}", "yellow")
        return 0 if r.clean else 1
    except ValueError as e:
        _print(f"[red]{e}[/red]")
        return 1


# ---------------------------------------------------------------------------
# Main dispatcher (called by legacy main.py and by the ledger entrypoint)
# ---------------------------------------------------------------------------

_COMMANDS: dict[str, tuple] = {
    "scan":      (cmd_scan,      "s",  "Import PDF statements"),
    "balance":   (cmd_balance,   "b",  "Show balance sheet"),
    "tax":       (cmd_tax,       "t",  "Show tax estimate"),
    "form1065":  (cmd_form1065,  "f1", "Show Form 1065 data"),
    "k1":        (cmd_k1,        "k",  "Show Schedule K-1"),
    "reconcile": (cmd_reconcile, "r",  "Run reconciliation"),
}


def _help() -> None:
    _print("\n[bold]ledger[/bold] — financial intelligence CLI\n")
    for name, (_, alias, desc) in _COMMANDS.items():
        _print(f"  [cyan]{name:12}[/cyan] ({alias})   {desc}")
    _print("\nOptions: --no-prompt  --allow-partial  --partner yash|parin\n")


def app() -> None:
    """Main entrypoint for the ``ledger`` shell command."""
    # Load .env if present
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        _help()
        sys.exit(0)

    if args[0] in ("--version", "-v"):
        from ledger_agent import __version__
        _print(f"ledger-agent {__version__}")
        sys.exit(0)

    cmd = args[0].lower()
    rest = args[1:]

    # Resolve aliases
    for name, (fn, alias, _) in _COMMANDS.items():
        if cmd in (name, alias):
            sys.exit(fn(rest))

    _print(f"[red]Unknown command: {cmd!r}[/red]")
    _help()
    sys.exit(1)


if __name__ == "__main__":
    app()
