"""Wash-sale diagnostic report — computed vs authoritative (1099-B CSV).

Usage:
    python scripts/wash_sale_report.py --year 2024
    python scripts/wash_sale_report.py --year 2025 --json

Outputs a MASKED per-ticker table to stdout so it is safe to paste. Writes
the full un-masked report (with real tickers + amounts) to
``private/reference/<year>-wash-sale.json`` for local review.

WHAT IT SHOWS
    * loss_sales_considered — how many SELL@loss events were examined
    * total_loss_at_risk    — sum of losses that could be disallowed
    * total_disallowed_computed — our own §1091 approximation from
                              transactions + monthly position deltas
    * total_disallowed_csv  — the authoritative 1099-B overlay (if present)
    * per-ticker delta      — where our estimate diverges (year-end learning
                              loop; empty until a CSV is placed at
                              private/wash_sale_adjustments.csv)
"""
from __future__ import annotations
import argparse, json, sys
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ledger_agent.core import api  # noqa: E402
from ledger_agent.core.accounting import wash_sale  # noqa: E402
from ledger_agent.core.database import init_db  # noqa: E402


def _bucket(v) -> str:
    if v is None: return "     —    "
    v = Decimal(str(v))
    a = abs(v)
    if a == 0:                  b = "$0"
    elif a < 1_000:             b = "~$XXX"
    elif a < 10_000:            b = "~$X,XXX"
    elif a < 100_000:           b = "~$XX,XXX"
    elif a < 1_000_000:         b = "~$XXX,XXX"
    else:                       b = "~$X,XXX,XXX"
    sign = "-" if v < 0 else " "
    return f"{sign}{b:>10}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--json", action="store_true",
                    help="also dump the full report to stdout (contains PII)")
    args = ap.parse_args()

    init_db()
    entity, _ = api._entity_and_periods(args.year)
    recon = wash_sale.reconcile_computed_vs_csv(entity.id, args.year)

    print(f"\n=== Wash-sale reconciliation — FY{args.year} (masked) ===\n")
    computed = recon["computed_report"]
    print(f"  loss_sales_considered:        {computed['loss_sales_considered']:>6d}")
    print(f"  total_loss_at_risk:           {_bucket(computed['total_loss_at_risk'])}")
    print(f"  computed_disallowed:          {_bucket(recon['computed_total'])}"
          f"   ({len(computed['findings'])} finding{'s' if len(computed['findings']) != 1 else ''})")
    print(f"  csv_disallowed (authoritative): {_bucket(recon['csv_total'])}"
          f"   ({'CSV present' if recon['csv_present'] else 'no CSV — computed will be used'})")
    print()

    if recon["per_ticker"]:
        print(f"  per-ticker deltas (computed − csv):")
        print(f"    ticker    computed      csv       delta")
        # Mask ticker to first char + hash
        for tk, row in recon["per_ticker"].items():
            masked = f"TKR_{abs(hash(tk)) % 1000:03d}"
            print(f"    {masked:<9s} {_bucket(row['computed'])}  "
                  f"{_bucket(row['authoritative_csv'])}  {_bucket(row['delta'])}")
        print()

    if computed["notes"]:
        print("  notes:")
        for n in computed["notes"]:
            print(f"    · {n}")
        print()

    # Full report (real values) goes to private/
    out_dir = REPO / "private" / "reference"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.year}-wash-sale.json"
    out_path.write_text(json.dumps(recon, indent=2))
    print(f"  full report: {out_path.relative_to(REPO)}")

    if args.json:
        print("\n--- FULL JSON (contains PII, do not paste to shared logs) ---")
        print(json.dumps(recon, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

