#!/usr/bin/env python3
"""
scripts/reclassify_cash_coded.py  –  C-11 owner-run reclassification
=====================================================================
Before C-11 the COA keyword scan could code an income / expense row with a
balance-sheet account: "deposit" / "balance" matched Cash (1000), "tax" matched
Taxes Payable (2020), and so on.  Those rows never reach the P&L, so income is
understated.  The classifier no longer does this, but rows stored earlier keep
their codes.  This script finds them and proposes a new code by re-running the
classifier rules.

Selected rows: ``coa_code`` 1xxx / 2xxx, ``is_transfer`` = 0, and not a trade /
prior-period-adjustment row (``buy``, ``sell``, ``prior_period_adjustment``).

The classifier runs deterministically for the proposal: an empty, throw-away
classification memory (so codes the old keyword scan taught it are not replayed
and nothing is written to the real memory file) and the local rule backend only
(no network egress).  A proposal of 9000 (internal transfer) is never applied: being
internal is decided by the import-time opposite-leg match (C-03).  Such rows are
counted as skipped.

Output is counts only, by ``old -> new`` code pair.  No description, amount, date,
account or entity is printed.

Usage::

    python scripts/reclassify_cash_coded.py --db data/db/financials.db            # dry run
    python scripts/reclassify_cash_coded.py --db data/db/financials.db --apply    # write
    python scripts/reclassify_cash_coded.py --db <db> --apply --skip-suspense     # leave rows
                                                         # whose proposal is 9999 alone

``--apply`` writes through ``TransactionRepo.update_coa_with_meta`` and emits one
``classifier.reassigned`` audit event per changed row (ids and codes only).
Running it again changes nothing (idempotent).  The dry run opens the DB read-only.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOURCE = "c11_reclassify_cash_coded"
TRANSFER_CODE = "9000"
SUSPENSE_CODE = "9999"
_EXCLUDED_TYPES = ("buy", "sell", "prior_period_adjustment")

_SELECT = (
    "SELECT * FROM transactions"
    " WHERE coa_code IS NOT NULL AND substr(coa_code, 1, 1) IN ('1', '2')"
    " AND COALESCE(is_transfer, 0) = 0"
    f" AND transaction_type NOT IN ({','.join('?' for _ in _EXCLUDED_TYPES)})"
    " ORDER BY id"
)


@dataclass
class Plan:
    """What the run found.  ``changes`` holds (tx_id, old_code, new_code, new_name)."""

    candidates: int = 0
    changes: list[tuple[str, str, str, str]] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)  # reason -> count

    def pair_counts(self) -> Counter:
        return Counter((old, new) for _, old, new, _ in self.changes)


def _ro_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load(db_path: Path):
    """Read the selected rows and the COA, read-only."""
    from ledger_agent.core.database import COARepo, TransactionRepo

    conn = _ro_connect(db_path)
    try:
        rows = conn.execute(_SELECT, _EXCLUDED_TYPES).fetchall()
        txns = [TransactionRepo._row_to_model(r) for r in rows]
        coa = [
            COARepo._row_to_model(r)
            for r in conn.execute("SELECT * FROM coa ORDER BY code").fetchall()
        ]
    finally:
        conn.close()
    return txns, coa


def _propose(txns, coa, skip_suspense: bool) -> Plan:
    """Re-run the classifier rules on each row (isolated memory, local backend)."""
    from ledger_agent.core.intelligence import ai_backend
    from ledger_agent.core.intelligence import memory as memory_mod
    from ledger_agent.core.intelligence.ai_backend.local_backend import LocalBackend
    from ledger_agent.core.intelligence.classifier import classify_transaction

    plan = Plan(candidates=len(txns))
    prev_memory, prev_backend = memory_mod._memory, ai_backend._instance
    with tempfile.TemporaryDirectory(prefix="c11_reclass_") as tmp:
        memory_mod._memory = memory_mod.ClassificationMemory(Path(tmp) / "memory.json")
        ai_backend._instance = LocalBackend()
        try:
            for txn in txns:
                old = txn.coa_code
                txn.coa_code, txn.coa_name = "", ""
                txn = classify_transaction(txn, coa)
                new = txn.coa_code or SUSPENSE_CODE
                if new == old:
                    plan.skipped["unchanged"] += 1
                elif new == TRANSFER_CODE or txn.is_transfer:
                    plan.skipped["would_be_transfer"] += 1
                elif new == SUSPENSE_CODE and skip_suspense:
                    plan.skipped["suspense_skipped"] += 1
                else:
                    plan.changes.append((txn.id, old, new, txn.coa_name or new))
        finally:
            memory_mod._memory, ai_backend._instance = prev_memory, prev_backend
    return plan


def _apply(plan: Plan, db_path: Path, audit_fn: Callable[..., None] | None) -> int:
    from ledger_agent.core.database import TransactionRepo
    from ledger_agent.core.intelligence.classifier import CLASSIFIER_VERSION

    if audit_fn is None:
        from ledger_agent.core.audit import audit as audit_fn
    for tx_id, old, new, name in plan.changes:
        TransactionRepo.update_coa_with_meta(
            tx_id, new, name, CLASSIFIER_VERSION, 0.0, db_path=db_path, is_transfer=None,
        )
        audit_fn(
            "classifier.reassigned",
            transaction_id=tx_id,
            prior_coa_code=old,
            coa_code=new,
            classifier_version=CLASSIFIER_VERSION,
            source=SOURCE,
        )
    return len(plan.changes)


def run(
    db_path: Path,
    apply: bool = False,
    skip_suspense: bool = False,
    audit_fn: Callable[..., None] | None = None,
) -> Plan:
    """Build the plan; write it only when ``apply`` is set."""
    txns, coa = _load(db_path)
    plan = _propose(txns, coa, skip_suspense)
    if apply and plan.changes:
        _apply(plan, db_path, audit_fn)
    return plan


def format_report(plan: Plan, applied: bool) -> str:
    """Counts only: old -> new code pairs, never a description or amount."""
    lines = [
        "reclassify_cash_coded (C-11): " + ("APPLIED" if applied else "DRY RUN, no writes"),
        f"balance-sheet coded, non-transfer rows: {plan.candidates}",
    ]
    pairs = plan.pair_counts()
    for (old, new), n in sorted(pairs.items()):
        lines.append(f"  {old} -> {new}: {n}")
    for reason, n in sorted(plan.skipped.items()):
        lines.append(f"skipped ({reason}): {n}")
    lines.append(("changed" if applied else "would change") + f": {len(plan.changes)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="C-11: re-code income/expense rows stored with a balance-sheet code."
    )
    parser.add_argument("--db", type=Path, required=True,
                        help="SQLite DB to inspect (required; never defaulted).")
    parser.add_argument("--apply", action="store_true",
                        help="Write the proposed codes (default: dry run, counts only).")
    parser.add_argument("--skip-suspense", action="store_true",
                        help="Do not move rows whose proposal is 9999 (review).")
    args = parser.parse_args(argv)

    db_path: Path = args.db
    if not db_path.is_file():
        print("error: --db does not point to an existing file", file=sys.stderr)
        return 2
    try:
        plan = run(db_path, apply=args.apply, skip_suspense=args.skip_suspense)
    except sqlite3.Error as exc:
        print(f"error: database not readable ({type(exc).__name__})", file=sys.stderr)
        return 2
    print(format_report(plan, applied=args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
