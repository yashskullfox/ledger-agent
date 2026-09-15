"""Diagnose the W17-BS-DELTA: compare each pipeline balance-sheet line for
FY2024 against the CPA-filed Schedule L truth. Prints a MASKED table so
stdout is safe to paste; writes the full un-masked diff into
private/reference/2024-bs-delta.json.

Usage:
    python scripts/diagnose_bs_delta.py --year 2024
    python scripts/diagnose_bs_delta.py --year 2025
"""
from __future__ import annotations
import argparse, json, sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ledger_agent.core import api  # noqa: E402
from ledger_agent.core.database import init_db, get_conn  # noqa: E402


def _bucket(v) -> str:
    if v is None: return "     —    "
    v = Decimal(str(v))
    a = abs(v)
    if a == 0: b = "$0"
    elif a < 1_000:    b = "~$XXX"
    elif a < 10_000:   b = "~$X,XXX"
    elif a < 100_000:  b = "~$XX,XXX"
    elif a < 1_000_000:b = "~$XXX,XXX"
    else:              b = "~$X,XXX,XXX"
    sign = "-" if v < 0 else " "
    return f"{sign}{b:>10}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, required=True)
    args = ap.parse_args()

    init_db()
    truth_path = REPO_ROOT / "private" / "reference" / f"{args.year}-truth.json"
    if not truth_path.exists():
        print(f"ERROR: {truth_path} not found. Run extract_filing_reference.py first.",
              file=sys.stderr)
        return 2
    truth = json.loads(truth_path.read_text())
    sch_l = truth.get("schedule_l", {})

    # Run pipeline
    bs = api.generate_balance_sheet(args.year)

    # Get every account contribution to total_assets
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT a.id, a.institution, a.account_type, "
            "       s.statement_period, s.ending_balance, s.gross_asset_value "
            "  FROM accounts a "
            "  LEFT JOIN account_snapshots s ON s.account_id = a.id "
            " WHERE s.statement_period LIKE ? "
            " ORDER BY a.institution, s.statement_period",
            (f"{args.year}-%",),
        ).fetchall()

    # Take LAST snapshot per account within the year
    last_snap: dict[str, dict] = {}
    for aid, inst, typ, period, end_bal, gross in rows:
        last_snap[aid] = {
            "id_short": aid[-8:],
            "institution": inst,
            "account_type": typ,
            "period": period,
            "ending_balance": Decimal(str(end_bal or 0)),
            "gross_asset_value": Decimal(str(gross or 0)),
        }

    total_from_ending = sum((s["ending_balance"] for s in last_snap.values()), Decimal("0"))
    total_from_gross  = sum((s["gross_asset_value"] for s in last_snap.values()), Decimal("0"))

    # Truth values from Sch L
    truth_l14 = sch_l.get("l14_total_assets_eoy")
    truth_l1  = sch_l.get("l1_cash_eoy")
    truth_l7  = sch_l.get("l7_other_investments_eoy")
    truth_l21 = sch_l.get("l21_partners_capital_eoy")
    truth_l22 = sch_l.get("l22_total_liab_capital_eoy")

    pipeline_ta  = Decimal(str(bs.total_assets))
    pipeline_te  = Decimal(str(bs.total_equity))
    pipeline_tl  = Decimal(str(bs.total_liabilities))

    delta_ta = pipeline_ta - Decimal(str(truth_l14)) if truth_l14 is not None else None
    delta_te = pipeline_te - Decimal(str(truth_l21)) if truth_l21 is not None else None

    print(f"\n=== FY{args.year} balance-sheet delta diagnosis (masked) ===\n")
    print(f"  {'Sch L line':<28s} {'CPA truth':>11s}  {'pipeline':>11s}  delta")
    print(f"  {'-'*72}")
    print(f"  {'L1  cash EOY':<28s} {_bucket(truth_l1)}  {'(part of TA)':>11s}")
    print(f"  {'L7  other investments EOY':<28s} {_bucket(truth_l7)}  {'(part of TA)':>11s}")
    print(f"  {'L14 total assets EOY':<28s} {_bucket(truth_l14)}  {_bucket(pipeline_ta)}  {_bucket(delta_ta)}")
    print(f"  {'L21 partners capital EOY':<28s} {_bucket(truth_l21)}  {_bucket(pipeline_te)}  {_bucket(delta_te)}")
    print(f"  {'L22 total liab+cap EOY':<28s} {_bucket(truth_l22)}  {_bucket(pipeline_ta)}")
    print()
    print(f"  Pipeline decomposition:")
    print(f"    sum(ending_balance across last snapshots):       {_bucket(total_from_ending)}")
    print(f"    sum(gross_asset_value across last snapshots):    {_bucket(total_from_gross)}")
    print(f"    difference (invested value − cash):              {_bucket(total_from_gross - total_from_ending)}")
    print(f"    pipeline.total_assets:                           {_bucket(pipeline_ta)}")
    print(f"    pipeline.total_liabilities:                      {_bucket(pipeline_tl)}")
    print()
    print(f"  Per-account contribution (last snapshot in FY{args.year}):")
    print(f"    {'inst':<8s} {'type':<10s} {'period':<8s} {'ending':>11s}  {'gross':>11s}")
    for s in sorted(last_snap.values(), key=lambda x: (x["institution"] or "", x["period"] or "")):
        # institution masked to first 5 chars sanitised
        inst_masked = "INST_" + (s["id_short"][:3].upper())
        print(f"    {inst_masked:<8s} {str(s['account_type'])[:10]:<10s} "
              f"{s['period']:<8s} {_bucket(s['ending_balance'])}  {_bucket(s['gross_asset_value'])}")

    # Write full report
    out = REPO_ROOT / "private" / "reference" / f"{args.year}-bs-delta.json"
    out.write_text(json.dumps({
        "fiscal_year": args.year,
        "truth_schedule_l": sch_l,
        "pipeline_total_assets": str(pipeline_ta),
        "pipeline_total_equity": str(pipeline_te),
        "pipeline_total_liabilities": str(pipeline_tl),
        "delta_total_assets": str(delta_ta) if delta_ta is not None else None,
        "delta_total_equity": str(delta_te) if delta_te is not None else None,
        "sum_ending_balance": str(total_from_ending),
        "sum_gross_asset_value": str(total_from_gross),
        "accounts": [
            {**{k: (str(v) if isinstance(v, Decimal) else v) for k, v in s.items()}}
            for s in last_snap.values()
        ],
    }, indent=2))
    print(f"\n  full report: {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

