"""
cli/prompts.py  –  Interactive user prompts (questionary + rich fallback)
──────────────────────────────────────────────────────────────────────────
All user interaction flows through this module so the rest of the codebase
remains I/O-free and testable.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from core.models import COAEntry, Transaction

try:
    import questionary
    from questionary import Style as QStyle

    _Q = True
    _STYLE = QStyle([
        ("qmark", "fg:#00bfff bold"),
        ("question", "fg:#ffffff bold"),
        ("answer", "fg:#00ff7f bold"),
        ("pointer", "fg:#00bfff bold"),
        ("highlighted", "fg:#00bfff bold"),
        ("selected", "fg:#00ff7f"),
    ])
except ImportError:
    _Q = False

try:
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    _RICH = True
except ImportError:
    _RICH = False
    console = None


def ask_text(prompt: str, default: str = "") -> str:
    if _Q:
        return questionary.text(prompt, default=default, style=_STYLE).ask() or default
    return input(f"{prompt} [{default}]: ").strip() or default


def ask_confirm(prompt: str, default: bool = True) -> bool:
    if _Q:
        return questionary.confirm(prompt, default=default, style=_STYLE).ask()
    ans = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return (ans in ("y", "yes", "")) if default else (ans in ("y", "yes"))


def ask_select(prompt: str, choices: list, default=None):
    if _Q:
        return questionary.select(prompt, choices=choices, default=default,
                                  style=_STYLE).ask()
    print(f"\n{prompt}")
    for i, c in enumerate(choices, 1):
        print(f"  {i}. {c}")
    idx = input("Enter number: ").strip()
    try:
        return choices[int(idx) - 1]
    except (ValueError, IndexError):
        return default


def ask_autocomplete(prompt: str, choices: List[str], default: str = "") -> str:
    if _Q:
        return questionary.autocomplete(
            prompt, choices=choices, default=default, style=_STYLE
        ).ask() or default
    return ask_text(prompt, default)


def wizard_entity() -> dict:
    """
    Guides the user through creating their entity record.
    Returns a dict with keys: name, entity_type, state, ein_masked, notes
    """
    if _RICH:
        console.print(Panel(
            "[bold cyan]Welcome to FinancialIntelligence![/bold cyan]\n\n"
            "Let's set up your entity profile first.\n"
            "This only runs once and is stored locally.",
            title="First-Run Setup", border_style="cyan",
        ))

    name = ask_text("Entity name (e.g. MY COMPANY LLC)", default="")

    entity_type = ask_select(
        "Entity type",
        choices=["LLC", "S-Corp", "C-Corp", "Sole Proprietor", "Partnership", "Other"],
        default="LLC",
    )

    state = ask_text("State of formation (2-letter)", default="MO").upper()[:2]

    ein_raw = ask_text(
        "EIN (optional – enter for record, stored as masked)", default=""
    )
    ein_masked = ""
    if ein_raw.strip():
        digits = "".join(c for c in ein_raw if c.isdigit())
        ein_masked = f"**-***{digits[-4:]}" if len(digits) >= 4 else "**-*****"

    notes = ask_text("Any notes about this entity?", default="")

    return {
        "name": name,
        "entity_type": entity_type,
        "state": state,
        "ein_masked": ein_masked,
        "notes": notes,
    }


def prompt_classify(
        txn: Transaction,
        coa_entries: List[COAEntry],
) -> Optional[Tuple[str, str, bool]]:
    """
    Ask the user to classify a transaction.

    Returns (coa_code, coa_name, is_transfer) or None if user skips.
    """
    if _RICH:
        console.print(
            f"\n[bold yellow]⚡ Classify transaction[/bold yellow]\n"
            f"  Date        : [cyan]{txn.date}[/cyan]\n"
            f"  Description : [white]{txn.description}[/white]\n"
            f"  Amount      : [{'green' if txn.amount >= 0 else 'red'}]"
            f"${txn.amount:,.2f}[/]\n"
        )
    else:
        print(f"\n--- Classify: {txn.date}  ${txn.amount:,.2f}  {txn.description}")

    # Build choice list from leaf COA entries
    leaves = [e for e in coa_entries if e.parent_code is not None]
    choices = [f"{e.code}  {e.name}" for e in leaves] + ["[SKIP] – classify later"]

    chosen = ask_autocomplete(
        "Select COA category (type to search):",
        choices=choices,
        default="",
    )

    if not chosen or chosen.startswith("[SKIP]"):
        return None

    # Extract code from chosen string
    code = chosen.split()[0].strip()
    entry = next((e for e in leaves if e.code == code), None)
    if entry is None:
        return None

    is_transfer = ask_confirm(
        "Is this an inter-account transfer (exclude from P&L)?",
        default=False,
    )

    return entry.code, entry.name, is_transfer


def prompt_statement_file(statements_dir) -> Optional[str]:
    """Ask user to pick a PDF from the statements dir or enter a path."""
    from pathlib import Path
    pdfs = list(Path(statements_dir).glob("*.pdf"))

    if pdfs:
        choices = [str(p.name) for p in pdfs] + ["[ Enter custom path ]"]
        sel = ask_select("Select statement PDF to import:", choices=choices)
        if sel and sel != "[ Enter custom path ]":
            return str(Path(statements_dir) / sel)

    path = ask_text("Enter full path to statement PDF:")
    return path.strip() if path.strip() else None


def print_success(msg: str) -> None:
    if _RICH:
        console.print(f"[bold green]✓[/bold green] {msg}")
    else:
        print(f"✓ {msg}")


def print_warning(msg: str) -> None:
    if _RICH:
        console.print(f"[bold yellow]⚠[/bold yellow] {msg}")
    else:
        print(f"⚠ {msg}")


def print_error(msg: str) -> None:
    if _RICH:
        console.print(f"[bold red]✗[/bold red] {msg}")
    else:
        print(f"✗ {msg}")


def print_info(msg: str) -> None:
    if _RICH:
        console.print(f"[dim]{msg}[/dim]")
    else:
        print(msg)
