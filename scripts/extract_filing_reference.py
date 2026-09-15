"""Extract Form 1065 / Schedule K / L / M-2 / K-1 truth values from a filed
partnership return PDF and cache them for parity testing.

Output is written to ``private/reference/<year>-truth.json`` — private/ is
gitignored, so real values stay off the tracked tree (AGENTS.md R-73/R-74).
The stdout summary is MASKED (labels + $X,XXX buckets only) so this script is
safe to run in a shared terminal / pipe into logs.

USAGE
    python scripts/extract_filing_reference.py \
        --year 2024 \
        --pdf "Synced-Accounts/2024/Tax-filled/2024 Tax Return Documents (SYNCED LLC).pdf"

    python scripts/extract_filing_reference.py \
        --year 2025 \
        --pdf "Synced-Accounts/2025 - SYNCED/2025 Tax Returns SYNCED.pdf"

DESIGN
    Every regex is anchored on IRS-stable line labels (Form 1065 line numbers,
    Schedule L/M-1/M-2 captions, K-1 box numbers). The extractor never depends
    on partner or entity names being any particular string — partners are
    captured in the order the K-1 pages appear (partner_1, partner_2, ...).

PRIVACY
    Raw text buffers stay in memory and are dropped on exit. Only structured
    numeric fields + partner ordinal slugs are written. Partner display names
    are NOT written to disk. See privacy.py fingerprinting convention.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

try:
    import pdfplumber  # type: ignore
except ImportError:  # pragma: no cover
    print("pdfplumber is required: pip install pdfplumber", file=sys.stderr)
    sys.exit(2)


# --------------------------------------------------------------------------- #
# Regex helpers                                                               #
# --------------------------------------------------------------------------- #

# Amount pattern: optional parenthesized negative "(1,234)", optional leading -,
# optional $, then digits with commas and optional cents. IRS returns often
# use whole dollars but not always.
_AMT_RE = re.compile(r"\(?-?\$?\s*\d[\d,]*(?:\.\d{1,2})?\)?")


def _to_number(raw: str) -> Optional[float]:
    if raw is None:
        return None
    s = raw.strip().replace("$", "").replace(" ", "").replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    if s in ("", "-"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _last_amount_on_line(line: str) -> Optional[float]:
    """Return the last numeric amount on the line (IRS forms right-align)."""
    matches = _AMT_RE.findall(line)
    if not matches:
        return None
    return _to_number(matches[-1])


def _find_label(label_re: str, text: str, flags: int = re.IGNORECASE) -> Optional[float]:
    """Find the first line containing ``label_re`` and return its last amount."""
    pat = re.compile(label_re, flags)
    for ln in text.split("\n"):
        if pat.search(ln):
            v = _last_amount_on_line(ln)
            if v is not None:
                return v
    return None


def _find_first(pattern: str, text: str, flags: int = re.MULTILINE) -> Optional[float]:
    """Back-compat helper: find first regex match, return group(1) as number."""
    m = re.search(pattern, text, flags)
    if not m:
        return None
    return _to_number(m.group(1))


# --------------------------------------------------------------------------- #
# Line-item catalog — {key: label_regex}                                      #
# --------------------------------------------------------------------------- #
# Strategy: locate any line that matches the label regex; return the LAST
# numeric amount on that line (IRS right-aligned totals column).

_FORM_1065_LABELS: dict[str, str] = {
    "l1a_gross_receipts":            r"Gross receipts or sales",
    "l1c_balance":                   r"Balance\.?\s*Subtract line 1b",
    "l2_cogs":                       r"Cost of goods sold",
    "l3_gross_profit":               r"Gross profit\.?\s*Subtract line 2",
    "l4_ord_income_partnerships":    r"Ordinary income \(loss\) from other partnerships",
    "l5_net_farm":                   r"Net farm profit \(loss\)",
    "l6_net_gain_4797":              r"Net gain \(loss\) from Form 4797",
    "l7_other_income":               r"Other income \(loss\)",
    "l8_total_income":               r"Total income \(loss\)\.?\s*Combine lines",
    "l9_salaries_wages":             r"Salaries and wages",
    "l10_guaranteed_payments":       r"Guaranteed payments to partners",
    "l11_repairs":                   r"Repairs and maintenance",
    "l12_bad_debts":                 r"^\s*12\s+Bad debts",
    "l13_rent":                      r"^\s*13\s+Rent\b",
    "l14_taxes_licenses":            r"Taxes and licenses",
    "l15_interest":                  r"^\s*15\s+Interest\b",
    "l16c_depreciation":             r"Depreciation.*Subtract line 16b",
    "l17_depletion":                 r"Depletion",
    "l18_retirement":                r"Retirement plans",
    "l19_employee_benefit":          r"Employee benefit programs",
    "l20_other_deductions":          r"Other deductions",
    "l21_total_deductions":          r"Total deductions\.?\s*Add",
    "l22_ordinary_business_income":  r"Ordinary business income \(loss\)\.?\s*Subtract",
}

_SCH_K_LABELS: dict[str, str] = {
    "k1_ordinary_business_income":   r"Ordinary business income \(loss\) \(page",
    "k2_net_rental_re":              r"Net rental real estate income",
    "k4a_guaranteed_services":       r"Guaranteed payments.*services",
    "k5_interest_income":            r"^\s*5\s+Interest income|^Interest income\b",
    "k6a_ordinary_dividends":        r"Ordinary dividends\b",
    "k6b_qualified_dividends":       r"Qualified dividends\b",
    "k7_royalties":                  r"^\s*7\s+Royalties|Royalties\b",
    "k8_net_stcg":                   r"Net short-term capital gain",
    "k9a_net_ltcg":                  r"Net long-term capital gain",
    "k10_net_1231":                  r"Net section 1231 gain",
    "k11_other_income":              r"^\s*11\s+Other income",
    "k12_sec179":                    r"Section 179 deduction",
    "k13a_contributions":            r"Cash contributions|Charitable contributions",
    "k13b_inv_interest_expense":     r"Investment interest expense",
    "k14a_se_income":                r"Net earnings \(loss\) from self-employment",
    "k20a_investment_income":        r"Investment income\b",
    "k20b_investment_expenses":      r"Investment expenses\b",
}

# Schedule L uses two side-by-side columns (BOY | EOY). "Last amount on line"
# will correctly pick EOY (right-most column) except where cents=0 renders as
# blank; the caller can post-process if needed.
_SCH_L_LABELS: dict[str, str] = {
    "l1_cash_eoy":                   r"^\s*1\s+Cash\b|^\s*Cash\b",
    "l6_us_gov_obligations_eoy":     r"U\.S\. government obligations",
    "l7_other_investments_eoy":      r"Other investments",
    "l14_total_assets_eoy":          r"Total assets\b",
    "l16_accounts_payable_eoy":      r"Accounts payable\b",
    "l18_other_st_liab_eoy":         r"Other current liabilities",
    "l21_partners_capital_eoy":      r"Partners.\s*capital accounts?",
    "l22_total_liab_capital_eoy":    r"Total liabilities and capital",
}

_SCH_M1_LABELS: dict[str, str] = {
    "m1_l1_net_income_books":        r"Net income \(loss\) per books",
}

_SCH_M2_LABELS: dict[str, str] = {
    "m2_l1_bal_boy":                 r"Balance at beginning of year",
    "m2_l2a_capital_contributed_cash": r"Capital contributed:\s*a\s*Cash",
    "m2_l3_net_income":              r"Net income \(loss\) \(see instructions\)",
    "m2_l6a_distributions_cash":     r"Distributions:\s*a\s*Cash",
    "m2_l9_bal_eoy":                 r"Balance at end of year",
}

_K1_LABELS: dict[str, str] = {
    "k1_box1_ord_income":            r"^\s*1\s+Ordinary business income",
    "k1_box2_net_rental_re":         r"^\s*2\s+Net rental real estate",
    "k1_box5_interest":              r"^\s*5\s+Interest income",
    "k1_box6a_ord_div":              r"^\s*6a\s+Ordinary dividends",
    "k1_box6b_qual_div":             r"^\s*6b\s+Qualified dividends",
    "k1_box8_stcg":                  r"^\s*8\s+Net short-term capital gain",
    "k1_box9a_ltcg":                 r"^\s*9a\s+Net long-term capital gain",
}


# --------------------------------------------------------------------------- #
# Data model                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class PartnerK1Truth:
    slug: str                     # "partner_1", "partner_2" (order-of-appearance)
    profit_loss_pct: Optional[float] = None
    capital_pct: Optional[float] = None
    capital_boy: Optional[float] = None
    capital_contributed: Optional[float] = None
    net_income: Optional[float] = None
    withdrawals: Optional[float] = None
    capital_eoy: Optional[float] = None
    boxes: dict = field(default_factory=dict)


@dataclass
class FilingTruth:
    fiscal_year: int
    source_pdf_basename: str      # basename only — never full path
    extractor_version: str = "1.0"
    form_1065: dict = field(default_factory=dict)
    schedule_k: dict = field(default_factory=dict)
    schedule_l: dict = field(default_factory=dict)
    schedule_m1: dict = field(default_factory=dict)
    schedule_m2: dict = field(default_factory=dict)
    partners: list[PartnerK1Truth] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Extraction pipeline                                                         #
# --------------------------------------------------------------------------- #


def _extract_block(text: str, labels: dict[str, str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, label_re in labels.items():
        val = _find_label(label_re, text)
        if val is not None:
            out[key] = val
    return out


def _find_capital_pct(text: str) -> tuple[Optional[float], Optional[float]]:
    """Item J on K-1: '% ending' for profit and capital."""
    pl = None
    cap = None
    # Order on K-1 item J: Profit, Loss, Capital
    m = re.search(r"Profit\s+[\d.]+\s*%\s+([\d.]+)\s*%", text)
    if m:
        pl = float(m.group(1))
    m = re.search(r"Capital\s+[\d.]+\s*%\s+([\d.]+)\s*%", text)
    if m:
        cap = float(m.group(1))
    return pl, cap


def extract(pdf_path: Path, fiscal_year: int) -> FilingTruth:
    truth = FilingTruth(fiscal_year=fiscal_year, source_pdf_basename=pdf_path.name)

    with pdfplumber.open(str(pdf_path)) as pdf:
        pages = [(i + 1, p.extract_text() or "") for i, p in enumerate(pdf.pages)]

    # Form 1065 pages 1-3 (income + deductions section). Include page 1 even if
    # it only carries the "U.S. Return of Partnership" header (some years) and
    # explicitly EXCLUDE any page that also carries "Schedule K-1" — those
    # partner pages contain overlapping labels that would poison lookups.
    body = "\n".join(
        t for i, t in pages
        if i <= 7
        and ("Form 1065" in t or "U.S. Return of Partnership" in t)
        and "Schedule K-1" not in t
    )
    truth.form_1065 = _extract_block(body, _FORM_1065_LABELS)

    # Schedule K — the "K " page (with trailing space) is distinct from K-1.
    sch_k_text = "\n".join(
        t for i, t in pages if "Schedule K " in t and "Schedule K-1" not in t
    )
    truth.schedule_k = _extract_block(sch_k_text, _SCH_K_LABELS)

    # Schedule L / M-1 / M-2 (typically page 5-6, sometimes with continuation).
    sch_l_text  = "\n".join(t for i, t in pages if "Schedule L" in t)
    sch_m1_text = "\n".join(t for i, t in pages if "Schedule M-1" in t)
    sch_m2_text = "\n".join(t for i, t in pages if "Schedule M-2" in t)
    truth.schedule_l  = _extract_block(sch_l_text,  _SCH_L_LABELS)
    truth.schedule_m1 = _extract_block(sch_m1_text, _SCH_M1_LABELS)
    truth.schedule_m2 = _extract_block(sch_m2_text, _SCH_M2_LABELS)

    # K-1 per partner — each partner's K-1 starts on a fresh page containing
    # both "Schedule K-1" and "Part II" (partner identification).
    partner_pages = [
        t for _, t in pages
        if "Schedule K-1" in t and "Part II" in t and "Information About the Partner" in t
    ]

    for idx, ptext in enumerate(partner_pages, start=1):
        boxes = _extract_block(ptext, _K1_LABELS)
        pl, cap = _find_capital_pct(ptext)
        # Capital account rollforward — item L, appears in a mini-table.
        cap_boy      = _find_label(r"Beginning capital account",           ptext)
        cap_contrib  = _find_label(r"Capital contributed during the year", ptext)
        cap_net      = _find_label(r"Current year net income \(loss\)",    ptext)
        cap_withdraw = _find_label(r"Withdrawals and distributions",       ptext)
        cap_eoy      = _find_label(r"Ending capital account",              ptext)
        truth.partners.append(PartnerK1Truth(
            slug=f"partner_{idx}",
            profit_loss_pct=pl,
            capital_pct=cap,
            capital_boy=cap_boy,
            capital_contributed=cap_contrib,
            net_income=cap_net,
            withdrawals=cap_withdraw,
            capital_eoy=cap_eoy,
            boxes=boxes,
        ))

    if not truth.partners:
        truth.notes.append("no K-1 partner pages detected — verify PDF structure")
    if not truth.form_1065:
        truth.notes.append("no Form 1065 line items detected — verify PDF structure")

    return truth


# --------------------------------------------------------------------------- #
# Masked stdout summary (safe for logs)                                       #
# --------------------------------------------------------------------------- #


def _mask(v: Optional[float]) -> str:
    if v is None:
        return "     —    "
    a = abs(v)
    if a == 0:
        bucket = "$0"
    elif a < 1_000:
        bucket = "~$XXX"
    elif a < 10_000:
        bucket = "~$X,XXX"
    elif a < 100_000:
        bucket = "~$XX,XXX"
    elif a < 1_000_000:
        bucket = "~$XXX,XXX"
    else:
        bucket = "~$X,XXX,XXX"
    sign = "-" if v < 0 else " "
    return f"{sign}{bucket:>10}"


def print_masked_summary(truth: FilingTruth) -> None:
    print(f"\n=== Filing truth — FY{truth.fiscal_year} (source: {truth.source_pdf_basename}) ===")
    print(f"    extractor v{truth.extractor_version}")
    print(f"    Form 1065 lines captured: {len(truth.form_1065)}")
    print(f"    Schedule K lines captured: {len(truth.schedule_k)}")
    print(f"    Schedule L lines captured: {len(truth.schedule_l)}")
    print(f"    Schedule M-2 lines captured: {len(truth.schedule_m2)}")
    print(f"    K-1 partner blocks captured: {len(truth.partners)}")

    key_lines = [
        ("Form 1065 L8  total income",         truth.form_1065.get("l8_total_income")),
        ("Form 1065 L21 total deductions",     truth.form_1065.get("l21_total_deductions")),
        ("Form 1065 L22 ord. business income", truth.form_1065.get("l22_ordinary_business_income")),
        ("Sch K  L5  interest income",         truth.schedule_k.get("k5_interest_income")),
        ("Sch K  L6a ordinary dividends",      truth.schedule_k.get("k6a_ordinary_dividends")),
        ("Sch K  L8  net STCG",                truth.schedule_k.get("k8_net_stcg")),
        ("Sch K  L9a net LTCG",                truth.schedule_k.get("k9a_net_ltcg")),
        ("Sch L  L14 total assets EOY",        truth.schedule_l.get("l14_total_assets_eoy")),
        ("Sch L  L21 partners' capital EOY",   truth.schedule_l.get("l21_partners_capital_eoy")),
        ("Sch L  L22 total liab+cap EOY",      truth.schedule_l.get("l22_total_liab_capital_eoy")),
        ("Sch M-2 L9 balance EOY",             truth.schedule_m2.get("m2_l9_bal_eoy")),
    ]
    print()
    for label, v in key_lines:
        print(f"    {label:<40s}  {_mask(v)}")
    print()
    for p in truth.partners:
        pl = f"{p.profit_loss_pct:>5.2f}%" if p.profit_loss_pct is not None else "  —  "
        cap = f"{p.capital_pct:>5.2f}%" if p.capital_pct is not None else "  —  "
        print(f"    {p.slug}: P/L={pl}  Capital={cap}  Box1={_mask(p.boxes.get('k1_box1_ord_income'))}  Cap EOY={_mask(p.capital_eoy)}")
    if truth.notes:
        print("\n    NOTES:")
        for n in truth.notes:
            print(f"      · {n}")


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def _default_output(year: int) -> Path:
    root = Path(__file__).resolve().parent.parent
    out_dir = root / "private" / "reference"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{year}-truth.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--pdf", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None,
                    help="output json (default: private/reference/<year>-truth.json)")
    args = ap.parse_args()

    if not args.pdf.exists():
        print(f"ERROR: {args.pdf} not found", file=sys.stderr)
        return 2

    truth = extract(args.pdf, args.year)
    out_path = args.out or _default_output(args.year)

    # Ensure output is under private/ or an absolute path outside the repo
    repo_root = Path(__file__).resolve().parent.parent
    try:
        rel = out_path.resolve().relative_to(repo_root)
        if not str(rel).startswith("private/"):
            print(f"REFUSING to write outside private/: {out_path}", file=sys.stderr)
            print("Extractor output contains PII (real filing amounts). "
                  "Use --out under private/ or omit --out.", file=sys.stderr)
            return 3
    except ValueError:
        pass  # absolute path outside repo — user's choice

    payload = {
        "fiscal_year": truth.fiscal_year,
        "source_pdf_basename": truth.source_pdf_basename,
        "extractor_version": truth.extractor_version,
        "form_1065": truth.form_1065,
        "schedule_k": truth.schedule_k,
        "schedule_l": truth.schedule_l,
        "schedule_m1": truth.schedule_m1,
        "schedule_m2": truth.schedule_m2,
        "partners": [asdict(p) for p in truth.partners],
        "notes": truth.notes,
    }
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"WROTE {out_path.relative_to(repo_root)}  ({out_path.stat().st_size} bytes)")
    print_masked_summary(truth)
    return 0


if __name__ == "__main__":
    sys.exit(main())




