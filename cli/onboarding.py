"""
cli/onboarding.py  –  12-Month Statement Auto-Discovery & Gap-Filling (R-45)

Zero-config onboarding: discover the last 12 months of statements from a
folder, render a coverage matrix (✅ present / ⚠ duplicate / ❌ missing),
interactively prompt for any missing months, then ingest all found PDFs.

Usage:
    python main.py onboard [FOLDER] [--window YYYY-MM:YYYY-MM] [--no-prompt]
    python main.py scan    [FOLDER] [--force] [--no-prompt]
    ./run.sh onboard [FOLDER]
    ./run.sh scan    [FOLDER]

Non-interactive (CI/script):
    ./run.sh scan FOLDER --no-prompt < /dev/null
    → emits JSON coverage matrix to stdout; exit 0 if complete, 2 if gaps remain.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.logging_setup import get_logger

log = get_logger(__name__)

# Type aliases
AccountKey = str                              # "{institution}|{last4}"
CoverageMap = Dict[Tuple[AccountKey, str], List[Path]]   # (acct_key, period) → paths

_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_MAP = {a.lower(): i + 1 for i, a in enumerate(_MONTH_ABBR)}


# ── Window helpers ─────────────────────────────────────────────────────────────

def rolling_window(n: int = 12) -> List[str]:
    """Return list of n 'YYYY-MM' strings ending at the last complete month."""
    today = date.today()
    last_day = today.replace(day=1) - timedelta(days=1)   # last day of prev month
    months: List[str] = []
    y, m = last_day.year, last_day.month
    for _ in range(n):
        months.append(f"{y}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(months))


def parse_window_arg(raw: str) -> List[str]:
    """Parse '--window 2025-01:2025-12' into a list of YYYY-MM strings."""
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid --window: {raw!r}  expected 'YYYY-MM:YYYY-MM'")

    def _ym(s: str) -> Tuple[int, int]:
        mo = re.match(r"(\d{4})-(\d{2})", s.strip())
        if not mo:
            raise ValueError(f"Invalid period: {s!r}")
        y, m = int(mo.group(1)), int(mo.group(2))
        if not (1 <= m <= 12):
            raise ValueError(f"Invalid month {m} in period: {s!r}")
        return y, m

    sy, sm = _ym(parts[0])
    ey, em = _ym(parts[1])
    months: List[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        months.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


# ── PDF text helpers ───────────────────────────────────────────────────────────

def _period_from_text(text: str) -> Optional[str]:
    """Best-effort YYYY-MM extraction from raw PDF text."""
    # MM/DD/YYYY  (most common in US bank statements)
    for mo in re.finditer(r"\b(\d{2})/(\d{2})/(\d{4})\b", text):
        mon, yr = int(mo.group(1)), int(mo.group(3))
        if 2020 <= yr <= 2035 and 1 <= mon <= 12:
            return f"{yr}-{mon:02d}"

    _MONTH_PAT = (
        r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?"
    )

    # Month D(D), YYYY  (e.g. "January 31, 2025", "March 1, 2026")
    for mo in re.finditer(
        rf"\b({_MONTH_PAT})\s+\d{{1,2}},?\s+(\d{{4}})\b",
        text, re.IGNORECASE,
    ):
        key = mo.group(1)[:3].lower()
        yr = int(mo.group(2))
        if 2020 <= yr <= 2035:
            return f"{yr}-{_MONTH_MAP[key]:02d}"

    # Month YYYY  (e.g. "January 2025", "Mar 2026")
    for mo in re.finditer(
        rf"\b({_MONTH_PAT})\s+(\d{{4}})\b",
        text, re.IGNORECASE,
    ):
        key = mo.group(1)[:3].lower()
        yr = int(mo.group(2))
        if 2020 <= yr <= 2035:
            return f"{yr}-{_MONTH_MAP[key]:02d}"

    return None


def _account_last4_from_text(text: str) -> str:
    """Extract masked last-4-digit account identifier from raw text."""
    patterns = [
        r"\*{2,}(\d{4})\b",                              # ****1234
        r"\.{3}(\d{4})\b",                               # ...1234
        r"\(\.{3}(\d{4})\)",                             # (...1234)
        r"\(\*{4}(\d{4})\)",                             # (****1234)
        r"Account\s+(?:Ending\s+in\s+)?(?:#|No\.?)?\s*(\d{4})\s*$",
        r"\b\d{4}\s+\d{4}\s+\d{4}\s+(\d{4})\b",        # card number groups
    ]
    for pat in patterns:
        mo = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if mo:
            return mo.group(1)
    return "0000"


# ── Discovery cache ────────────────────────────────────────────────────────────

def _cache_key(pdf_path: Path) -> str:
    st = pdf_path.stat()
    return f"{pdf_path}|{st.st_mtime:.3f}|{st.st_size}"


def load_discovery_cache(cache_path: Path) -> dict:
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except Exception:
            pass
    return {}


def save_discovery_cache(cache_path: Path, cache: dict) -> None:
    try:
        cache_path.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


# ── PDF probing ────────────────────────────────────────────────────────────────

def probe_pdf(pdf_path: Path, cache: dict) -> Optional[dict]:
    """
    Return {parser_id, institution, account_last4, period} for a PDF.
    Returns None if the file is not recognised by any parser.
    Uses the discovery cache to avoid re-parsing unchanged files.
    """
    key = _cache_key(pdf_path)
    if key in cache:
        cached = cache[key]
        # None sentinel means previously unrecognised
        return cached if isinstance(cached, dict) else None

    import parsers  # noqa: F401 – triggers auto-discovery
    from parsers.base import BaseStatementParser
    from parsers.registry import ParserRegistry

    try:
        text = BaseStatementParser.extract_text(pdf_path)
    except Exception as exc:
        log.debug("Text extraction failed", extra={"file": str(pdf_path), "error": str(exc)})
        cache[key] = None
        return None

    parser_cls = ParserRegistry.detect(text)
    if not parser_cls:
        cache[key] = None
        return None

    period = _period_from_text(text)
    last4 = _account_last4_from_text(text)

    result = {
        "parser_id": parser_cls.PARSER_ID,
        "institution": parser_cls.INSTITUTION,
        "account_last4": last4,
        "period": period or "0000-00",
    }
    cache[key] = result
    return result


def discover_folder(
    folder: Path,
    glob_pattern: str,
    cache: dict,
) -> List[Tuple[Path, dict]]:
    """
    Recursively find PDFs under folder, probe each, return [(path, info), ...].
    Unrecognised or unparseable files are silently skipped.
    """
    pdfs = sorted(set(folder.rglob(glob_pattern)) | set(folder.rglob("*.PDF")))
    results: List[Tuple[Path, dict]] = []
    for pdf in pdfs:
        info = probe_pdf(pdf, cache)
        if info:
            results.append((pdf, info))
    return results


# ── Coverage matrix ────────────────────────────────────────────────────────────

def build_coverage(
    discovered: List[Tuple[Path, dict]],
    window: List[str],
) -> Tuple[dict, CoverageMap]:
    """
    Build account registry and coverage map from discovered PDFs.

    Returns:
      accounts  – {account_key: {institution, last4, parser_id}}
      coverage  – {(account_key, period): [Path, ...]}
    """
    accounts: dict = {}
    coverage: CoverageMap = {}

    for pdf_path, info in discovered:
        key = f"{info['institution']}|{info['account_last4']}"
        if key not in accounts:
            accounts[key] = {
                "institution": info["institution"],
                "last4": info["account_last4"],
                "parser_id": info["parser_id"],
            }
        cell = (key, info["period"])
        coverage.setdefault(cell, []).append(pdf_path)

    return accounts, coverage


def render_coverage_matrix(
    accounts: dict,
    coverage: CoverageMap,
    window: List[str],
) -> None:
    """Print the coverage matrix table using Rich (fallback to plaintext)."""
    col_labels = [
        f"{_MONTH_ABBR[int(ym[5:]) - 1]}\n{ym[:4]}"
        for ym in window
    ]

    try:
        from rich.console import Console
        from rich.table import Table
        from rich import box

        console = Console()
        tbl = Table(
            title=f"📅  Statement Coverage — {window[0]} → {window[-1]}",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold cyan",
        )
        tbl.add_column("Account", style="bold white", no_wrap=True, min_width=28)
        for lbl in col_labels:
            tbl.add_column(lbl, justify="center", width=5)

        for acct_key, info in accounts.items():
            label = f"{info['institution']}\n****{info['last4']}"
            cells = []
            for ym in window:
                paths = coverage.get((acct_key, ym), [])
                if not paths:
                    cells.append("[red]❌[/red]")
                elif len(paths) == 1:
                    cells.append("[green]✅[/green]")
                else:
                    cells.append(f"[yellow]⚠x{len(paths)}[/yellow]")
            tbl.add_row(label, *cells)

        console.print(tbl)
        return
    except ImportError:
        pass

    # Plain-text fallback
    print(f"\n=== Statement Coverage ({window[0]} → {window[-1]}) ===")
    hdr = f"{'Account':<36}" + "".join(f"{l.replace(chr(10),' '):<7}" for l in col_labels)
    print(hdr)
    print("-" * len(hdr))
    for acct_key, info in accounts.items():
        lbl = f"{info['institution']} ****{info['last4']}"
        row = f"{lbl:<36}"
        for ym in window:
            paths = coverage.get((acct_key, ym), [])
            row += ("✅ " if len(paths) == 1 else ("⚠  " if paths else "❌ "))
        print(row)
    print()


def _summary_stats(accounts: dict, coverage: CoverageMap, window: List[str]) -> dict:
    total = len(accounts) * len(window)
    present = sum(
        1 for ak in accounts for ym in window
        if coverage.get((ak, ym))
    )
    missing = total - present
    duplicates = sum(1 for paths in coverage.values() if len(paths) > 1)
    return {
        "total_cells": total,
        "present": present,
        "missing": missing,
        "duplicates": duplicates,
        "accounts": len(accounts),
        "months": len(window),
    }


def _print_summary(stats: dict) -> None:
    msg = (
        f"Found {stats['present']} of {stats['total_cells']} cells present "
        f"({stats['accounts']} account(s) × {stats['months']} months)"
    )
    missing_msg = f"  ❌ {stats['missing']} cell(s) missing" if stats["missing"] else "  ✅ All months present"
    dup_msg = f"  ⚠  {stats['duplicates']} duplicate(s)" if stats["duplicates"] else ""

    try:
        from rich.console import Console
        c = Console()
        c.print(f"\n{msg}")
        c.print(f"[{'red' if stats['missing'] else 'green'}]{missing_msg}[/{'red' if stats['missing'] else 'green'}]")
        if dup_msg:
            c.print(f"[yellow]{dup_msg}[/yellow]")
    except ImportError:
        print(f"\n{msg}")
        print(missing_msg)
        if dup_msg:
            print(dup_msg)


# ── Gap-filling interaction ────────────────────────────────────────────────────

def _filename_hint(info: dict, period: str) -> str:
    """Suggested filename pattern for a missing statement."""
    slug = re.sub(r"[^a-z0-9]+", "_", info["institution"].lower()).strip("_")
    return f"{slug}_*_{period}*.pdf"


def gap_fill_interactive(
    accounts: dict,
    coverage: CoverageMap,
    window: List[str],
    folder: Path,
    cache: dict,
) -> bool:
    """
    Prompt user for each missing (account, month) cell.
    Returns True to proceed with ingestion, False to abort.
    """
    missing = [
        (ak, ym)
        for ak in accounts
        for ym in window
        if not coverage.get((ak, ym))
    ]
    if not missing:
        return True

    try:
        from rich.console import Console
        console = Console()

        def _p(msg: str) -> None:
            console.print(msg)
    except ImportError:
        def _p(msg: str) -> None:  # type: ignore[misc]
            print(re.sub(r"\[/?[^\]]+\]", "", msg))

    idx = 0
    while idx < len(missing):
        ak, ym = missing[idx]
        info = accounts[ak]
        hint = _filename_hint(info, ym)

        _p(f"\n[bold yellow]Missing:[/bold yellow] "
           f"{info['institution']} (****{info['last4']}) — {ym}")
        _p(f"  Expected filename: [dim]{hint}[/dim]")
        _p(f"  Drop into:         [dim]{folder}[/dim]")
        _p("  [bold][w][/bold] Wait  "
           "[bold][s][/bold] Skip  "
           "[bold][a][/bold] Skip all  "
           "[bold][q][/bold] Quit")

        try:
            choice = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "q"

        if choice == "w":
            _p(f"  [dim]Watching {folder} for up to 5 min (poll every 2 s)…[/dim]")
            filled = _poll_for_drop(folder, ak, ym, accounts, coverage, cache)
            if filled:
                _p(f"  [green]✓ Detected! {ym} is now present.[/green]")
                missing.pop(idx)
                render_coverage_matrix(accounts, coverage, window)
            else:
                _p("  [yellow]⏰ Timed out. Skipping this month.[/yellow]")
                idx += 1
        elif choice == "s":
            idx += 1
        elif choice == "a":
            _p("[dim]Skipping all remaining gaps — proceeding with available statements.[/dim]")
            break
        elif choice == "q":
            _p("[dim]Exiting. Re-run once you have the missing statements.[/dim]")
            return False
        else:
            _p(f"  Unknown option '{choice}'. Enter w / s / a / q.")

    return True


def _poll_for_drop(
    folder: Path,
    target_key: str,
    target_period: str,
    accounts: dict,
    coverage: CoverageMap,
    cache: dict,
    timeout_s: int = 300,
    interval_s: float = 2.0,
) -> bool:
    """Poll folder every interval_s seconds until a matching PDF appears or timeout."""
    import parsers  # noqa: F401

    known: set = set(folder.rglob("*.pdf")) | set(folder.rglob("*.PDF"))
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        time.sleep(interval_s)
        current = set(folder.rglob("*.pdf")) | set(folder.rglob("*.PDF"))
        new_files = current - known

        for new_pdf in sorted(new_files):
            # Remove stale cache entry so fresh probe runs
            stale_key = f"{new_pdf}|"
            for ck in list(cache.keys()):
                if ck.startswith(str(new_pdf)):
                    del cache[ck]

            info = probe_pdf(new_pdf, cache)
            if not info:
                continue
            ak = f"{info['institution']}|{info['account_last4']}"
            if ak == target_key and info["period"] == target_period:
                coverage.setdefault((ak, target_period), []).append(new_pdf)
                return True

        known = current

    return False


# ── CI / non-interactive output ────────────────────────────────────────────────

def emit_coverage_json(
    accounts: dict,
    coverage: CoverageMap,
    window: List[str],
) -> dict:
    """Serialise coverage matrix as JSON (for --no-prompt / CI mode)."""
    matrix: dict = {}
    for ak, info in accounts.items():
        label = f"{info['institution']} ****{info['last4']}"
        matrix[label] = {
            ym: (
                "present" if len(coverage.get((ak, ym), [])) == 1
                else "duplicate" if len(coverage.get((ak, ym), [])) > 1
                else "missing"
            )
            for ym in window
        }
    stats = _summary_stats(accounts, coverage, window)
    return {
        "window": window,
        "accounts": [
            {"key": ak, "institution": v["institution"], "last4": v["last4"]}
            for ak, v in accounts.items()
        ],
        "matrix": matrix,
        "missing_count": stats["missing"],
        "complete": stats["missing"] == 0,
    }


# ── Folder resolution & persistence ───────────────────────────────────────────

def _load_last_dir(data_dir: Path) -> Optional[Path]:
    marker = data_dir / ".last_statements_dir"
    if marker.exists():
        try:
            raw = marker.read_text().strip()
            if raw:
                return Path(raw)
        except Exception:
            pass
    return None


def _save_last_dir(data_dir: Path, folder: Path) -> None:
    try:
        (data_dir / ".last_statements_dir").write_text(str(folder))
    except Exception:
        pass


def resolve_folder(
    cli_arg: Optional[str],
    data_dir: Path,
    statements_dir: Path,
) -> Path:
    """
    Resolve the statements folder with this precedence:
    1. CLI positional arg
    2. FI_STATEMENTS_DIR environment variable
    3. Last-used path cached in data/.last_statements_dir
    4. Default data/statements/ (if non-empty)
    5. Interactive prompt (TTY only)
    6. Default data/statements/ (fallback / create)
    """
    if cli_arg:
        return Path(cli_arg).expanduser().resolve()

    env = os.environ.get("FI_STATEMENTS_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()

    last = _load_last_dir(data_dir)
    if last and last.is_dir():
        return last

    if statements_dir.is_dir() and any(
        True for _ in statements_dir.rglob("*.pdf")
    ):
        return statements_dir

    if sys.stdin.isatty():
        try:
            raw = input(
                f"\nWhere are your statement PDFs?\n"
                f"[Enter path or press Enter to use {statements_dir}]: "
            ).strip()
            if raw:
                return Path(raw).expanduser().resolve()
        except (EOFError, KeyboardInterrupt):
            pass

    return statements_dir


# ── Ingestion helper ───────────────────────────────────────────────────────────

def _ingest_discovered(
    discovered: List[Tuple[Path, dict]],
    force: bool = False,
) -> Tuple[int, int, List[str]]:
    """
    Import every discovered PDF through the existing import pipeline.
    Returns (imported_count, skipped_count, failed_names).
    """
    from core.database import init_db
    from cli.commands import _get_or_setup_entity
    from cli.prompts import print_info, print_warning, print_error, print_success, prompt_classify
    from cli.quick_scan import _import_single, _AlreadyImportedSkip
    from core.exceptions import ParserNotFoundError

    init_db()
    entity = _get_or_setup_entity()
    if not entity:
        print_error("No entity configured. Run: ./run.sh setup")
        return 0, 0, []

    imported = skipped = 0
    failed: List[str] = []

    for pdf_path, _info in discovered:
        print_info(f"  📄 {pdf_path.name}")
        try:
            _import_single(
                pdf_path=pdf_path,
                entity=entity,
                force=force,
                prompt_classify_fn=prompt_classify,
            )
            imported += 1
        except _AlreadyImportedSkip:
            skipped += 1
        except ParserNotFoundError as exc:
            print_warning(f"     ⚠  No parser: {exc}")
            failed.append(pdf_path.name)
        except Exception as exc:
            log.exception("Import failed", extra={"file": str(pdf_path)})
            print_error(f"     ✗  {exc}")
            failed.append(pdf_path.name)

    try:
        from cli.prompts import print_success as ps
        ps(
            f"\n✅ Imported: {imported}  |  "
            f"⏭ Already imported (skipped): {skipped}"
            + (f"  |  ❌ Failed: {len(failed)}" if failed else "")
        )
    except Exception:
        print(f"\nImported: {imported}  Skipped: {skipped}  Failed: {len(failed)}")

    if failed:
        print_warning("Failed: " + ", ".join(failed))

    return imported, skipped, failed


# ── Main command ───────────────────────────────────────────────────────────────

def cmd_onboard(
    folder: Optional[str] = None,
    window_arg: Optional[str] = None,
    no_prompt: bool = False,
    force: bool = False,
    show_report: bool = False,
) -> int:
    """
    R-45 entry point: discover → matrix → gap-fill → ingest.

    Args:
        folder:      Path to folder containing PDFs (None = auto-resolve).
        window_arg:  'YYYY-MM:YYYY-MM' override for the 12-month window.
        no_prompt:   Skip gap-fill loop; emit JSON matrix; CI-safe.
        force:       Re-import already-imported statements.
        show_report: After ingestion, render balance sheet + tax estimate.

    Returns:
        0  – complete (no gaps) or all gaps filled.
        2  – gaps remain (non-interactive / user skipped).
    """
    from config import DATA_DIR, STATEMENTS_DIR

    try:
        from rich.console import Console
        from rich.panel import Panel
        Console().print(Panel(
            "[bold cyan]📅  Statement Coverage Wizard[/bold cyan]\n"
            "[dim]Discover 12 months of statements, find gaps, then import[/dim]",
            border_style="cyan",
        ))
    except ImportError:
        print("\n=== Statement Coverage Wizard (R-45) ===\n")

    # ── 1. Resolve folder
    scan_dir = resolve_folder(folder, DATA_DIR, STATEMENTS_DIR)
    if not scan_dir.is_dir():
        scan_dir.mkdir(parents=True, exist_ok=True)
        print(f"  📁 Created: {scan_dir}")
    _save_last_dir(DATA_DIR, scan_dir)

    # ── 2. Build window
    window = parse_window_arg(window_arg) if window_arg else rolling_window(12)

    print(f"\n🔍  Scanning: {scan_dir}")
    print(f"📆  Window:   {window[0]} → {window[-1]}\n")

    # ── 3. Discover PDFs
    glob_pat = os.environ.get("FI_STATEMENT_GLOB", "*.pdf")
    cache_path = DATA_DIR / ".discovery_cache.json"
    cache = load_discovery_cache(cache_path)

    discovered = discover_folder(scan_dir, glob_pat, cache)
    save_discovery_cache(cache_path, cache)

    if not discovered:
        _print_no_pdfs(scan_dir, no_prompt)
        return 2

    # ── 4. Build coverage
    accounts, coverage = build_coverage(discovered, window)

    if not accounts:
        print("⚠  No PDFs were recognised by any parser.")
        print("   Supported: Truist, Fidelity, Chase, BofA, U.S. Bank, IBKR")
        return 2

    # ── 5. Render matrix
    render_coverage_matrix(accounts, coverage, window)
    stats = _summary_stats(accounts, coverage, window)
    _print_summary(stats)

    # ── 6. Non-interactive mode (CI / piped stdin)
    is_interactive = sys.stdin.isatty() and not no_prompt
    if not is_interactive:
        cov_json = emit_coverage_json(accounts, coverage, window)
        sys.stdout.write(json.dumps(cov_json, indent=2) + "\n")
        sys.stdout.flush()
        # Proceed to ingest what we have
        _ingest_discovered(discovered, force=force)
        return 0 if cov_json["complete"] else 2

    # ── 7. Interactive gap-filling
    if stats["missing"] > 0:
        proceed = gap_fill_interactive(accounts, coverage, window, scan_dir, cache)
        save_discovery_cache(cache_path, cache)
        if not proceed:
            return 2
        # Re-discover after gap-filling (new files may have been dropped)
        discovered = discover_folder(scan_dir, glob_pat, cache)
        save_discovery_cache(cache_path, cache)

    # ── 8. Ingest
    print("\n📥  Ingesting statements…")
    imported, skipped, _ = _ingest_discovered(discovered, force=force)

    # ── 9. Optional post-import report
    if show_report and (imported + skipped) > 0:
        _show_post_import_report()

    return 0


def _print_no_pdfs(scan_dir: Path, no_prompt: bool) -> None:
    print(f"⚠  No PDF statements found in: {scan_dir}")
    print(f"   Drop your bank statement PDFs into: {scan_dir}")
    if sys.stdin.isatty() and not no_prompt:
        try:
            input("\n  Press Enter to exit and try again after adding files…")
        except (EOFError, KeyboardInterrupt):
            pass


def _show_post_import_report() -> None:
    """After ingestion, render balance sheet + tax for the most recent period."""
    try:
        from core.database import SnapshotRepo, init_db
        from cli.commands import _get_or_setup_entity
        from accounting.balance_sheet import BalanceSheetBuilder
        from reports.renderer import render_balance_sheet
        from accounting.tax_estimator import TaxEstimator, render_tax_estimate

        init_db()
        entity = _get_or_setup_entity()
        if not entity:
            return
        snapshots = SnapshotRepo.list_for_entity(entity.id)
        periods = sorted({s.statement_period for s in snapshots}, reverse=True)
        if not periods:
            return
        period = periods[0]
        bs = BalanceSheetBuilder(entity.id, period).build()
        render_balance_sheet(bs)
        est = TaxEstimator(entity.name, int(period[:4])).estimate_from_balance_sheet(bs)
        render_tax_estimate(est)
    except Exception as exc:
        log.warning("Post-import report failed", extra={"error": str(exc)})
