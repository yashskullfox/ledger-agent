"""
tests/unit/test_cli_entity_isolation.py  –  card R-09 (ARCH-30 entity isolation, CLI)

The interactive / legacy CLI commands in ``ledger_agent/cli/commands.py`` must only
touch the selected entity's data when another entity has rows in the SAME period:

* ``cmd_transactions`` — the listing and the CSV export carry only the selected
  entity's transactions (the bug: ``list_for_period(period)`` read every entity);
* ``cmd_import`` post-import reconcile — transfers are paired only within the entity
  that owns the imported account; an amount/date-matching pair that straddles two
  entities is never matched;
* ``cmd_classify`` — only the selected entity's unclassified rows are offered;
* a single-entity DB behaves exactly as before (same rows, same reconcile result).

Commands are driven non-interactively (prompts monkeypatched).  CSV exports go to
tmp_path.  Synthetic tmp_path databases and pseudonymous names only; no real data.
"""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ledger_agent.cli import commands

FY = 2026
PERIOD = f"{FY}-01"

ENT_A = "ent-r09-a"
ENT_B = "ent-r09-b"
NAMES = {ENT_A: "ENTITY_A HOLDINGS LLC", ENT_B: "ENTITY_B VENTURES LLC"}

ACCTS = {
    # id: (entity, institution, account type, masked number)
    "acct-r09-a-chk": (ENT_A, "BANK_X", "checking", "1111"),
    "acct-r09-a-brk": (ENT_A, "BROKER_Y", "brokerage", "2222"),
    "acct-r09-b-chk": (ENT_B, "BANK_X2", "checking", "3333"),
    "acct-r09-b-brk": (ENT_B, "BROKER_Z", "brokerage", "4444"),
}

# id: (account, day, description, amount, type, coa_code)
TXNS = {
    # ENTITY_A — an in-entity transfer pair (500), a cross-entity out-leg (900),
    # one classified and one unclassified ordinary row.
    "tx-a-out-500": ("acct-r09-a-brk", 10, "alpha sweep out", "-500", "transfer_out", "9000"),
    "tx-a-in-500": ("acct-r09-a-chk", 11, "alpha sweep in", "500", "transfer_in", "9000"),
    "tx-a-out-900": ("acct-r09-a-chk", 20, "alpha wire out", "-900", "transfer_out", ""),
    "tx-a-fee": ("acct-r09-a-chk", 5, "alpha software plan", "-61", "debit", "5010"),
    "tx-a-unc": ("acct-r09-a-chk", 6, "alpha mystery charge", "-23", "debit", ""),
    # ENTITY_B — its own in-entity pair (700), the cross-entity in-leg (900, same
    # day and amount as A's out-leg), one classified and one unclassified row.
    "tx-b-out-700": ("acct-r09-b-brk", 12, "bravo sweep out", "-700", "transfer_out", "9000"),
    "tx-b-in-700": ("acct-r09-b-chk", 12, "bravo sweep in", "700", "transfer_in", "9000"),
    "tx-b-in-900": ("acct-r09-b-chk", 20, "bravo wire in", "900", "transfer_in", ""),
    "tx-b-fee": ("acct-r09-b-chk", 7, "bravo monthly hosting", "-77", "debit", "5010"),
    "tx-b-unc": ("acct-r09-b-chk", 8, "bravo mystery charge", "-41", "debit", ""),
}


def _ids_of(entity_id: str) -> set[str]:
    return {tid for tid, row in TXNS.items() if ACCTS[row[0]][0] == entity_id}


def _txn(tid: str):
    from ledger_agent.core.models import Transaction, TransactionType

    acct, day, desc, amt, ttype, code = TXNS[tid]
    return Transaction(
        id=tid, account_id=acct, date=date(FY, 1, day), description=desc,
        raw_description=desc, amount=Decimal(amt),
        transaction_type=TransactionType(ttype), statement_period=PERIOD,
        coa_code=code, coa_name="synthetic" if code else "",
        is_transfer=code == "9000",
    )


