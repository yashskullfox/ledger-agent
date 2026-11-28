"""
tests/unit/test_reconciler.py  –  card R-03 (reconciler correctness)

* ``reconcile()`` pairs TRANSFER_OUT / TRANSFER_IN legs only across DIFFERENT accounts,
  within ``MATCH_WINDOW_DAYS`` (7) days, and is pure (no mutation of its input, already
  reconciled legs are still paired, so re-runs are stable);
* ``api.reconcile_year`` reads only the active entity's legs (no cross-entity pair) and
  persists ``is_reconciled`` for matched legs only — and only with ``persist=True``;
* ``build_customer_summary`` reports unmatched transfer legs as a count-only next action
  plus the ``UNMATCHED_TRANSFERS`` flag (a warning: it does not block ``CLOSE_READY``).

Synthetic tmp_path databases and pseudonymous names only; no real data.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ledger_agent.core.intelligence.reconciler import (
    MATCH_WINDOW_DAYS,
    matched_leg_ids,
    reconcile,
)
from ledger_agent.core.models import Transaction, TransactionType

FY = 2026
BASE = date(FY, 3, 2)


def _leg(tid, account_id, amount, *, days=0, ttype=None, reconciled=False):
    amount = Decimal(str(amount))
    if ttype is None:
        ttype = TransactionType.TRANSFER_OUT if amount < 0 else TransactionType.TRANSFER_IN
    return Transaction(
        id=tid, account_id=account_id, date=BASE + timedelta(days=days),
        description=f"synthetic leg {tid}", amount=amount, transaction_type=ttype,
        statement_period=f"{FY}-03", coa_code="9000", is_transfer=True,
        is_reconciled=reconciled,
    )


def _pairs(matches):
    return {(m.outgoing.id, m.incoming.id) for m in matches}


# ── 1. reconcile(): accounts, window, purity ──────────────────────────────────

def test_cross_account_pair_matches():
    matches, unmatched = reconcile([_leg("o", "acct-1", "-250"), _leg("i", "acct-2", "250", days=1)])
    assert _pairs(matches) == {("o", "i")}
    assert matches[0].delta_days == 1
    assert unmatched == []


def test_same_account_pair_not_matched():
    out, inc = _leg("o", "acct-1", "-250"), _leg("i", "acct-1", "250", days=1)
    matches, unmatched = reconcile([out, inc])
    assert matches == []
    assert {t.id for t in unmatched} == {"o", "i"}


def test_same_account_candidate_skipped_for_other_account():
    """A closer same-account leg must not shadow a valid cross-account counterpart."""
    legs = [_leg("o", "acct-1", "-250"), _leg("i-same", "acct-1", "250"),
            _leg("i-other", "acct-2", "250", days=3)]
    matches, unmatched = reconcile(legs)
    assert _pairs(matches) == {("o", "i-other")}
    assert [t.id for t in unmatched] == ["i-same"]


def test_legs_without_account_not_matched():
    matches, _ = reconcile([_leg("o", "", "-250"), _leg("i", "", "250")])
    assert matches == []


def test_window_is_named_constant_of_seven_days():
    import inspect

    assert MATCH_WINDOW_DAYS == 7
    default = inspect.signature(reconcile).parameters["tolerance_days"].default
    assert default == MATCH_WINDOW_DAYS


@pytest.mark.parametrize("lag, matched", [(0, True), (7, True), (8, False), (-7, True),
                                          (-8, False)])
def test_window_boundary(lag, matched):
    matches, unmatched = reconcile([_leg("o", "acct-1", "-250"),
                                    _leg("i", "acct-2", "250", days=lag)])
    assert bool(matches) is matched
    assert len(unmatched) == (0 if matched else 2)
    if matched:
        assert matches[0].delta_days == abs(lag)


def test_docstring_no_longer_claims_three_days():
    from ledger_agent.core.intelligence import reconciler

    assert "±3" not in (reconciler.__doc__ or "")
    assert "MATCH_WINDOW_DAYS" in (reconciler.__doc__ or "")


def test_closest_date_wins_and_each_leg_used_once():
    legs = [_leg("o1", "acct-1", "-100"), _leg("o2", "acct-1", "-100", days=5),
            _leg("i1", "acct-2", "100", days=6)]
    matches, unmatched = reconcile(legs)
    assert _pairs(matches) == {("o1", "i1")}  # o1 is processed first; i1 then used
    assert [t.id for t in unmatched] == ["o2"]


def test_amount_must_match_exactly():
    matches, _ = reconcile([_leg("o", "acct-1", "-100.00"), _leg("i", "acct-2", "100.01")])
    assert matches == []


def test_non_transfer_rows_ignored():
    debit = _leg("d", "acct-1", "-100", ttype=TransactionType.DEBIT)
    credit = _leg("c", "acct-2", "100", ttype=TransactionType.CREDIT)
    assert reconcile([debit, credit]) == ([], [])


def test_reconcile_does_not_mutate_inputs():
    legs = [_leg("o", "acct-1", "-250"), _leg("i", "acct-2", "250"),
            _leg("x", "acct-1", "-99")]
    for t in legs:
        t.is_transfer = False  # the old reconcile() flipped this to True on matches
    before = copy.deepcopy(legs)
    matches, _ = reconcile(legs)
    assert len(matches) == 1
    assert legs == before
    assert not any(t.is_reconciled or t.is_transfer for t in legs)


def test_already_reconciled_legs_still_paired_and_rerun_is_stable():
    legs = [_leg("o", "acct-1", "-250", reconciled=True),
            _leg("i", "acct-2", "250", reconciled=True)]
    first, _ = reconcile(legs)
    second, _ = reconcile(legs)
    assert _pairs(first) == _pairs(second) == {("o", "i")}


def test_matched_leg_ids():
    matches, _ = reconcile([_leg("o", "acct-1", "-250"), _leg("i", "acct-2", "250")])
    assert matched_leg_ids(matches) == ["o", "i"]
    assert matched_leg_ids([]) == []


# ── 2. Synthetic DB helpers ───────────────────────────────────────────────────

ENT_A, ENT_B = "ent-r03-a", "ent-r03-b"
NAMES = {ENT_A: "ENTITY_A RECONCASE LLC", ENT_B: "ENTITY_B RECONCASE LLC"}
A_CHK, A_BRK, B_CHK = "acct-r03-a-chk", "acct-r03-a-brk", "acct-r03-b-chk"
ACCTS = {A_CHK: (ENT_A, "checking"), A_BRK: (ENT_A, "brokerage"), B_CHK: (ENT_B, "checking")}


@pytest.fixture
def rdb(tmp_path, monkeypatch):
    from ledger_agent.core.database import AccountRepo, EntityRepo, init_db
    from ledger_agent.core.models import Account, AccountType, Entity

    monkeypatch.setenv("FI_DB_PATH", str(tmp_path / "recon.db"))
    for k in ("FI_PARTNER_1_PL", "FI_PARTNER_2_PL", "FI_PARTNER_1_CAPITAL",
              "FI_PARTNER_2_CAPITAL"):
        monkeypatch.delenv(k, raising=False)
    init_db()
    for eid, name in NAMES.items():
        EntityRepo.upsert(Entity(id=eid, name=name, entity_type="LLC", state="MO"))
    for aid, (eid, atype) in ACCTS.items():
        AccountRepo.upsert(Account(
            id=aid, entity_id=eid, name=f"synthetic {atype}", institution="BANK_X",
            account_type=AccountType(atype), account_number_masked=f"****{aid[-3:]}",
        ))


def _row(tid, account_id, amount, *, day=10, period=f"{FY}-03", ttype=None, coa="9000",
         description=None):
    amount = Decimal(str(amount))
    if ttype is None:
        if coa == "9000":
            ttype = TransactionType.TRANSFER_OUT if amount < 0 else TransactionType.TRANSFER_IN
        else:
            ttype = TransactionType.DEBIT if amount < 0 else TransactionType.CREDIT
    desc = description or f"synthetic row {tid}"
    return Transaction(
        id=tid, account_id=account_id, date=date(int(period[:4]), int(period[5:]), day),
        description=desc, raw_description=desc, amount=amount, transaction_type=ttype,
        statement_period=period, coa_code=coa, coa_name="synthetic",
        is_transfer=coa == "9000",
    )


def _insert(*txns):
    from ledger_agent.core.database import TransactionRepo

    TransactionRepo.bulk_insert(list(txns))


def _reconciled_ids() -> set[str]:
    from ledger_agent.core.database import get_conn

    with get_conn() as conn:
        rows = conn.execute("SELECT id FROM transactions WHERE is_reconciled=1").fetchall()
    return {r[0] for r in rows}


def _seed_two_entities():
    """ENTITY_A: in-entity pair (500, brokerage → checking) + an out-leg (900) whose only
    amount/date match is ENTITY_B's in-leg.  ENTITY_A has more rows, so it is active."""
    _insert(
        _row("a-out-500", A_BRK, "-500", day=10),
        _row("a-in-500", A_CHK, "500", day=11),
        _row("a-out-900", A_CHK, "-900", day=20),
        _row("a-fee", A_CHK, "-12", day=5, coa="5010"),
        _row("b-in-900", B_CHK, "900", day=20),
    )


