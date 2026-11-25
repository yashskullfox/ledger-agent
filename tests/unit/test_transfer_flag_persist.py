"""
tests/unit/test_transfer_flag_persist.py  –  G-00 group 6.

* ``SCHEMA_VERSION = 8``: ``init_db`` on a v7 DB backfills ``is_transfer=1`` for rows
  coded ``9000`` (inter-account transfer).
* ``TransactionRepo.update_coa_with_meta(..., is_transfer=)`` persists the flag;
  code ``9000`` always implies a transfer; ``is_transfer=None`` never clears it.
* ``classify_batch`` passes classifier-detected transfers through (add-only).

Synthetic tmp_path DB only; pseudonymous entity / institution names.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture
def xfer_db(tmp_path, monkeypatch):
    from ledger_agent.core.database import AccountRepo, EntityRepo, init_db
    from ledger_agent.core.models import Account, AccountType, Entity

    db_file = tmp_path / "xfer.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    init_db(db_file)
    EntityRepo.upsert(
        Entity(id="ent-xfer", name="ENTITY_A", entity_type="LLC", state="MO"), db_file
    )
    AccountRepo.upsert(
        Account(
            id="acct-xfer",
            entity_id="ent-xfer",
            name="Operating",
            institution="Bank X",
            account_type=AccountType.CHECKING,
            account_number_masked="0000",
        ),
        db_file,
    )
    return db_file


def _insert(db_file, tx_id: str, description: str, code: str = "", is_transfer: bool = False):
    from ledger_agent.core.database import TransactionRepo
    from ledger_agent.core.models import Transaction, TransactionType

    TransactionRepo.bulk_insert(
        [
            Transaction(
                id=tx_id,
                account_id="acct-xfer",
                date=date(2026, 2, 10),
                description=description,
                amount=Decimal("-25"),
                transaction_type=TransactionType.DEBIT,
                statement_period="2026-02",
                coa_code=code,
                coa_name=code,
                is_transfer=is_transfer,
            )
        ],
        db_file,
    )


def _flag(db_file, tx_id: str) -> int:
    con = sqlite3.connect(str(db_file))
    try:
        return int(
            con.execute("SELECT is_transfer FROM transactions WHERE id=?", (tx_id,)).fetchone()[0]
        )
    finally:
        con.close()


def _version(db_file) -> str:
    con = sqlite3.connect(str(db_file))
    try:
        return str(con.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0])
    finally:
        con.close()


def _get(tx_id: str):
    from ledger_agent.core.database import TransactionRepo

    txn = TransactionRepo.get_by_id(tx_id)
    assert txn is not None
    return txn


def test_schema_version_is_8():
    from ledger_agent.core.database import SCHEMA_VERSION

    assert SCHEMA_VERSION == 8


def test_v7_to_v8_migration_backfills_transfer_flag(xfer_db):
    from ledger_agent.core.database import init_db

    _insert(xfer_db, "t-xfer", "Synthetic sweep", code="9000", is_transfer=False)
    _insert(xfer_db, "t-opex", "Synthetic software", code="5010", is_transfer=False)

    con = sqlite3.connect(str(xfer_db))
    con.execute("UPDATE schema_meta SET value='7' WHERE key='version'")
    con.commit()
    con.close()
    assert _flag(xfer_db, "t-xfer") == 0
    assert _version(xfer_db) == "7"

    init_db(xfer_db)

    assert _flag(xfer_db, "t-xfer") == 1
    assert _flag(xfer_db, "t-opex") == 0  # only 9000 rows are backfilled
    assert _version(xfer_db) == "8"


def test_update_coa_with_meta_9000_sets_flag(xfer_db):
    from ledger_agent.core.database import TransactionRepo

    _insert(xfer_db, "t1", "Synthetic sweep")
    TransactionRepo.update_coa_with_meta("t1", "9000", "Inter-Account Transfer", "1.0", 0.9)

    assert _flag(xfer_db, "t1") == 1
    assert _get("t1").is_transfer is True


def test_update_coa_with_meta_explicit_flag_persists(xfer_db):
    from ledger_agent.core.database import TransactionRepo

    _insert(xfer_db, "t2", "Synthetic card payment")
    TransactionRepo.update_coa_with_meta(
        "t2", "5020", "Bank & Transaction Fees", "1.0", 0.9, is_transfer=True
    )
    assert _flag(xfer_db, "t2") == 1


def test_update_coa_with_meta_none_never_clears_flag(xfer_db):
    from ledger_agent.core.database import TransactionRepo

    _insert(xfer_db, "t3", "Synthetic sweep", is_transfer=True)
    TransactionRepo.update_coa_with_meta("t3", "5010", "Software & Subscriptions", "1.0", 0.9)
    assert _flag(xfer_db, "t3") == 1
    TransactionRepo.update_coa_with_meta(
        "t3", "5010", "Software & Subscriptions", "1.0", 0.9, is_transfer=None
    )
    assert _flag(xfer_db, "t3") == 1


def test_update_coa_with_meta_none_leaves_non_transfer_unflagged(xfer_db):
    from ledger_agent.core.database import TransactionRepo

    _insert(xfer_db, "t4", "Synthetic software")
    TransactionRepo.update_coa_with_meta("t4", "5010", "Software & Subscriptions", "1.0", 0.9)
    assert _flag(xfer_db, "t4") == 0


def test_classify_batch_persists_detected_transfer(xfer_db):
    from ledger_agent.core.intelligence.classifier import classify_batch

    _insert(xfer_db, "t5", "TRANSFER * SYNTHETIC SWEEP")
    classify_batch([_get("t5")])

    stored = _get("t5")
    assert stored.coa_code == "9000"
    assert stored.is_transfer is True


def test_classify_batch_persists_transfer_flag_for_non_9000_code(xfer_db):
    """A transfer the classifier flags under another code (here via the user prompt)
    must still reach the DB — the 9000 rule alone would not cover it."""
    from ledger_agent.core.intelligence.classifier import classify_batch

    _insert(xfer_db, "t6", "ZQXJ SYNTHETIC UNMATCHED 0G00")

    def prompt_fn(txn, coa_entries):
        return ("3010", "Members Capital Contributions", True)

    classify_batch([_get("t6")], prompt_fn=prompt_fn)

    stored = _get("t6")
    assert stored.coa_code == "3010"
    assert stored.is_transfer is True