def _seed(entity_ids, skip_txns=()) -> None:
    from ledger_agent.core.database import (
        AccountRepo,
        EntityRepo,
        SnapshotRepo,
        TransactionRepo,
        init_db,
    )
    from ledger_agent.core.models import Account, AccountSnapshot, AccountType, Entity

    init_db()
    for eid in entity_ids:
        EntityRepo.upsert(Entity(id=eid, name=NAMES[eid], entity_type="LLC", state="MO"))
    for aid, (eid, inst, atype, masked) in ACCTS.items():
        if eid not in entity_ids:
            continue
        AccountRepo.upsert(Account(
            id=aid, entity_id=eid, name=f"{inst} account", institution=inst,
            account_type=AccountType(atype), account_number_masked=masked,
        ))
        SnapshotRepo.upsert(AccountSnapshot(
            account_id=aid, statement_period=PERIOD, ending_balance=Decimal("1000"),
        ))
    TransactionRepo.bulk_insert([
        _txn(tid) for tid, row in TXNS.items()
        if ACCTS[row[0]][0] in entity_ids and tid not in skip_txns
    ])


@pytest.fixture
def two_entity_db(db):
    _seed((ENT_A, ENT_B))
    return db


@pytest.fixture
def single_entity_db(db):
    _seed((ENT_A,))
    return db


@pytest.fixture
def prompts(monkeypatch):
    """Non-interactive prompts: entity choice + CSV-export answer are settable."""
    state = SimpleNamespace(entity_name=NAMES[ENT_A], export_csv=False, selects=[])

    def _select(prompt, choices, default=None):
        state.selects.append(prompt)
        if prompt == "Select entity:":
            assert state.entity_name in choices
            return state.entity_name
        return default if default is not None else choices[0]

    monkeypatch.setattr(commands, "ask_select", _select)
    monkeypatch.setattr(commands, "ask_confirm", lambda prompt, default=True: state.export_csv)
    monkeypatch.setattr(commands, "wizard_entity", lambda: pytest.fail("setup wizard ran"))
    return state


@pytest.fixture
def rendered(monkeypatch):
    """Capture what cmd_transactions renders (instead of printing it)."""
    from ledger_agent.core.reports import renderer

    calls: list[list] = []
    monkeypatch.setattr(renderer, "render_transactions",
                        lambda txns, title="", show_coa=True: calls.append(list(txns)))
    return calls


@pytest.fixture
def csv_dir(tmp_path, monkeypatch):
    """Redirect the transactions CSV export to tmp_path (never data/exports)."""
    from ledger_agent.core.reports import renderer

    out = tmp_path / "exports"
    real = renderer.export_transactions_csv
    monkeypatch.setattr(renderer, "export_transactions_csv",
                        lambda txns, period: real(txns, period, out_dir=out))
    return out


def _csv_descriptions(path) -> list[str]:
    with open(path, newline="", encoding="utf-8") as f:
        return [row["Description"] for row in csv.DictReader(f)]


def _desc(tids) -> set[str]:
    return {TXNS[t][2] for t in tids}


# ── 0. The seed really mixes entities in one period (non-vacuous) ─────────────

def test_seed_mixes_entities_in_one_period(two_entity_db):
    from ledger_agent.core.database import TransactionRepo
    from ledger_agent.core.intelligence.reconciler import reconcile

    rows = TransactionRepo.list_for_period(PERIOD)
    assert {t.id for t in rows} == set(TXNS)
    # Unscoped, the reconciler WOULD pair the cross-entity 900 legs.
    matches, _ = reconcile(rows)
    pairs = {(m.outgoing.id, m.incoming.id) for m in matches}
    assert ("tx-a-out-900", "tx-b-in-900") in pairs


# ── 1. cmd_transactions: listing + CSV are the selected entity's rows ─────────

@pytest.mark.parametrize("eid", [ENT_A, ENT_B])
def test_transactions_listing_only_selected_entity(two_entity_db, prompts, rendered,
                                                   csv_dir, eid):
    prompts.entity_name = NAMES[eid]
    commands.cmd_transactions(PERIOD)
    assert "Select entity:" in prompts.selects
    assert len(rendered) == 1
    assert {t.id for t in rendered[0]} == _ids_of(eid)
    assert not csv_dir.exists()  # export declined → nothing written


@pytest.mark.parametrize("eid", [ENT_A, ENT_B])
def test_transactions_csv_export_only_selected_entity(two_entity_db, prompts, rendered,
                                                      csv_dir, eid):
    prompts.entity_name = NAMES[eid]
    prompts.export_csv = True
    commands.cmd_transactions(PERIOD)
    path = csv_dir / f"transactions_{PERIOD}.csv"
    assert path.exists()
    descs = _csv_descriptions(path)
    assert sorted(descs) == sorted(_desc(_ids_of(eid)))
    other = ENT_B if eid == ENT_A else ENT_A
    assert not set(descs) & _desc(_ids_of(other))
    # CSV rows are exactly the rows listed on screen.
    assert sorted(descs) == sorted(t.description for t in rendered[0])


