#!/usr/bin/env python3
"""
scripts/regen_parity_corpus.py  –  Regenerate 2024 CPA parity fixture (ARCH-21)
=================================================================================

Reads the CPA corpus file (key=value plain text) and emits the structured JSON
fixture consumed by ``tests/integration/test_2024_cpa_parity.py``.

Usage
-----
    # Default paths (corpus → fixture):
    python scripts/regen_parity_corpus.py

    # Override corpus source:
    FI_CPA_CORPUS_PATH=/path/to/actuals.txt python scripts/regen_parity_corpus.py

    # Override output path:
    python scripts/regen_parity_corpus.py --out /tmp/fixture.json

    # Dry-run (print to stdout only):
    python scripts/regen_parity_corpus.py --dry-run

Corpus format
-------------
Plain text, one ``key=value`` pair per line.  Lines starting with ``#`` are
comments and are ignored.  Values must be numeric (will be validated as Decimal).

Expected keys (all required unless noted):
  Form 1065:
    ordinary_business_income, total_income, total_deductions,
    net_stcg, net_ltcg (optional), dividend_income, interest_income
  Schedule K-1 allocations:
    yash_ordinary_income, parin_ordinary_income
  Balance sheet:
    total_assets, total_equity

Partner ownership percentages are structural constants (not in corpus);
they are embedded from SYNCED LLC defaults: Yash 99%/100%, Parin 1%/0%.

Exit codes
----------
  0  Success
  1  Corpus file not found
  2  Required key missing from corpus
  3  Write failure
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CORPUS = _REPO_ROOT / "statements" / "2024.txt"
_DEFAULT_OUTPUT = (
    _REPO_ROOT / "tests" / "integration" / "fixtures" / "2024_cpa_expected.json"
)

# ---------------------------------------------------------------------------
# Structural constants — SYNCED LLC ownership split (CRIT-03)
# These are NOT in the corpus; they are encoded in the partnership agreement.
# ---------------------------------------------------------------------------

_YASH_CAPITAL_PCT = "0.99"
_YASH_PL_PCT = "1.00"
_PARIN_CAPITAL_PCT = "0.01"
_PARIN_PL_PCT = "0.00"

# Required corpus keys — absence is a fatal error (R-50)
_REQUIRED_KEYS = {
    "ordinary_business_income",
    "total_income",
    "total_deductions",
    "net_stcg",
    "dividend_income",
    "interest_income",
    "yash_ordinary_income",
    "parin_ordinary_income",
    "total_assets",
    "total_equity",
}

# Optional keys (present in SYNCED LLC corpus but not universally guaranteed)
_OPTIONAL_KEYS = {"net_ltcg"}


# ---------------------------------------------------------------------------
# Corpus parsing
# ---------------------------------------------------------------------------

def _load_corpus(path: Path) -> dict[str, str]:
    """Parse key=value corpus file; return raw string values (stripped)."""
    values: dict[str, str] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            print(f"[WARN] line {lineno}: no '=' — skipped: {line!r}", file=sys.stderr)
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().replace(",", "")
        if not value:
            continue
        try:
            Decimal(value)  # validate numeric
        except InvalidOperation:
            print(
                f"[WARN] line {lineno}: key={key!r} value={value!r} is not numeric — skipped",
                file=sys.stderr,
            )
            continue
        values[key] = value
    return values


def _validate_required(corpus: dict[str, str]) -> list[str]:
    """Return list of missing required keys."""
    return sorted(_REQUIRED_KEYS - corpus.keys())


# ---------------------------------------------------------------------------
# Fixture assembly
# ---------------------------------------------------------------------------

def _build_fixture(corpus: dict[str, str], source_path: Path) -> dict:
    net_ltcg = corpus.get("net_ltcg", "0.00")
    return {
        "_meta": {
            "description": (
                "CPA parity reference figures — SYNCED LLC fiscal year 2024 (ARCH-21)"
            ),
            "source": str(source_path.name),
            "tolerance_usd": "1.00",
            "notes": [
                "Replace values with CPA-prepared actuals once validated against "
                "K-1 / Form 1065 / Schedule L.",
                "Any divergence > $1.00 from these values is a P0 release blocker (R-50).",
                "parin_ordinary_income is 0.00 because Parin holds 0% P&L (CRIT-03).",
                "net_ltcg is negative: unrealised loss carried into 2024 Schedule K.",
            ],
        },
        "form_1065": {
            "ordinary_business_income": corpus["ordinary_business_income"],
            "total_income": corpus["total_income"],
            "total_deductions": corpus["total_deductions"],
            "net_short_term_capital_gain": corpus["net_stcg"],
            "net_long_term_capital_gain": net_ltcg,
            "dividend_income": corpus["dividend_income"],
            "interest_income": corpus["interest_income"],
        },
        "schedule_k1": {
            "yash": {
                "ordinary_income_loss": corpus["yash_ordinary_income"],
                "profit_loss_pct": _YASH_PL_PCT,
                "capital_pct": _YASH_CAPITAL_PCT,
            },
            "parin": {
                "ordinary_income_loss": corpus["parin_ordinary_income"],
                "profit_loss_pct": _PARIN_PL_PCT,
                "capital_pct": _PARIN_CAPITAL_PCT,
            },
        },
        "balance_sheet": {
            "total_assets": corpus["total_assets"],
            "total_equity": corpus["total_equity"],
        },
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Regenerate tests/integration/fixtures/2024_cpa_expected.json "
                    "from the CPA corpus file.",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to corpus file (default: $FI_CPA_CORPUS_PATH or statements/2024.txt)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUTPUT,
        metavar="PATH",
        help=f"Output JSON fixture path (default: {_DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the fixture JSON to stdout instead of writing to disk.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Resolve corpus path
    corpus_path: Path | None = args.corpus
    if corpus_path is None:
        env_path = os.environ.get("FI_CPA_CORPUS_PATH")
        if env_path:
            corpus_path = Path(env_path)
        else:
            corpus_path = _DEFAULT_CORPUS

    if not corpus_path.exists():
        print(
            f"[ERROR] Corpus file not found: {corpus_path}\n"
            "Set FI_CPA_CORPUS_PATH or place the file at statements/2024.txt",
            file=sys.stderr,
        )
        return 1

    print(f"[INFO] Reading corpus: {corpus_path}", file=sys.stderr)
    corpus = _load_corpus(corpus_path)

    missing = _validate_required(corpus)
    if missing:
        print(
            f"[ERROR] Required corpus keys missing: {missing}\n"
            "Update the corpus file and retry.",
            file=sys.stderr,
        )
        return 2

    fixture = _build_fixture(corpus, corpus_path)
    output_json = json.dumps(fixture, indent=2) + "\n"

    if args.dry_run:
        print(output_json)
        return 0

    out_path: Path = args.out
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_json, encoding="utf-8")
    except OSError as exc:
        print(f"[ERROR] Failed to write fixture: {exc}", file=sys.stderr)
        return 3

    print(f"[OK] Fixture written: {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
