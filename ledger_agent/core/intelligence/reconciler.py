"""
intelligence/reconciler.py  –  Cross-account reconciliation
─────────────────────────────────────────────────────────────
Pairs the two legs of an internal transfer between two accounts of the
same entity (e.g. Broker Y → Bank X).

Algorithm (R-03):
  For each TRANSFER_OUT leg, look for a TRANSFER_IN leg that
    * sits in a DIFFERENT account (two legs in one account are not a transfer
      between the entity's accounts — usually a reversal or a refund),
    * has the same absolute amount, and
    * is dated within ±``MATCH_WINDOW_DAYS`` days (ACH / weekend settlement lag).
  The closest-dated candidate wins; ties keep input order.  Each leg is used once.

Entity scoping: ``Transaction`` carries no entity id, so callers must pass the
legs of ONE entity only (``api.reconcile_year`` and the CLI post-import reconcile
both read entity-scoped transactions — ARCH-30 / R-09).

``reconcile`` is a pure function: it neither mutates its input nor skips legs that
are already ``is_reconciled`` (re-running it gives the same pairs).  Callers that
want the result stored call ``TransactionRepo.mark_reconciled`` on the matched ids.
"""
from __future__ import annotations

from typing import NamedTuple

from ledger_agent.core.models import Transaction, TransactionType

# Maximum settlement lag, in calendar days, between the two legs of a transfer.
# Seven days covers a weekend plus a holiday on a slow ACH.
MATCH_WINDOW_DAYS = 7


class ReconciliationMatch(NamedTuple):
    outgoing: Transaction  # TRANSFER_OUT leg
    incoming: Transaction  # TRANSFER_IN leg (a different account of the same entity)
    delta_days: int  # settlement lag (usually 0-2)


def reconcile(
        all_transactions: list[Transaction],
        tolerance_days: int = MATCH_WINDOW_DAYS,
) -> tuple[list[ReconciliationMatch], list[Transaction]]:
    """
    Match TRANSFER_OUT ↔ TRANSFER_IN pairs across different accounts.

    ``all_transactions`` must belong to a single entity.  Non-transfer rows are
    ignored.  The input objects are not modified.

    Returns:
        (matches, unmatched_transfers) — unmatched holds every TRANSFER_OUT /
        TRANSFER_IN leg that found no counterpart.
    """
    outgoing = [
        t for t in all_transactions if t.transaction_type == TransactionType.TRANSFER_OUT
    ]
    incoming = [
        t for t in all_transactions if t.transaction_type == TransactionType.TRANSFER_IN
    ]

    matches: list[ReconciliationMatch] = []
    matched_ids: set = set()

    for out in outgoing:
        best: tuple[int, Transaction] | None = None
        for inc in incoming:
            if inc.id in matched_ids:
                continue
            # Both legs in one account (or account unknown) → not an inter-account transfer.
            if not out.account_id or out.account_id == inc.account_id:
                continue
            if abs(out.amount) != abs(inc.amount):
                continue
            delta = abs((inc.date - out.date).days)
            if delta > tolerance_days:
                continue
            if best is None or delta < best[0]:
                best = (delta, inc)

        if best:
            delta_days, matched_inc = best
            matched_ids.add(matched_inc.id)
            matched_ids.add(out.id)
            matches.append(ReconciliationMatch(out, matched_inc, delta_days))

    unmatched = [t for t in outgoing + incoming if t.id not in matched_ids]
    return matches, unmatched


def matched_leg_ids(matches: list[ReconciliationMatch]) -> list[str]:
    """Ids of both legs of every match (for ``TransactionRepo.mark_reconciled``)."""
    return [tid for m in matches for tid in (m.outgoing.id, m.incoming.id)]


def print_reconciliation_report(
        matches: list[ReconciliationMatch],
        unmatched: list[Transaction],
) -> str:
    """Return a text summary of reconciliation results."""
    lines = [
        f"{'=' * 60}",
        "  RECONCILIATION REPORT",
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