def test_transactions_period_prompt_uses_selected_entity(two_entity_db, prompts, rendered,
                                                         csv_dir):
    prompts.entity_name = NAMES[ENT_B]
    commands.cmd_transactions()  # period chosen from the entity's snapshot periods
    assert "Select period:" in prompts.selects
    assert {t.id for t in rendered[0]} == _ids_of(ENT_B)


# ── 2. cmd_classify: only the selected entity's unclassified rows ─────────────

@pytest.fixture
def classify_spy(monkeypatch):
    from ledger_agent.core.intelligence import classifier

    seen: list[list] = []

    def _spy(txns, prompt_fn=None, confidence=0.0):
        seen.append(list(txns))
        return list(txns), 0, 0

    monkeypatch.setattr(classifier, "classify_batch", _spy)
    return seen


def _unclassified(eid) -> set[str]:
    return {t for t in _ids_of(eid) if not TXNS[t][5]}


@pytest.mark.parametrize("eid", [ENT_A, ENT_B])
def test_classify_sees_only_selected_entity(two_entity_db, prompts, classify_spy, eid):
    prompts.entity_name = NAMES[eid]
    commands.cmd_classify()
    assert "Select entity:" in prompts.selects
    assert len(classify_spy) == 1
    assert {t.id for t in classify_spy[0]} == _unclassified(eid)


def test_classify_all_done_for_entity_skips_batch(two_entity_db, prompts, classify_spy):
    """ENTITY_B fully classified while ENTITY_A still has rows → nothing offered."""
    from ledger_agent.core.database import TransactionRepo

    for tid in _unclassified(ENT_B):
        TransactionRepo.update_coa(tid, "5010", "synthetic")
    prompts.entity_name = NAMES[ENT_B]
    commands.cmd_classify()
    assert classify_spy == []


def test_classify_without_entity_fails_closed(db, prompts, classify_spy):
    commands.cmd_classify()  # no entity: no wizard (prompts fixture fails on it), no batch
    assert classify_spy == []


# ── 3. cmd_import: post-import reconcile stays inside the account's entity ────

@pytest.fixture
def reconcile_spy(monkeypatch):
    from ledger_agent.core.intelligence import reconciler

    calls: list[dict] = []
    real = reconciler.reconcile

    def _spy(all_transactions, tolerance_days=7):
        matches, unmatched = real(all_transactions, tolerance_days)
        calls.append({
            "input": {t.id for t in all_transactions},
            "pairs": {(m.outgoing.id, m.incoming.id) for m in matches},
            "unmatched": {t.id for t in unmatched},
        })
        return matches, unmatched

    monkeypatch.setattr(reconciler, "reconcile", _spy)
    return calls


def _fake_import(monkeypatch, tmp_path, account_id, tids):
    """Make cmd_import 'parse' a statement for ``account_id`` holding ``tids``."""
    import ledger_agent.core.parsers  # noqa: F401  – same auto-discovery cmd_import runs
    from ledger_agent.core.models import StatementType
    from ledger_agent.core.parsers.base import BaseStatementParser
    from ledger_agent.core.parsers.registry import ParserRegistry

    _, inst, atype, masked = ACCTS[account_id]
    stmt = SimpleNamespace(
        statement_period=PERIOD, institution=inst, account_number_masked=masked,
        statement_type=StatementType.BANK_CHECKING if atype == "checking"
        else StatementType.BROKERAGE,
        parser_id="synthetic_r09", positions=[], snapshot=None,
        transactions=[_txn(t) for t in tids],
    )

    class _FakeParser:
        INSTITUTION = inst

        def parse(self, _path):
            return stmt

    monkeypatch.setattr(BaseStatementParser, "extract_text", staticmethod(lambda p: ""))
    monkeypatch.setattr(ParserRegistry, "detect_or_raise", classmethod(lambda cls, t: _FakeParser))
    pdf = tmp_path / "synthetic_statement.pdf"
    pdf.write_bytes(b"%PDF-synthetic")
    return str(pdf)


A_CHK_TXNS = ("tx-a-in-500", "tx-a-out-900", "tx-a-fee", "tx-a-unc")


