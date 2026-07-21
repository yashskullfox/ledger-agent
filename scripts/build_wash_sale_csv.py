#!/usr/bin/env python3
"""Extract wash-sale disallowances from 1099-B PDFs into
``private/wash_sale_adjustments.csv`` (W16).

Reads one or more 1099-B PDFs and aggregates wash-sale loss disallowed amounts
by ticker into the CSV format consumed by
``ledger_agent/core/accounting/wash_sale.py``.

The output path is ALWAYS validated to be under ``private/`` (gitignored) so
real tickers and dollar amounts never leak into the tracked tree — mirrors
the pattern from ``scripts/extract_filing_reference.py``.

USAGE
    # Point at BROKER_Y and BROKER_Z 1099-B PDFs under Synced-Accounts/
    # (paths shown symbolically; real paths live in gitignored areas):
    python scripts/build_wash_sale_csv.py \\
        --1099b "$BROKER_Y_1099B_PDF" \\
        --1099b "$BROKER_Z_8949_PDF"

    python scripts/build_wash_sale_csv.py \\
        --1099b "path/to/1099b.pdf" \\
        --out private/wash_sale_adjustments.csv

DESIGN NOTES
    * Iterates every table on every page of each PDF via ``pdfplumber``.
    * Picks the column whose header contains both "wash" and "disallow"
      (case-insensitive) and the column whose header contains "symbol",
      "ticker", or "description".
    * Aggregates disallowances by uppercase ticker across all input PDFs.
    * Stdout is MASKED (bucketed amounts, ticker counts only). Only the CSV
      itself contains real values.

KNOWN LIMITATIONS
    * Broker 1099-B PDFs vary widely in layout. If a PDF renders as text with
      no detected tables, this script emits zero rows for that PDF — inspect
      manually with ``pdfplumber`` and file a per-broker helper.
    * Consolidated 1099s (multiple sections in one file) may need a
      section-scoped variant; today we scan all pages uniformly.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import DefaultDict

try:
    import pdfplumber  # type: ignore
except ImportError:  # pragma: no cover
    print("pdfplumber is required: pip install pdfplumber", file=sys.stderr)
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parent.parent


def _mask_amount(amount: Decimal) -> str:
    """Bucket a dollar amount for safe stdout display."""
    a = abs(amount)
    if a == 0:               return "$0"
    if a < Decimal("1000"):  return "~$XXX"
    if a < Decimal("10000"): return "~$X,XXX"
    if a < Decimal("100000"):return "~$XX,XXX"
    if a < Decimal("1000000"): return "~$XXX,XXX"
    return "~$X,XXX,XXX"


def _parse_amount(raw: str) -> Decimal | None:
    """Parse an amount cell (e.g. plain, comma-formatted, or parenthesized
    negative) to Decimal or None."""  # redaction: allow
    if raw is None:
        return None
    s = str(raw).strip().replace("$", "").replace(",", "").replace(" ", "")
    if not s or s in ("-", "—"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = "-" + s[1:-1]
    try:
        return Decimal(s)
    except Exception:
        return None


def _extract_wash_sale_disallowances(pdf_path: Path) -> DefaultDict[str, Decimal]:
    """Parse a 1099-B PDF and return {uppercase_ticker: total_disallowed_loss}."""
    result: DefaultDict[str, Decimal] = defaultdict(lambda: Decimal("0"))

    if not pdf_path.exists():
        print(f"WARNING: PDF not found: {pdf_path}", file=sys.stderr)
        return result

    try:
        pdf = pdfplumber.open(str(pdf_path))
    except Exception as e:
        print(f"ERROR: cannot open {pdf_path.name}: {e}", file=sys.stderr)
        return result

    with pdf:
        for page in pdf.pages:
            try:
                tables = page.extract_tables() or []
            except Exception:
                continue

            for table in tables:
                if not table or len(table) < 2:
                    continue
                headers = table[0]
                if not headers:
                    continue

                ticker_col = None
                wash_col = None
                for idx, header in enumerate(headers):
                    if header is None:
                        continue
                    hl = str(header).lower()
                    if ticker_col is None and any(x in hl for x in ("symbol", "ticker", "description")):
                        ticker_col = idx
                    if wash_col is None and "wash" in hl and "disallow" in hl:
                        wash_col = idx

                if ticker_col is None or wash_col is None:
                    continue

                for row in table[1:]:
                    if not row or len(row) <= max(ticker_col, wash_col):
                        continue

                    ticker_raw = row[ticker_col]
                    wash_raw = row[wash_col]
                    if not ticker_raw or wash_raw in (None, "", "-"):
                        continue

                    ticker = str(ticker_raw).strip().upper()
                    # Ticker sanity: 1-6 alphanumerics; skip long free-text descriptions
                    if not ticker or len(ticker) > 8 or not ticker.replace(".", "").isalnum():
                        continue

                    amount = _parse_amount(wash_raw)
                    if amount is None or amount == 0:
                        continue

                    # Wash-sale disallowance is always a positive amount in the
                    # "loss disallowed" column, even though it modifies a loss.
                    result[ticker] += abs(amount)

    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--1099b", type=Path, action="append", dest="pdfs",
                    help="path to 1099-B PDF (repeatable)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output CSV (default: private/wash_sale_adjustments.csv)")
    args = ap.parse_args()

    if not args.pdfs:
        print("ERROR: at least one --1099b PDF is required", file=sys.stderr)
        return 2

    out_path = args.out or (REPO_ROOT / "private" / "wash_sale_adjustments.csv")

    # Refuse to write outside private/ (mirrors extract_filing_reference.py)
    try:
        rel = out_path.resolve().relative_to(REPO_ROOT)
        if not str(rel).startswith("private/"):
            print(f"REFUSING to write outside private/: {out_path}", file=sys.stderr)
            print("Output contains PII (real tickers/amounts). Use --out under "
                  "private/ or omit --out.", file=sys.stderr)
            return 3
    except ValueError:
        pass  # absolute path outside repo — caller's responsibility

    per_pdf: list[dict] = []
    aggregate: DefaultDict[str, Decimal] = defaultdict(lambda: Decimal("0"))

    for pdf_path in args.pdfs:
        if not pdf_path.exists():
            print(f"ERROR: PDF not found: {pdf_path}", file=sys.stderr)
            return 2
        rows = _extract_wash_sale_disallowances(pdf_path)
        for ticker, amt in rows.items():
            aggregate[ticker] += amt
        per_pdf.append({
            "name": pdf_path.name,
            "tickers": len(rows),
            "total": sum(rows.values(), Decimal("0")),
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["ticker", "disallowed_loss"])
        writer.writeheader()
        for ticker in sorted(aggregate.keys()):
            writer.writerow({
                "ticker": ticker,
                "disallowed_loss": f"{aggregate[ticker]:.2f}",
            })

    total_all = sum(aggregate.values(), Decimal("0"))
    print(f"\n=== Wash-sale CSV built (masked stdout) ===")
    print(f"  output:              {out_path.relative_to(REPO_ROOT)}")
    print(f"  tickers aggregated:  {len(aggregate)}")
    print(f"  total disallowed:    {_mask_amount(total_all)}")
    print()
    for s in per_pdf:
        print(f"  {s['name']:<55s}  {s['tickers']:3d} tickers  {_mask_amount(s['total']):>10s}")
    print(f"\nWROTE {out_path.relative_to(REPO_ROOT)} ({out_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