# ── 3. api.reconcile_year: entity scoping + persistence ───────────────────────

def test_mark_reconciled_sets_only_given_ids(rdb):
    from ledger_agent.core.database import TransactionRepo

    _seed_two_entities()
    assert TransactionRepo.mark_reconciled(["a-out-500", "a-in-500", "a-out-500", ""]) == 2
    assert _reconciled_ids() == {"a-out-500", "a-in-500"}
    assert TransactionRepo.mark_reconciled([]) == 0
    assert TransactionRepo.mark_reconciled(["a-out-500"]) == 1  # idempotent
    assert _reconciled_ids() == {"a-out-500", "a-in-500"}


def test_reconcile_year_never_pairs_across_entities(rdb):
    from ledger_agent.core import api
    from ledger_agent.core.database import TransactionRepo

    _seed_two_entities()
    # Unscoped, the reconciler WOULD pair the cross-entity 900 legs (non-vacuous seed).
    every_row = TransactionRepo.list_for_period(f"{FY}-03")
    assert ("a-out-900", "b-in-900") in _pairs(reconcile(every_row)[0])

    r = api.reconcile_year(FY)
    assert (r.matched, r.unmatched) == (1, 1)


def test_reconcile_year_is_read_only_by_default(rdb):
    from ledger_agent.core import api

    _seed_two_entities()
    api.reconcile_year(FY)
    assert _reconciled_ids() == set()


