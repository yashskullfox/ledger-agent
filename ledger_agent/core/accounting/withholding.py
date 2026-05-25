"""
accounting/withholding.py  –  Partner withholding reclassification (ARCH-29)
─────────────────────────────────────────────────────────────────────────────
For pass-through entities (LLCs taxed as partnerships), estimated tax
payments made from the entity's bank account are partner draws, NOT
entity-level tax expenses (the entity pays no federal/state corporate tax).

This module audits the transaction table for any transactions with
coa_code in the ``WITHHOLDING_EXPENSE_CODES`` set (e.g. 5050) that match
the ``WITHHOLDING_KEYWORDS`` pattern, and reclassifies them to 3040
(Members Distributions / Owner Draws).

Usage::

    from ledger_agent.core.accounting.withholding import reclassify_partner_withholding
    count = reclassify_partner_withholding(entity_id="...")
"""
from __future__ import annotations

import logging
from typing import List

log = logging.getLogger(__name__)

# COA codes that INCORRECTLY classify pass-through tax payments as expenses
WITHHOLDING_EXPENSE_CODES: frozenset = frozenset({"5050", "5055"})

# Target COA code for pass-through entity tax payments
WITHHOLDING_TARGET_CODE = "3040"  # Members Distributions / Owner Draws

# Keywords in transaction description that identify tax payments
WITHHOLDING_KEYWORDS: tuple = (
    "usataxpymt",
    "irs",
    "estimated tax",
    "owner draw",
    "distribution",
    "member draw",
)


def find_misclassified_withholding(
    entity_id: str,
    fiscal_year: int,
) -> List[str]:
    """
    Return transaction IDs that are incorrectly coded as entity-level tax
    expense when they should be partner draws (3040).

    A transaction qualifies if:
    - Its coa_code is in WITHHOLDING_EXPENSE_CODES
    - Its description matches at least one WITHHOLDING_KEYWORD (case-insensitive)
    - Its account belongs to entity_id

    Returns a list of transaction IDs.
    """
    from ledger_agent.core.database import AccountRepo, get_conn

    accounts = AccountRepo.list_for_entity(entity_id)
    if not accounts:
        return []
    account_ids = [a.id for a in accounts]
    placeholders = ",".join("?" * len(account_ids))

    prefix = f"{fiscal_year}-%"
    candidates: List[str] = []

    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, description, coa_code FROM transactions "
            f"WHERE account_id IN ({placeholders}) "
            f"  AND coa_code IN ({','.join('?' * len(WITHHOLDING_EXPENSE_CODES))}) "
            f"  AND statement_period LIKE ?",
            (*account_ids, *WITHHOLDING_EXPENSE_CODES, prefix),
        ).fetchall()

    for txn_id, desc, code in rows:
        desc_lower = (desc or "").lower()
        if any(kw in desc_lower for kw in WITHHOLDING_KEYWORDS):
            candidates.append(txn_id)

    return candidates


def reclassify_partner_withholding(
    entity_id: str,
    fiscal_year: int,
    *,
    dry_run: bool = False,
) -> int:
    """
    Reclassify pass-through tax-payment transactions from expense codes to
    3040 (Members Distributions).

    Args:
        entity_id: The entity whose transactions to audit.
        fiscal_year: The year to audit.
        dry_run: If True, return the count without writing changes.

    Returns:
        Number of transactions reclassified (or that would be reclassified).
    """
    txn_ids = find_misclassified_withholding(entity_id, fiscal_year)
    if not txn_ids or dry_run:
        if txn_ids:
            log.info(
                "ARCH-29 dry_run: %d transaction(s) would be reclassified to %s",
                len(txn_ids), WITHHOLDING_TARGET_CODE,
            )
        return len(txn_ids)

    from ledger_agent.core.database import get_conn

    with get_conn() as conn:
        conn.executemany(
            "UPDATE transactions SET coa_code = ? WHERE id = ?",
            [(WITHHOLDING_TARGET_CODE, tid) for tid in txn_ids],
        )

    log.info(
        "ARCH-29: reclassified %d transaction(s) → %s (Members Distributions)",
        len(txn_ids), WITHHOLDING_TARGET_CODE,
    )
    return len(txn_ids)
