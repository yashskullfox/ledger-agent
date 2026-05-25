"""
accounting/continuity.py  –  Fiscal-year period continuity checker (R-72 / ARCH-31)
"""
from __future__ import annotations

from decimal import Decimal
from typing import List, Optional, Tuple


def check_period_continuity(
    entity_id: str,
    period_a: str,
    period_b: str,
) -> Optional[Decimal]:
    """
    Compare the total ending balance of period_a against the total ending balance
    of period_b for the same entity. Returns the delta (period_b - period_a), or
    None if either period has no snapshot data.

    A delta of 0 means clean carry-forward. A non-zero delta means untracked
    prior-period adjustments exist and should be materialised as
    TransactionType.PRIOR_PERIOD_ADJUSTMENT rows.

    Args:
        entity_id: The entity to check.
        period_a: The earlier period (e.g. "2024-12").
        period_b: The later period (e.g. "2025-01").

    Returns:
        Decimal delta (period_b total - period_a total), or None if data absent.
    """
    from ledger_agent.core.database import SnapshotRepo, AccountRepo

    accounts = AccountRepo.list_for_entity(entity_id)
    if not accounts:
        return None

    def _total_ending(period: str) -> Optional[Decimal]:
        snapshots = SnapshotRepo.list_for_entity(entity_id)
        period_snaps = [s for s in snapshots if s.statement_period == period]
        if not period_snaps:
            return None
        return sum((s.ending_balance for s in period_snaps), Decimal("0"))

    closing_a = _total_ending(period_a)
    closing_b = _total_ending(period_b)

    if closing_a is None or closing_b is None:
        return None

    return closing_b - closing_a


def list_discontinuities(
    entity_id: str,
    periods: List[str],
) -> List[Tuple[str, str, Decimal]]:
    """
    Check all consecutive period pairs in `periods` for carry-forward gaps.

    Returns a list of (period_a, period_b, delta) tuples where delta != 0.
    An empty list means perfect continuity across all periods.
    """
    results = []
    sorted_periods = sorted(periods)
    for i in range(len(sorted_periods) - 1):
        pa = sorted_periods[i]
        pb = sorted_periods[i + 1]
        delta = check_period_continuity(entity_id, pa, pb)
        if delta is not None and delta != Decimal("0"):
            results.append((pa, pb, delta))
    return results


def materialise_prior_period_adjustments(
        entity_id: str,
        period_a: str,
        period_b: str,
        *,
        dry_run: bool = False,
) -> Optional[Decimal]:
    """
    If a carry-forward gap exists between period_a and period_b, create a
    ``TransactionType.PRIOR_PERIOD_ADJUSTMENT`` transaction in the database
    to reconcile it.

    Args:
        entity_id: The entity to check.
        period_a: The earlier period (e.g. "2024-12").
        period_b: The later period (e.g. "2025-01").
        dry_run: If True, compute and return the delta but do NOT write to DB.

    Returns:
        The delta written (or that would be written), or None if no data.
    """
    from ledger_agent.core.database import AccountRepo, TransactionRepo
    from ledger_agent.core.models import Transaction, TransactionType
    from datetime import date as date_
    import uuid

    delta = check_period_continuity(entity_id, period_a, period_b)
    if delta is None or delta == Decimal("0"):
        return delta

    if dry_run:
        return delta

    # Resolve the first account for this entity to attach the adjustment to
    accounts = AccountRepo.list_for_entity(entity_id)
    if not accounts:
        return None
    account_id = accounts[0].id

    # Parse first day of period_b as the adjustment date
    try:
        year, month = (int(x) for x in period_b.split("-", 1))
        adj_date = date_(year, month, 1)
    except (ValueError, AttributeError):
        return None

    txn = Transaction(
        id=str(uuid.uuid4()),
        account_id=account_id,
        date=adj_date,
        description=f"Prior-period adjustment: {period_a} \u2192 {period_b}",
        amount=abs(delta),
        transaction_type=TransactionType.PRIOR_PERIOD_ADJUSTMENT,
        statement_period=period_b,
        is_transfer=False,
        coa_code="9999",
    )
    TransactionRepo.bulk_insert([txn])
    return delta
