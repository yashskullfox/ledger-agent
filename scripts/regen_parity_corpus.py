#!/usr/bin/env python3
"""
scripts/regen_parity_corpus.py — ARCH-21
=========================================

Reads the CPA-prepared 2024 reference file (``$FI_CPA_CORPUS_PATH`` or the
default path ``statements/2024/2024.txt``) and emits the canonical JSON
fixture at ``tests/integration/fixtures/2024_cpa_expected.json``.

The source file is markdown prose (not key=value); this script applies
pattern-based extraction to find each figure.  Every extracted value is
annotated with the markdown line it came from so reviewers can cross-check
without re-reading the whole file.

Usage
-----
    # Default paths:
    python scripts/regen_parity_corpus.py

    # Explicit source:
    FI_CPA_CORPUS_PATH=/path/to/2024.txt python scripts/regen_parity_corpus.py

    # Dry-run (print JSON, do not write):
    python scripts/regen_parity_corpus.py --dry-run

    # Write to custom output path:
    python scripts/regen_parity_corpus.py --out /tmp/expected.json

Acceptance (ARCH-21)
--------------------
    FI_CPA_CORPUS_PATH=statements/2024/2024.txt python scripts/regen_parity_corpus.py
    cat tests/integration/fixtures/2024_cpa_expected.json | python -m json.tool > /dev/null
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = REPO_ROOT / "statements" / "2024" / "2024.txt"
DEFAULT_FIXTURE = (
    REPO_ROOT / "tests" / "integration" / "fixtures" / "2024_cpa_expected.json"
)


# ── Extraction helpers ────────────────────────────────────────────────────────

def _dollar(text: str) -> str | None:
    """Extract the first $N,NNN value from *text* as a plain decimal string."""
    m = re.search(r"\$([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?)", text)
    if not m:
        return None
    return m.group(1).replace(",", "")


def _dollar_last(text: str) -> str | None:
    """Extract the LAST $N,NNN value from *text*.

    Used for markdown table rows where the End-of-Year value appears in the
    rightmost column: ``| Description | $BOY | **$EOY** |``
    """
    hits = re.findall(r"\$([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?)", text)
    if not hits:
        return None
    return hits[-1].replace(",", "")


def _pct(text: str) -> str | None:
    """Extract the first N% value as a 0.0NN decimal string."""
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", text)
    if not m:
        return None
    return str(round(float(m.group(1)) / 100, 6)).rstrip("0").rstrip(".")


def _find(lines: list[str], *keywords: str) -> str | None:
    """Return the first line that contains ALL of *keywords* (case-insensitive)."""
    kw = [k.lower() for k in keywords]
    for line in lines:
        lo = line.lower()
        if all(k in lo for k in kw):
            return line
    return None


# ── Parser ────────────────────────────────────────────────────────────────────

def parse_corpus(text: str) -> dict:
    """
    Extract CPA reference figures from the markdown corpus text.

    Returns a dict of {key: str_decimal} plus ``_source_lines`` provenance.
    """
    lines = text.splitlines()
    provenance: dict[str, str] = {}

    def grab(key: str, line: str | None, extractor=_dollar) -> str | None:
        if line is None:
            return None
        val = extractor(line)
        if val is not None:
            provenance[key] = line.strip()
        return val

    # ── Form 1065 income ─────────────────────────────────────────────────────
    gross_receipts_line   = _find(lines, "gross receipts")
    cogs_line             = _find(lines, "cost of goods")
    gross_profit_line     = _find(lines, "gross profit")
    total_deductions_line = _find(lines, "total deductions")
    ordinary_income_line  = _find(lines, "ordinary business income")

    total_income   = grab("total_income",   gross_receipts_line)
    cogs           = grab("cost_of_goods_sold", cogs_line)
    gross_profit   = grab("gross_profit",   gross_profit_line)
    total_ded      = grab("total_deductions", total_deductions_line)
    ordinary_inc   = grab("ordinary_business_income", ordinary_income_line)

    # ── Schedule K ───────────────────────────────────────────────────────────
    stcg_line       = _find(lines, "short-term capital gain")
    dividend_line   = _find(lines, "dividend")
    int_exp_line    = _find(lines, "investment interest expense")

    net_stcg   = grab("net_stcg",   stcg_line)
    dividend   = grab("dividend_income", dividend_line)
    int_exp    = grab("net_investment_interest_expense", int_exp_line)

    # ── Balance sheet ─────────────────────────────────────────────────────────
    # The corpus uses a markdown table with two value columns (BOY | EOY).
    # Use _dollar_last to pick the End-of-Year (right-most) column.
    cash_line         = _find(lines, "cash")
    oca_line          = _find(lines, "other current assets")
    total_assets_line = _find(lines, "total assets")
    liab_line         = _find(lines, "other current liabilities")
    equity_line       = _find(lines, "partners' capital accounts")

    cash_eoy   = grab("cash_eoy",                 cash_line,   _dollar_last)
    oca_eoy    = grab("other_current_assets_eoy",  oca_line,    _dollar_last)
    ta_eoy     = grab("total_assets_eoy",          total_assets_line, _dollar_last)
    liab_eoy   = grab("total_liabilities_eoy",     liab_line,   _dollar_last)
    equity_eoy = grab("total_equity_eoy",          equity_line, _dollar_last)

    # ── Schedule M-2 ─────────────────────────────────────────────────────────
    contrib_line = _find(lines, "capital contributed")
    ni_line      = _find(lines, "net income per books")
    dist_line    = _find(lines, "distributions")

    contrib    = grab("capital_contributed",   contrib_line)
    ni_books   = grab("net_income_per_books",  ni_line)
    dists      = grab("distributions",         dist_line)

    # ── Missouri PTE ─────────────────────────────────────────────────────────
    mo_income_line = _find(lines, "missouri net income")
    mo_rate_line   = _find(lines, "tax rate")
    mo_liab_line   = _find(lines, "pte income tax liability")
    mo_pay_line    = _find(lines, "anticipated tax payments")
    mo_over_line   = _find(lines, "overpayment")

    mo_income = grab("mo_net_income",          mo_income_line)
    mo_rate   = grab("mo_tax_rate",            mo_rate_line, _pct)
    mo_liab   = grab("mo_pte_liability",       mo_liab_line)
    mo_pay    = grab("mo_anticipated_payments", mo_pay_line)
    mo_over   = grab("mo_overpayment",         mo_over_line)

    # ── Partner ownership ─────────────────────────────────────────────────────
    # Look for ownership lines like "Partner 1 (...): NN% Capital, MM% Profit/Loss".
    # Real partner-name tokens used to identify the partner row come from
    # private/institutions.py (PARTNER_1_CORPUS_TOKEN / PARTNER_2_CORPUS_TOKEN)
    # or env-var fallback so the script can run against the gitignored CPA
    # corpus without hard-coding real names in committed source.
    try:
        from private.institutions import (  # type: ignore
            PARTNER_1_CORPUS_TOKEN as _P1_TOKEN,
            PARTNER_2_CORPUS_TOKEN as _P2_TOKEN,
        )
    except Exception:
        _P1_TOKEN = os.environ.get("FI_PARTNER_1_CORPUS_TOKEN", "partner 1")
        _P2_TOKEN = os.environ.get("FI_PARTNER_2_CORPUS_TOKEN", "partner 2")

    p1_line = _find(lines, _P1_TOKEN, "%")
    p2_line = _find(lines, _P2_TOKEN, "%")

    def _two_pcts(line: str) -> tuple[str | None, str | None]:
        """Return (first_pct, second_pct) from a line with two % values."""
        hits = re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*%", line)
        a = str(round(float(hits[0]) / 100, 6)).rstrip("0").rstrip(".") if len(hits) > 0 else None
        b = str(round(float(hits[1]) / 100, 6)).rstrip("0").rstrip(".") if len(hits) > 1 else None
        return a, b

    p1_cap_pct, p1_pl_pct = _two_pcts(p1_line) if p1_line else (None, None)
    p2_cap_pct, p2_pl_pct = _two_pcts(p2_line) if p2_line else (None, None)
    if p1_cap_pct:
        provenance["partner_1_capital_pct"]     = (p1_line or "").strip()
        provenance["partner_1_profit_loss_pct"] = (p1_line or "").strip()
    if p2_cap_pct:
        provenance["partner_2_capital_pct"]     = (p2_line or "").strip()
        provenance["partner_2_profit_loss_pct"] = (p2_line or "").strip()

    # ── K-1 derived figures ───────────────────────────────────────────────────
    # partner_1_ordinary_income = ordinary_business_income × p1_pl_pct
    # partner_2_ordinary_income = ordinary_business_income × p2_pl_pct
    p1_oi = p2_oi = None
    if ordinary_inc is not None and p1_pl_pct is not None:
        p1_oi = str((Decimal(ordinary_inc) * Decimal(p1_pl_pct)).quantize(Decimal("0.01")))
        p2_oi = str((Decimal(ordinary_inc) * Decimal(p2_pl_pct or "0")).quantize(Decimal("0.01")))
        provenance["partner_1_ordinary_income"] = f"Derived: {ordinary_inc} x {p1_pl_pct}"
        provenance["partner_2_ordinary_income"] = f"Derived: {ordinary_inc} x {p2_pl_pct or '0'}"

    # ── Assemble output ───────────────────────────────────────────────────────
    result = {
        "_source": "statements/2024/2024.txt — CPA-prepared 2024 figures for ENTITY_A",
        "_generated_by": "scripts/regen_parity_corpus.py",
        "_source_lines": provenance,
    }

    def _add(key: str, val: str | None) -> None:
        if val is not None:
            result[key] = f"{Decimal(val):.2f}"

    _add("ordinary_business_income",        ordinary_inc)
    _add("total_income",                    total_income)
    _add("cost_of_goods_sold",              cogs)
    _add("gross_profit",                    gross_profit)
    _add("total_deductions",                total_ded)
    _add("net_stcg",                        net_stcg)
    _add("dividend_income",                 dividend)
    _add("net_investment_interest_expense", int_exp)
    result.setdefault("interest_income",    "0.00")
    _add("partner_1_ordinary_income",       p1_oi)
    _add("partner_2_ordinary_income",       p2_oi)
    def _fmt_pct(s: str | None) -> str:
        """Format a percentage decimal string with exactly 2 decimal places."""
        if s is None:
            return "0.00"
        return f"{Decimal(s):.2f}"

    if p1_cap_pct:
        result["partner_1_capital_pct"]     = _fmt_pct(p1_cap_pct)
        result["partner_1_profit_loss_pct"] = _fmt_pct(p1_pl_pct)
    if p2_cap_pct:
        result["partner_2_capital_pct"]     = _fmt_pct(p2_cap_pct)
        result["partner_2_profit_loss_pct"] = _fmt_pct(p2_pl_pct)
    _add("total_assets_eoy",               ta_eoy)
    _add("cash_eoy",                        cash_eoy)
    _add("other_current_assets_eoy",        oca_eoy)
    _add("total_liabilities_eoy",           liab_eoy)
    _add("total_equity_eoy",                equity_eoy)
    _add("capital_contributed",             contrib)
    _add("net_income_per_books",            ni_books)
    _add("distributions",                   dists)
    _add("mo_net_income",                   mo_income)
    if mo_rate:
        result["mo_tax_rate"] = mo_rate
    _add("mo_pte_liability",                mo_liab)
    _add("mo_anticipated_payments",         mo_pay)
    _add("mo_overpayment",                  mo_over)

    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate tests/integration/fixtures/2024_cpa_expected.json "
                    "from the CPA corpus file."
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path(os.environ.get("FI_CPA_CORPUS_PATH", str(DEFAULT_CORPUS))),
        help="Path to CPA corpus file (default: statements/2024/2024.txt)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="Output JSON fixture path (default: tests/integration/fixtures/2024_cpa_expected.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print JSON to stdout; do not write file.",
    )
    args = parser.parse_args()

    if not args.corpus.exists():
        print(
            f"ERROR: Corpus file not found: {args.corpus}\n"
            f"Set FI_CPA_CORPUS_PATH to the path of the CPA 2024 reference file.",
            file=sys.stderr,
        )
        return 1

    text = args.corpus.read_text(encoding="utf-8")
    data = parse_corpus(text)
    out  = json.dumps(data, indent=2) + "\n"

    if args.dry_run:
        print(out)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(out, encoding="utf-8")
        print(f"Written: {args.out}")
        print(f"Keys extracted: {[k for k in data if not k.startswith('_')]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
