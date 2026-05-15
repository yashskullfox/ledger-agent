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
