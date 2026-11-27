"""
intelligence/owner_draws.py  –  Owner draws to the owners' own accounts (C-04 / D2)
─────────────────────────────────────────────────────────────────────────────────────
Owner decision D2: partners are not on payroll. Money moved out of a business
account to an owner's *own* account is an owner draw / distribution (COA 3040),
not an internal transfer (9000) and not payroll.

The owners' accounts are identified by their last 4 digits, kept only in the
gitignored ``private/institutions.py``::

    OWNER_DRAW_ACCOUNTS = {"last4": [...]}

This module is the single shared matcher used by the brokerage parsers and the
classifier:

* :func:`account_refs` finds 4-digit groups that are *immediately* preceded by
  an account marker (``x1234``, ``*1234``, ``...1234``, ``acct 1234``,
  ``account ending 1234``, ``ending in 1234``, ``ACCT# 1234``). Dates, amounts,
  check numbers and the tail of a longer digit run never match.
* :func:`apply_owner_draw_rule` recodes a matching outflow to 3040 (not a
  transfer), or tags an outgoing transfer to an unlisted, non-entity account
  with ``owner_draw_candidate`` (coding unchanged) for the self-audit.

The matched digits are never logged or echoed.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from ledger_agent.core.models import Transaction, TransactionType
from ledger_agent.core.privacy import ACCOUNT_REF_RE

OWNER_DRAW_CODE = "3040"
OWNER_DRAW_NAME = "Members Distributions / Owner Draws"
OWNER_DRAW_CANDIDATE_TAG = "owner_draw_candidate"
TRANSFER_CODE = "9000"


def _load_listed_last4() -> frozenset[str]:
    """Read ``OWNER_DRAW_ACCOUNTS["last4"]`` from the private config.

    A missing ``private/institutions.py``, a missing ``OWNER_DRAW_ACCOUNTS`` name
    (``from x import y`` raises ImportError for both) or a malformed value all
    yield an empty set, so nothing is reclassified.
    """
    try:
        from private.institutions import OWNER_DRAW_ACCOUNTS as _CFG  # type: ignore
    except ImportError:
        return frozenset()
    try:
        values = _CFG.get("last4") or []
        return frozenset(
            s for s in (str(v).strip() for v in values) if len(s) == 4 and s.isdigit()
        )
    except (AttributeError, TypeError):
        return frozenset()


# Last-4 digits of the owners' own accounts. Module-level so tests can monkeypatch it.
OWNER_DRAW_LAST4: frozenset[str] = _load_listed_last4()

# A 4-digit group immediately preceded by an account marker, case-insensitive.
# The grammar lives in core/privacy.py (ACCOUNT_MARKER_PATTERN / ACCOUNT_REF_RE)
# so this matcher and the egress firewall (P-05) agree on what an account
# reference is:
#   x1234 / XXXX1234 / XXXX-1234   (x not part of a word or number)
#   *1234 / **1234 / ** 1234   (any run of stars)
#   ...1234 / …1234
#   acct 1234 / ACCT# 1234 / acct no. 1234 / account 1234 / account ending (in) 1234
#   ending in 1234
# The group must not be followed by another digit (or "-digit"), so the last 4
# digits of a longer run, dates (01/15, 2026-01-14) and amounts never match.
_ACCOUNT_REF_RE = ACCOUNT_REF_RE


def account_refs(text: str) -> list[str]:
    """Return every marker-preceded 4-digit account reference in ``text``."""
    if not text:
        return []
    return [m.group(1) for m in _ACCOUNT_REF_RE.finditer(text)]


def _last4(masked: str) -> str:
    digits = re.sub(r"\D", "", masked or "")
    return digits[-4:] if len(digits) >= 4 else ""


def known_account_last4(account_id: str | None = None) -> frozenset[str]:
    """Last-4 of the entity's own accounts, best effort.

    With ``account_id`` (a stored account) → the accounts of that account's entity.
    Without it (parse time, entity unknown) → every account in the DB. Returns an
    empty set when the DB does not exist yet (never creates it) or cannot be read.
    """
    try:
        from ledger_agent.core.database import AccountRepo, EntityRepo, _resolve_db_path

        if not _resolve_db_path(None).exists():
            return frozenset()
        if account_id:
            acct = AccountRepo.get_by_id(account_id)
            entity_ids = [acct.entity_id] if acct else []
        else:
            entity_ids = [e.id for e in EntityRepo.list_all()]
        out = set()
        for eid in entity_ids:
            for a in AccountRepo.list_for_entity(eid):
                tail = _last4(a.account_number_masked)
                if tail:
                    out.add(tail)
        return frozenset(out)
    except Exception:  # best effort: a missing/old schema must never break parsing
        return frozenset()


def _match_text(txn: Transaction, context: str = "") -> str:
    return " ".join(p for p in (txn.description, txn.raw_description, context) if p)


def is_listed_owner_draw(txn: Transaction, context: str = "") -> bool:
    """True when ``txn`` is an outflow referencing a listed owner account."""
    if not OWNER_DRAW_LAST4 or txn.amount >= 0:
        return False
    return any(r in OWNER_DRAW_LAST4 for r in account_refs(_match_text(txn, context)))


def _is_transfer_like(txn: Transaction) -> bool:
    return (
        txn.is_transfer
        or txn.coa_code == TRANSFER_CODE
        or txn.transaction_type == TransactionType.TRANSFER_OUT
    )


def apply_owner_draw_rule(
    txn: Transaction,
    *,
    own_last4: Iterable[str] | None = None,
    context: str = "",
) -> str | None:
    """Apply the D2 owner-draw rule to one transaction, in place.

    Returns ``"draw"`` (recoded to 3040, not a transfer), ``"candidate"`` (outgoing
    transfer to an unlisted account outside the entity; coding unchanged, tagged
    ``owner_draw_candidate``) or ``None`` (untouched).

    ``context`` is extra statement text (e.g. a wrapped continuation line) searched
    for account references. ``own_last4`` is the entity's own accounts; when None
    it is looked up lazily from the DB (only if a candidate needs ruling out).
    """
    if txn.amount >= 0:
        return None
    refs = account_refs(_match_text(txn, context))
    if not refs:
        return None
    if any(r in OWNER_DRAW_LAST4 for r in refs):
        txn.coa_code = OWNER_DRAW_CODE
        txn.coa_name = OWNER_DRAW_NAME
        txn.is_transfer = False
        if txn.transaction_type == TransactionType.TRANSFER_OUT:
            txn.transaction_type = TransactionType.DEBIT
        if OWNER_DRAW_CANDIDATE_TAG in txn.tags:
            txn.tags = [t for t in txn.tags if t != OWNER_DRAW_CANDIDATE_TAG]
        return "draw"
    if not _is_transfer_like(txn):
        return None
    own = (
        frozenset(own_last4)
        if own_last4 is not None
        else known_account_last4(txn.account_id or None)
    )
    if all(r in own for r in refs):
        return None  # transfer between the entity's own accounts
    if OWNER_DRAW_CANDIDATE_TAG not in txn.tags:
        txn.tags = [*txn.tags, OWNER_DRAW_CANDIDATE_TAG]
    return "candidate"
