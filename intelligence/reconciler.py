"""
intelligence/reconciler.py  –  Cross-account reconciliation
─────────────────────────────────────────────────────────────
Identifies transfers between accounts (e.g. BROKER_Y → BANK_X)
and marks matching transactions is_transfer=True so they are
excluded from the P&L and balance sheet is correct.

Algorithm:
  For each TRANSFER_OUT in brokerage account, look for a matching
  TRANSFER_IN in bank accounts within ±3 days and same absolute amount.
"""
from __future__ import annotations

from typing import List, NamedTuple, Tuple

from core.models import Transaction, TransactionType


class ReconciliationMatch(NamedTuple):
    outgoing: Transaction  # brokerage TRANSFER_OUT
    incoming: Transaction  # bank     TRANSFER_IN
    delta_days: int  # settlement lag (usually 0-2)


def reconcile(
        all_transactions: List[Transaction],
        tolerance_days: int = 3,
) -> Tuple[List[ReconciliationMatch], List[Transaction]]:
    """
    Match TRANSFER_OUT ↔ TRANSFER_IN pairs across accounts.

    Returns:
        (matches, unmatched_transfers)
    """
    outgoing = [
        t for t in all_transactions
        if t.transaction_type in (TransactionType.TRANSFER_OUT,)
           and not t.is_reconciled
    ]
    incoming = [
        t for t in all_transactions
        if t.transaction_type in (TransactionType.TRANSFER_IN,)
           and not t.is_reconciled
    ]

    matches: List[ReconciliationMatch] = []
    matched_ids: set = set()

    for out in outgoing:
        best: Tuple[int, Transaction] | None = None
        for inc in incoming:
            if inc.id in matched_ids:
                continue
            # Same absolute amount?
            if abs(out.amount) != abs(inc.amount):
                continue
            # Within tolerance window?
            delta = abs((inc.date - out.date).days)
            if delta > tolerance_days:
                continue
            if best is None or delta < best[0]:
                best = (delta, inc)

        if best:
            delta_days, matched_inc = best
            matched_ids.add(matched_inc.id)
            matched_ids.add(out.id)
            # Mark both sides reconciled
            out.is_reconciled = True
            out.is_transfer = True
            matched_inc.is_reconciled = True
            matched_inc.is_transfer = True
            matches.append(ReconciliationMatch(out, matched_inc, delta_days))

    unmatched = [
        t for t in outgoing + incoming
        if t.id not in matched_ids
                   and t.transaction_type in (TransactionType.TRANSFER_OUT, TransactionType.TRANSFER_IN)
    ]

    return matches, unmatched


def print_reconciliation_report(
        matches: List[ReconciliationMatch],
        unmatched: List[Transaction],
) -> str:
    """Return a text summary of reconciliation results."""
    lines = [
        f"{'=' * 60}",
        f"  RECONCILIATION REPORT",
        f"{'=' * 60}",
        f"  Matched pairs  : {len(matches)}",
        f"  Unmatched items: {len(unmatched)}",
        f"{'=' * 60}",
    ]
    for m in matches:
        lines.append(
            f"  ✓ {m.outgoing.date}  ${abs(m.outgoing.amount):>10,.2f}"
            f"  [{m.outgoing.account_id[:8]}…] → [{m.incoming.account_id[:8]}…]"
            f"  (+{m.delta_days}d)"
        )
    if unmatched:
        lines.append("\n  ⚠ UNMATCHED TRANSFERS:")
        for t in unmatched:
            lines.append(
                f"    {t.date}  ${t.amount:>10,.2f}  {t.description[:50]}"
            )
    return "\n".join(lines)
