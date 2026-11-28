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
    ledger tax    [YEAR] [--no-prompt]
    ledger form1065 [YEAR]
    ledger k1 [YEAR] [--partner partner_1|partner_2]
    ledger reconcile [YEAR]
    ledger audit  [YEAR] [--no-prompt]
    ledger export [YEAR] [--format csv|excel|json]
    ledger --version
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _load_dotenv_early() -> None:
    """Load ``.env`` (CWD first, then the repo root) if python-dotenv is installed.

    Must run before ``config`` is imported: ``ledger_agent.core.config`` reads
    ``FI_*`` variables at import time, so loading ``.env`` afterwards would be
    silently ignored. Never overrides variables already set in the environment.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return
    cwd_env = find_dotenv(usecwd=True)
    if cwd_env:
        load_dotenv(cwd_env, override=False)
    repo_env = Path(__file__).resolve().parent.parent.parent / ".env"
    if repo_env.is_file():
        load_dotenv(repo_env, override=False)


# Only meaningful while config is still unimported (i.e. a real CLI launch);
# when imported by tests or other modules, config has already been read.
if "ledger_agent.core.config" not in sys.modules:
    _load_dotenv_early()

from ledger_agent.core.config import NOT_TAX_ADVICE_DISCLAIMER, STATEMENTS_DIR  # noqa: E402

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
        Path(os.environ.get("FI_STATEMENTS_DIR") or STATEMENTS_DIR).expanduser().resolve()
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
    period = next((a for a in args if len(a) == 7 and a[:4].isdigit() and a[4] == "-" and a[5:].isdigit()), None)

    api = _import_api()
    from ledger_agent.core.reports.renderer import render_balance_sheet
    try:
        bs = api.generate_balance_sheet(year, period=period)
        render_balance_sheet(bs)
        _print(f"\n[dim]⚠️  {NOT_TAX_ADVICE_DISCLAIMER}[/dim]\n")
        return 0
    except ValueError as e:
        _print(f"[red]{e}[/red]")
        return 1


def cmd_tax(args: list[str]) -> int:
    """Show the entity-level PTET estimate (``api.pte_estimate``), or record an external
    PTE payment via ``--record-payment``.  ``--no-prompt`` prints the estimate as JSON."""
    year_arg = next((a for a in args if a.isdigit() and len(a) == 4), None)
    year = int(year_arg) if year_arg else 2024
    no_prompt = "--no-prompt" in args

    api = _import_api()
    if "--record-payment" in args:
        try:
            q_idx = args.index("--quarter")
            quarter = args[q_idx + 1]
            a_idx = args.index("--amount")
            amount = args[a_idx + 1]
        except (ValueError, IndexError):
            _print("[red]Usage: ledger tax <year> --record-payment --quarter Q1 --amount <amt> [--paid-on YYYY-MM-DD][/red]")
            return 1
        paid_on = None
        if "--paid-on" in args:
            try:
                paid_on = args[args.index("--paid-on") + 1]
            except IndexError:
                pass
        try:
            rec = api.record_pte_payment(year, quarter, amount, paid_on=paid_on)
            msg = (
                f"✓ Recorded PTE payment for FY{year} {rec['quarter']}: "
                f"${float(rec['amount']):,.2f}"
            )
            if no_prompt:
                print(msg, file=sys.stderr)  # stdout carries only the JSON estimate
            else:
                _print(f"[green]{msg}[/green]")
        except ValueError as e:
            _print(f"[red]{e}[/red]")
            return 1

    try:
        est = api.pte_estimate(year)
    except ValueError as e:
        _print(f"[red]{e}[/red]")
        return 1

    if no_prompt:
        import dataclasses
        print(json.dumps(dataclasses.asdict(est), indent=2, default=str))
        return 0

    from ledger_agent.core.reports.renderer import render_pte_estimate
    render_pte_estimate(est)  # basis line first; ends with the not-tax-advice disclaimer
    return 0


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
        _print(f"\n[dim]⚠️  {NOT_TAX_ADVICE_DISCLAIMER}[/dim]\n")
        return 0
    except ValueError as e:
        _print(f"[red]{e}[/red]")
        return 1


def cmd_k1(args: list[str]) -> int:
    """Show Schedule K-1 for a partner."""
    year_arg = next((a for a in args if a.isdigit()), None)
    year = int(year_arg) if year_arg else 2024
    # --partner partner_1|partner_2
    partner = "partner_1"
    try:
        pi = args.index("--partner")
        partner = args[pi + 1]
    except (ValueError, IndexError):
        pass

    api = _import_api()
    try:
        k1 = api.generate_k1(year, partner)
        _print(f"\n[bold cyan]Schedule K-1 — {k1.partner_name} ({k1.ownership_pct:.0%}) — {k1.fiscal_year}[/bold cyan]")  # redaction: allow (format specifier, not a real percent)
        _print(f"  Ordinary Income/Loss:      ${k1.ordinary_income_loss:>12,.2f}")
        _print(f"  Net Short-Term Cap. Gain:  ${k1.net_stcg:>12,.2f}")
        _print(f"  Dividend Income:           ${k1.dividend_income:>12,.2f}")
        _print(f"\n[dim]⚠️  {NOT_TAX_ADVICE_DISCLAIMER}[/dim]\n")
        return 0
    except ValueError as e:
        _print(f"[red]{e}[/red]")
        return 1


def cmd_summary(args: list[str]) -> int:
    """Print customer-readable outcome summary (W27 contract)."""
    year_arg = next((a for a in args if a.isdigit()), None)
    year = int(year_arg) if year_arg else 2024
    no_prompt = "--no-prompt" in args

    api = _import_api()
    try:
        s = api.build_customer_summary(year)
    except ValueError as e:
        _print(f"[red]{e}[/red]")
        return 1

    if no_prompt:
        import dataclasses
        print(json.dumps(dataclasses.asdict(s), indent=2, default=str))
        return 0

    # Human-readable output
    pl = s.profit_or_loss
    bs = s.balance_sheet_health
    pte = s.pte_due_signal
    gs = s.growth_signal

    pl_color = "green" if pl["signal"] == "positive" else (
        "red" if pl["signal"] == "negative" else "yellow"
    )
    _print(f"\n[bold cyan]Customer Outcome Summary — {s.fiscal_year}[/bold cyan]")
    _print(f"  Period covered:       {s.period_covered}")
    _print(f"  P/L status:           [{pl_color}]{pl['status'].upper()}[/{pl_color}]"
           f"  (OBI: {float(pl['ordinary_business_income']):>+12,.2f})")
    _print(f"  Growth signal:        {gs['status'].upper()} — {gs['note']}")
    _print(f"\n[bold]Balance Sheet[/bold] ({bs['period']}):")
    _print(f"  Total Assets:         {float(bs['total_assets']):>14,.2f}")
    _print(f"  Total Liabilities:    {float(bs['total_liabilities']):>14,.2f}")
    _print(f"  Total Equity:         {float(bs['total_equity']):>14,.2f}")
    bs_color = "green" if bs["status"] == "healthy" else "yellow"
    _print(f"  Health:               [{bs_color}]{bs['status'].upper()}[/{bs_color}]"
           + (f" ({bs['skipped_accounts']} account(s) missing snapshots)"
              if bs["skipped_accounts"] else ""))
    _print("\n[bold]PTE / Tax:[/bold]")
    pte_color = "yellow" if pte["status"] == "likely_due" else "green"
    _print(f"  PTE status:           [{pte_color}]{pte['status'].upper()}[/{pte_color}]"
           f"  (annual est: {float(pte['annual_estimate']):,.2f})")
    if float(pte.get("paid_ytd", 0)) > 0:
        _print(f"  PTE paid YTD:         {float(pte['paid_ytd']):,.2f}"
               f"  (remaining est: {float(pte['remaining_balance']):,.2f})")
    if pte["status"] == "likely_due":
        _print(f"  Next payment due:     {pte['next_due']}")
    # Q-01: period basis of the tax figures (same wording as ``ledger tax``).
    from ledger_agent.core.reports.renderer import pte_basis_line, pte_missing_statement_count
    _print(f"  {pte_basis_line(pte)}")
    if pte.get("basis", "full_year") != "full_year":
        # pte_due_signal carries no per-account detail; the count comes from the same
        # engine call the summary used (api.pte_estimate, default basis).
        try:
            n_missing = pte_missing_statement_count(api.pte_estimate(year))
        except ValueError:
            n_missing = None
        if n_missing is not None:
            _print(f"  [yellow]Missing statements:   {n_missing} account-month(s) — "
                   "import them for full-year figures.[/yellow]")
    _print("\n[bold]Confidence:[/bold]")
    for flag in s.confidence_flags:
        flag_color = "red" if flag.startswith("NOT_") else "green"
        _print(f"  [{flag_color}]{flag}[/{flag_color}]")
    _print("\n[bold]Next actions:[/bold]")
    for action in s.next_actions:
        _print(f"  • {action}")
    _print(f"\n[dim]⚠️  {NOT_TAX_ADVICE_DISCLAIMER}[/dim]\n")
    return 0


def cmd_audit(args: list[str]) -> int:
    """CPA-style self-audit of the year's figures (``api.audit_year``).

    Exit code 0 for pass/warn, 1 when the audit fails or cannot run.
    ``--no-prompt`` prints the audit report as JSON on stdout.
    """
    year_arg = next((a for a in args if a.isdigit() and len(a) == 4), None)
    year = int(year_arg) if year_arg else 2024
    no_prompt = "--no-prompt" in args

    api = _import_api()
    try:
        report = api.audit_year(year)
    except ValueError as e:
        _print(f"[red]{e}[/red]")
        return 1

    if no_prompt:
        print(json.dumps(report, indent=2, default=str))
        return 1 if report["status"] == "fail" else 0

    colors = {"pass": "green", "warn": "yellow", "fail": "red", "not_applicable": "dim"}
    overall = report["status"]
    oc = colors.get(overall, "")
    _print(f"\n[bold cyan]Self-audit — {report['fiscal_year']}[/bold cyan]: "
           f"[{oc}]{overall.upper()}[/{oc}]")
    for c in report["checks"]:
        cc = colors.get(c["status"], "")
        _print(f"  {c['name']:24} [{cc}]{c['status'].upper():15}[/{cc}] {c['summary']}")
    _print(f"\n[dim]⚠️  {NOT_TAX_ADVICE_DISCLAIMER}[/dim]\n")
    return 1 if overall == "fail" else 0


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
        _print(f"\n[dim]⚠️  {NOT_TAX_ADVICE_DISCLAIMER}[/dim]\n")
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
    "summary":   (cmd_summary,   "su", "Customer outcome summary (W27)"),
    "audit":     (cmd_audit,     "a",  "CPA-style self-audit of the year"),
}


def _help() -> None:
    _print("\n[bold]ledger[/bold] — financial intelligence CLI\n")
    for name, (_, alias, desc) in _COMMANDS.items():
        _print(f"  [cyan]{name:12}[/cyan] ({alias})   {desc}")
    _print("\nOptions: --no-prompt  --allow-partial  --partner partner_1|partner_2\n")


def app() -> None:
    """Main entrypoint for the ``ledger`` shell command."""
    # Load .env if present (no-op for keys already set; see _load_dotenv_early)
    _load_dotenv_early()

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
