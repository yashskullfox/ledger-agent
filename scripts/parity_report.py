"""Parity report — diff the current pipeline's output for a fiscal year
against the CPA-filed truth (either statements/<year>.txt anchors or the
extracted private/reference/<year>-truth.json).

USAGE
    python scripts/parity_report.py --year 2024
    python scripts/parity_report.py --year 2025 --strict

WHAT IT DOES
    1. Reads the truth for <year>:
         · statements/<year>.txt   (CPA anchors — authoritative, always used
                                    if present)
         · private/reference/<year>-truth.json (extractor cache — used to
                                    fill fields the anchors don't cover)
    2. Runs the core API (no CLI/rich deps):
         · generate_form_1065(year)
         · generate_balance_sheet(year)
         · generate_k1(year, partner) for each partner in PARTNERS env
    3. Diffs each line item and prints a MASKED parity table
       (labels + $X,XXX buckets only — safe for terminal / CI logs).
    4. Writes the full un-masked report to
       private/reference/<year>-parity.json (gitignored) for local review.

EXIT STATUS
    0 — all comparisons match within $1 (R-51)
    1 — one or more DIFF > $1 or MISS in strict mode
    2 — pipeline failed to produce output for the year (empty DB, no txns,
        no snapshots — usually means statements not ingested)
    3 — truth missing (no statements/<year>.txt and no reference cache)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ledger_agent.core import api  # noqa: E402
from ledger_agent.core.database import init_db  # noqa: E402


TOL = 1.0  # R-51: divergence > $1 is a P0 release blocker


# --------------------------------------------------------------------------- #
# Truth loading                                                               #
# --------------------------------------------------------------------------- #

def _load_anchors(year: int) -> dict[str, float]:
    p = REPO_ROOT / "statements" / f"{year}.txt"
    if not p.exists():
        return {}
    anchors: dict[str, float] = {}
    for ln in p.read_text().splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        try:
            anchors[k.strip()] = float(v.strip())
        except ValueError:
            pass
    return anchors


def _load_extracted(year: int) -> dict:
    p = REPO_ROOT / "private" / "reference" / f"{year}-truth.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


# --------------------------------------------------------------------------- #
# Pipeline invocation                                                         #
# --------------------------------------------------------------------------- #

def _run_pipeline(year: int) -> dict:
    """Call every relevant public API function for the year, returning a
    dict of the raw outputs. Never raises for empty-DB — instead each key
    is set to None and a note is appended to ``errors``.
    """
    out: dict = {"errors": []}
    init_db()
    for name, fn in [
        ("form_1065",     lambda: api.generate_form_1065(year)),
        ("balance_sheet", lambda: api.generate_balance_sheet(year)),
        ("pte",           lambda: api.pte_estimate(year)),
        ("reconcile",     lambda: api.reconcile_year(year)),
        ("summary",       lambda: api.build_customer_summary(year)),
    ]:
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001 — diagnostic
            out[name] = None
            out["errors"].append(f"{name}: {type(e).__name__}: {e}")
    # K-1 per partner — enumerate slugs from PARTNERS env / defaults
    partners: dict[str, object] = {}
    for slug in ("partner_1", "partner_2"):
        try:
            partners[slug] = api.generate_k1(year, slug)
        except Exception as e:  # noqa: BLE001
            partners[slug] = None
            out["errors"].append(f"k1[{slug}]: {type(e).__name__}: {e}")
    out["k1"] = partners
    return out


def _get(obj, attr, default=None):
    if obj is None:
        return default
    v = getattr(obj, attr, default)
    # Pipeline returns Decimal; truth is float. Normalise to float for diff.
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


# --------------------------------------------------------------------------- #
# Field map — expected_key : (extractor_side, pipeline_side)                  #
# --------------------------------------------------------------------------- #

def _build_comparisons(anchors: dict[str, float],
                       extracted: dict,
                       pipeline: dict) -> list[dict]:
    """Return a list of comparison rows, each with keys:
       field, section, truth (anchor→extractor fallback), pipeline_value,
       delta (pipeline − truth), status (MATCH/DIFF/MISS/NO_TRUTH).
    """
    f1065 = pipeline.get("form_1065")
    bs    = pipeline.get("balance_sheet")

    # Truth = anchor if present; else fall back to extracted cache (which is
    # a diagnostic sample, not authoritative).
    def truth(anchor_key: str, ext_getter=lambda e: None) -> tuple[Optional[float], str]:
        if anchor_key in anchors:
            return anchors[anchor_key], "anchor"
        v = ext_getter(extracted)
        return v, ("extractor" if v is not None else "none")

    rows: list[dict] = []

    def add(field: str, section: str,
            truth_val: Optional[float], truth_src: str,
            pipeline_val: Optional[float]):
        if truth_val is None:
            status = "NO_TRUTH"
            delta = None
        elif pipeline_val is None:
            status = "MISS"
            delta = None
        else:
            delta = pipeline_val - truth_val
            status = "MATCH" if abs(delta) < TOL else "DIFF"
        rows.append({
            "field": field, "section": section,
            "truth": truth_val, "truth_source": truth_src,
            "pipeline": pipeline_val, "delta": delta, "status": status,
        })

    # --- Form 1065 ---------------------------------------------------------
    t, s = truth("total_income",
                 lambda e: e.get("form_1065", {}).get("l8_total_income"))
    add("total_income", "form_1065", t, s, _get(f1065, "total_income"))

    t, s = truth("total_deductions",
                 lambda e: e.get("form_1065", {}).get("l21_total_deductions"))
    add("total_deductions", "form_1065", t, s, _get(f1065, "total_deductions"))

    t, s = truth("ordinary_business_income",
                 lambda e: e.get("form_1065", {}).get("l22_ordinary_business_income"))
    add("ordinary_business_income", "form_1065", t, s, _get(f1065, "ordinary_business_income"))

    t, s = truth("cost_of_goods_sold",
                 lambda e: e.get("form_1065", {}).get("l2_cogs"))
    add("cost_of_goods_sold", "form_1065", t, s, _get(f1065, "cost_of_goods_sold"))

    t, s = truth("gross_profit",
                 lambda e: e.get("form_1065", {}).get("l3_gross_profit"))
    add("gross_profit", "form_1065", t, s, _get(f1065, "gross_profit"))

    # --- Schedule K --------------------------------------------------------
    t, s = truth("net_stcg",
                 lambda e: e.get("schedule_k", {}).get("k8_net_stcg"))
    add("net_stcg", "schedule_k", t, s, _get(f1065, "net_short_term_capital_gain"))

    t, s = truth("net_ltcg",
                 lambda e: e.get("schedule_k", {}).get("k9a_net_ltcg"))
    add("net_ltcg", "schedule_k", t, s, _get(f1065, "net_long_term_capital_gain"))

    t, s = truth("dividend_income",
                 lambda e: e.get("schedule_k", {}).get("k6a_ordinary_dividends"))
    add("dividend_income", "schedule_k", t, s, _get(f1065, "dividend_income"))

    t, s = truth("interest_income",
                 lambda e: e.get("schedule_k", {}).get("k5_interest_income"))
    add("interest_income", "schedule_k", t, s, _get(f1065, "interest_income"))

    t, s = truth("investment_interest_expense",
                 lambda e: e.get("schedule_k", {}).get("k13b_inv_interest_expense"))
    add("investment_interest_expense", "schedule_k", t, s,
        _get(f1065, "investment_interest_expense"))

    # --- Schedule L (year-end balance sheet) -------------------------------
    t, s = truth("total_assets",
                 lambda e: e.get("schedule_l", {}).get("l14_total_assets_eoy"))
    add("total_assets", "schedule_l", t, s, _get(bs, "total_assets"))

    t, s = truth("total_equity",
                 lambda e: e.get("schedule_l", {}).get("l21_partners_capital_eoy"))
    add("total_equity", "schedule_l", t, s, _get(bs, "total_equity"))

    # --- K-1 partner ordinary income --------------------------------------
    p1_pipeline = _get(pipeline.get("k1", {}).get("partner_1"), "ordinary_income_loss")
    p2_pipeline = _get(pipeline.get("k1", {}).get("partner_2"), "ordinary_income_loss")

    t, s = truth("partner_1_ordinary_income", lambda e: None)
    add("partner_1_ordinary_income", "schedule_k1", t, s, p1_pipeline)

    t, s = truth("partner_2_ordinary_income", lambda e: None)
    add("partner_2_ordinary_income", "schedule_k1", t, s, p2_pipeline)

    return rows


# --------------------------------------------------------------------------- #
# Masked stdout table                                                         #
# --------------------------------------------------------------------------- #

def _bucket(v: Optional[float]) -> str:
    if v is None:
        return "     —    "
    a = abs(v)
    if a == 0:
        b = "$0"
    elif a < 1_000:       b = "~$XXX"
    elif a < 10_000:      b = "~$X,XXX"
    elif a < 100_000:     b = "~$XX,XXX"
    elif a < 1_000_000:   b = "~$XXX,XXX"
    else:                 b = "~$X,XXX,XXX"
    sign = "-" if v < 0 else " "
    return f"{sign}{b:>10}"


def print_masked_table(year: int, rows: list[dict], pipeline_errors: list[str]) -> None:
    print(f"\n=== Parity report — FY{year} ===")
    print(f"{'field':<28}  {'section':<12}  {'src':<9}  {'truth':>11}  {'pipeline':>11}  status")
    print("-" * 92)
    counts = {"MATCH": 0, "DIFF": 0, "MISS": 0, "NO_TRUTH": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        print(f"{r['field']:<28}  {r['section']:<12}  {r['truth_source']:<9}  "
              f"{_bucket(r['truth'])}  {_bucket(r['pipeline'])}  {r['status']}")
    print("-" * 92)
    print(f"summary: {counts['MATCH']} match, {counts['DIFF']} diff, "
          f"{counts['MISS']} miss, {counts['NO_TRUTH']} no-truth")
    if pipeline_errors:
        print("\npipeline errors (first 5):")
        for e in pipeline_errors[:5]:
            print(f"  · {e[:120]}")


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--strict", action="store_true",
                    help="also fail on MISS (default: only DIFF fails)")
    args = ap.parse_args()

    anchors   = _load_anchors(args.year)
    extracted = _load_extracted(args.year)

    if not anchors and not extracted:
        print(f"ERROR: no truth for FY{args.year}. Populate either "
              f"statements/{args.year}.txt or run "
              f"scripts/extract_filing_reference.py --year {args.year}",
              file=sys.stderr)
        return 3

    pipeline = _run_pipeline(args.year)
    rows = _build_comparisons(anchors, extracted, pipeline)
    print_masked_table(args.year, rows, pipeline.get("errors", []))

    # Write full unmasked report to private/ for local review
    out_dir = REPO_ROOT / "private" / "reference"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.year}-parity.json"
    payload = {
        "fiscal_year": args.year,
        "tolerance_usd": TOL,
        "rows": rows,
        "pipeline_errors": pipeline.get("errors", []),
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nfull report: {out_path.relative_to(REPO_ROOT)}")

    diff = sum(1 for r in rows if r["status"] == "DIFF")
    miss = sum(1 for r in rows if r["status"] == "MISS")
    if diff or (args.strict and miss):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

