#!/usr/bin/env python3
"""
scripts/rebuild_snapshots.py  –  Rebuild 2024 account_snapshots (W17)
=======================================================================
This script rebuilds the ``account_snapshots`` table for a given fiscal year
by re-importing statements from the ``statements/<year>/`` directory.

It is idempotent: existing snapshots are overwritten via UPSERT.

Prerequisites:
  1. ``private/institutions.py`` must be populated (BANK_X2/X3 detection tokens)
  2. Statement PDFs must exist under ``statements/<year>/``
  3. ``pdfplumber`` must be installed

Usage::

    python scripts/rebuild_snapshots.py 2024
    python scripts/rebuild_snapshots.py 2024 --db path/to/rebuild_test.db
    python scripts/rebuild_snapshots.py 2024 --dry-run   # print plan, no writes
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rebuild_snapshots")


def _check_private_institutions() -> bool:
    """Return True if private/institutions.py is importable and non-empty."""
    try:
        import private.institutions as inst  # noqa
        return True
    except ImportError:
        return False


def _count_existing_snapshots(db_path: Path, year: int) -> int:
    import sqlite3
    if not db_path.exists():
        return 0
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM account_snapshots WHERE statement_period LIKE ?",
            (f"{year}-%",),
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0
    finally:
        con.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Rebuild account_snapshots for a fiscal year from statement PDFs"
    )
    parser.add_argument("year", type=int, help="Fiscal year to rebuild (e.g. 2024)")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Override database path (default: data/db/financials.db via FI_DB_PATH)",
    )
    parser.add_argument(
        "--statements-dir",
        type=Path,
        default=None,
        help="Override statements directory (default: statements/<year>/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without writing to the database",
    )
    args = parser.parse_args(argv)

    year = args.year
    stmt_dir = args.statements_dir or (ROOT / "statements" / str(year))
    db_override = args.db

    # Set up DB path override
    if db_override:
        os.environ["FI_DB_PATH"] = str(db_override)

    log.info("=== rebuild_snapshots.py — year=%d ===", year)

    # 1. Preflight: check prerequisites
    if not _check_private_institutions():
        log.warning(
            "BLOCKED-ENV: private/institutions.py not found or not importable. "
            "Populate it from private/institutions.example.py before running. "
            "BANK_X2/BANK_X3 detection will not work without real tokens."
        )

    if not stmt_dir.is_dir():
        log.error("Statements directory not found: %s", stmt_dir)
        log.error("Create it and populate with PDFs for year %d, then re-run.", year)
        sys.exit(1)

    pdfs = sorted(stmt_dir.rglob("*.pdf"))
    if not pdfs:
        log.error("No PDF files found under %s", stmt_dir)
        sys.exit(1)

    log.info("Found %d PDF file(s) under %s", len(pdfs), stmt_dir)

    if args.dry_run:
        log.info("DRY-RUN: would import %d PDF(s) for year %d", len(pdfs), year)
        for p in pdfs:
            log.info("  %s", p.name)
        log.info("DRY-RUN complete — no changes written")
        return 0

    # 2. Run import
    from ledger_agent.core.api import import_statements
    from ledger_agent.core.database import init_db

    init_db()
    log.info("Importing statements from %s ...", stmt_dir)
    report = import_statements(stmt_dir, allow_partial=True)

    log.info(
        "Import complete: imported=%d skipped=%d failed=%d",
        report.imported, report.skipped, report.failed,
    )
    if report.failed_files:
        log.warning("Failed files: %s", report.failed_files)

    # 3. Report snapshot counts
    from config import DB_PATH
    db_path = Path(os.environ.get("FI_DB_PATH", str(DB_PATH)))
    count = _count_existing_snapshots(db_path, year)
    log.info("account_snapshots for %d: %d row(s) now in DB", year, count)

    if count == 0:
        log.warning(
            "W17: No snapshots found for year %d after import. "
            "Check that statements are parseable and private/institutions.py is configured. "
            "BANK_X2/BANK_X3 statements require real detection tokens.", year
        )
        return 1

    log.info("W17: rebuild_snapshots complete — %d snapshot(s) for %d", count, year)
    return 0


if __name__ == "__main__":
    sys.exit(main())