def test_reconcile_year_persists_matched_legs_only(rdb):
    from ledger_agent.core import api

    _seed_two_entities()
    r = api.reconcile_year(FY, persist=True)
    assert r.matched == 1
    assert _reconciled_ids() == {"a-out-500", "a-in-500"}
    # Re-running after persistence gives the same counts (reconciled legs still paired).
    again = api.reconcile_year(FY, persist=True)
    assert (again.matched, again.unmatched) == (r.matched, r.unmatched)
    assert _reconciled_ids() == {"a-out-500", "a-in-500"}


def test_reconcile_year_same_account_legs_not_persisted(rdb):
    from ledger_agent.core import api

    _insert(_row("s-out", A_CHK, "-75", day=3), _row("s-in", A_CHK, "75", day=4))
    r = api.reconcile_year(FY, persist=True)
    assert (r.matched, r.unmatched) == (0, 2)
    assert _reconciled_ids() == set()


# ── 4. Customer summary: count-only next action + flag ────────────────────────

def _full_year(extra: dict[tuple[str, str], list[Transaction]]):
    """12 tied-out months for A_CHK and A_BRK (one expense line each on A_CHK), plus
    ``extra`` rows keyed by (account, period); snapshots include them."""
    from ledger_agent.core.database import SnapshotRepo
    from ledger_agent.core.models import AccountSnapshot

    for aid in (A_CHK, A_BRK):
        bal = Decimal("1000.00")
        SnapshotRepo.upsert(AccountSnapshot(account_id=aid, statement_period=f"{FY - 1}-12",
                                            ending_balance=bal))
        for m in range(1, 13):
            per = f"{FY}-{m:02d}"
            rows = list(extra.get((aid, per), []))
            if aid == A_CHK:
                rows.append(_row(f"exp-{m}", aid, "-10.00", period=per, coa="5010",
                                 description=f"synthetic expense {m}"))
            _insert(*rows)
            end = bal + sum((t.amount for t in rows), Decimal("0"))
            SnapshotRepo.upsert(AccountSnapshot(account_id=aid, statement_period=per,
                                                ending_balance=end, beginning_balance=bal))
            bal = end