@pytest.mark.parametrize("selected", [ENT_A, ENT_B])
def test_import_reconciles_only_within_owning_entity(db, tmp_path, monkeypatch, prompts,
                                                     classify_spy, reconcile_spy, selected):
    """Importing ENTITY_A's checking statement pairs A's 500 transfer but never the
    cross-entity 900 legs — even when ENTITY_B is the entity picked at the prompt."""
    _seed((ENT_A, ENT_B), skip_txns=A_CHK_TXNS)
    prompts.entity_name = NAMES[selected]
    pdf = _fake_import(monkeypatch, tmp_path, "acct-r09-a-chk", A_CHK_TXNS)

    commands.cmd_import(pdf)

    assert len(reconcile_spy) == 1
    call = reconcile_spy[0]
    assert call["input"] == _ids_of(ENT_A)
    assert call["pairs"] == {("tx-a-out-500", "tx-a-in-500")}
    assert call["unmatched"] == {"tx-a-out-900"}
    assert not any("tx-b" in tid for pair in call["pairs"] for tid in pair)


def test_import_into_entity_b_account_reconciles_b_only(db, tmp_path, monkeypatch, prompts,
                                                        classify_spy, reconcile_spy):
    b_chk = ("tx-b-in-700", "tx-b-in-900", "tx-b-fee", "tx-b-unc")
    _seed((ENT_A, ENT_B), skip_txns=b_chk)
    prompts.entity_name = NAMES[ENT_B]
    pdf = _fake_import(monkeypatch, tmp_path, "acct-r09-b-chk", b_chk)

    commands.cmd_import(pdf)

    call = reconcile_spy[0]
    assert call["input"] == _ids_of(ENT_B)
    assert call["pairs"] == {("tx-b-out-700", "tx-b-in-700")}
    assert call["unmatched"] == {"tx-b-in-900"}


# ── 4. Single-entity DB: behaviour unchanged ──────────────────────────────────

def test_single_entity_transactions_unchanged(single_entity_db, prompts, rendered, csv_dir):
    from ledger_agent.core.database import TransactionRepo

    prompts.export_csv = True
    commands.cmd_transactions(PERIOD)
    assert "Select entity:" not in prompts.selects  # no entity prompt, as before
    before = TransactionRepo.list_for_period(PERIOD)  # the pre-R-09 query
    assert {t.id for t in rendered[0]} == {t.id for t in before} == _ids_of(ENT_A)
    dates = [t.date for t in rendered[0]]
    assert dates == sorted(dates)
    descs = _csv_descriptions(csv_dir / f"transactions_{PERIOD}.csv")
    assert sorted(descs) == sorted(t.description for t in before)


def test_single_entity_classify_unchanged(single_entity_db, prompts, classify_spy):
    from ledger_agent.core.database import TransactionRepo

    commands.cmd_classify()
    assert "Select entity:" not in prompts.selects
    before = TransactionRepo.list_unclassified()  # the pre-R-09 query
    assert {t.id for t in classify_spy[0]} == {t.id for t in before} == _unclassified(ENT_A)


def test_single_entity_import_reconcile_unchanged(db, tmp_path, monkeypatch, prompts,
                                                  classify_spy, reconcile_spy):
    from ledger_agent.core.database import TransactionRepo

    _seed((ENT_A,), skip_txns=A_CHK_TXNS)
    pdf = _fake_import(monkeypatch, tmp_path, "acct-r09-a-chk", A_CHK_TXNS)

    commands.cmd_import(pdf)

    assert "Select entity:" not in prompts.selects
    call = reconcile_spy[0]
    unscoped = {t.id for t in TransactionRepo.list_for_period(PERIOD)}  # pre-R-09 input
    assert call["input"] == unscoped == _ids_of(ENT_A)
    assert call["pairs"] == {("tx-a-out-500", "tx-a-in-500")}
    assert call["unmatched"] == {"tx-a-out-900"}


# ── 5. R-03: post-import reconcile persists is_reconciled for matched legs only ─

def _reconciled_ids() -> set[str]:
    from ledger_agent.core.database import get_conn

    with get_conn() as conn:
        rows = conn.execute("SELECT id FROM transactions WHERE is_reconciled=1").fetchall()
    return {r[0] for r in rows}


def test_import_persists_is_reconciled_for_matched_legs_only(db, tmp_path, monkeypatch,
                                                            prompts, classify_spy):
    _seed((ENT_A, ENT_B), skip_txns=A_CHK_TXNS)
    pdf = _fake_import(monkeypatch, tmp_path, "acct-r09-a-chk", A_CHK_TXNS)

    commands.cmd_import(pdf)

    # A's in-entity 500 pair only: never A's 900 out-leg, never ENTITY_B's legs.
    assert _reconciled_ids() == {"tx-a-out-500", "tx-a-in-500"}