def test_summary_without_unmatched_transfers_has_no_flag(rdb):
    from ledger_agent.core import api

    _full_year({
        (A_BRK, f"{FY}-03"): [_row("m-out", A_BRK, "-500", day=10)],
        (A_CHK, f"{FY}-03"): [_row("m-in", A_CHK, "500", day=11)],
    })
    s = api.build_customer_summary(FY)
    assert "UNMATCHED_TRANSFERS" not in s.confidence_flags
    assert not any("internal transfer leg" in a for a in s.next_actions)
    assert _reconciled_ids() == set()  # the summary never writes


def test_summary_reports_unmatched_transfer_count_only(rdb):
    from ledger_agent.core import api

    secret_desc = "QUILLBROOK WIRE MEMO"
    _full_year({
        # matched pair across accounts
        (A_BRK, f"{FY}-03"): [_row("m-out", A_BRK, "-500", day=10)],
        (A_CHK, f"{FY}-03"): [
            _row("m-in", A_CHK, "500", day=11),
            # same-account round trip: two unmatched legs (statement still ties out)
            _row("u-out", A_CHK, "-345.67", day=14, description=secret_desc),
            _row("u-in", A_CHK, "345.67", day=15, description=secret_desc),
        ],
        # one stray out-leg on the brokerage
        (A_BRK, f"{FY}-06"): [_row("u-out2", A_BRK, "-222.22", day=9,
                                   description=secret_desc)],
    })
    s = api.build_customer_summary(FY)

    assert "UNMATCHED_TRANSFERS" in s.confidence_flags
    hits = [a for a in s.next_actions if "internal transfer leg" in a]
    assert hits == [
        "3 internal transfer leg(s) have no matching counterpart — check the other "
        "account's statement or whether it is a third-party payment."
    ]
    blob = " ".join(s.next_actions)
    for leaked in (secret_desc, "345.67", "222.22", "500", f"{FY}-03-14", f"{FY}-06-09"):
        assert leaked not in blob
    # A warning, like AUDIT_WARN: it does not block close-readiness by itself.
    assert "AUDIT_FAIL" not in s.confidence_flags
    assert "CLOSE_READY" in s.confidence_flags
    assert _reconciled_ids() == set()


def test_summary_unmatched_count_matches_reconcile_year(rdb):
    from ledger_agent.core import api

    _full_year({(A_CHK, f"{FY}-02"): [_row("u1", A_CHK, "-40", day=3),
                                      _row("u2", A_CHK, "40", day=4)]})
    assert api.reconcile_year(FY).unmatched == 2
    s = api.build_customer_summary(FY)
    assert any(a.startswith("2 internal transfer leg(s)") for a in s.next_actions)
