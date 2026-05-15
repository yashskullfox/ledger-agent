"""
tests/integration/test_classification_persisted.py  —  ARCH-27
===============================================================

Verifies R-65 / R-66: once a transaction is classified, the resulting
``coa_code`` (plus ``classifier_version`` and ``confidence``) is written
back to ``transactions`` in the same ``run_cycle``.  Report-time
classification of rows that still hold an empty ``coa_code`` is
prohibited.

Test strategy
-------------
All tests use an isolated SQLite database (via FI_DB_PATH monkeypatch)
so they never touch the production DB.  The ``db`` fixture seeds the
minimum required entity / account / transaction rows.

Acceptance
----------
    pytest tests/integration/test_classification_persisted.py -q
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest


# ── DB fixture ────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_cls.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    from ledger_agent.core.database import init_db
    init_db(db_file)
    return db_file


def _entity(db):
    from ledger_agent.core.database import EntityRepo
    from ledger_agent.core.models import Entity
    e = Entity(name="ENTITY_A", entity_type="LLC", state="FL",
               id=str(uuid.uuid4()))
    EntityRepo.upsert(e, db)
    return e


def _account(db, entity_id):
    from ledger_agent.core.database import AccountRepo
    from ledger_agent.core.models import Account, AccountType
    a = Account(
        entity_id=entity_id,
        name="Business Checking",
        institution="Bank X",
        account_type=AccountType.CHECKING,
        account_number_masked="0001",
        id=str(uuid.uuid4()),
    )
    AccountRepo.upsert(a, db)
    return a


def _txn(db, account_id, description, amount, coa_code=""):
    from ledger_agent.core.database import TransactionRepo
    from ledger_agent.core.models import Transaction, TransactionType
    t = Transaction(
        account_id=account_id,
        date=date(2025, 1, 15),
        description=description,
        amount=Decimal(amount),
        transaction_type=TransactionType.DEBIT,
        statement_period="2025-01",
        coa_code=coa_code,
        id=str(uuid.uuid4()),
    )
    TransactionRepo.bulk_insert([t], db)
    return t


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestClassificationPersisted:
    """classify_batch must write coa_code back to transactions."""

    def test_unclassified_txn_gets_code_written(self, db):
        entity = _entity(db)
        acct = _account(db, entity.id)
        txn = _txn(db, acct.id, "TRAN FEE SERVICE CHARGE", "-5.00")
        assert txn.coa_code == ""

        from ledger_agent.core.intelligence.classifier import classify_batch
        classify_batch([txn])

        from ledger_agent.core.database import TransactionRepo
        persisted = TransactionRepo.get_by_id(txn.id, db)
        assert persisted is not None
        assert persisted.coa_code != "", (
            "coa_code must be written back to transactions after classification"
        )

    def test_already_classified_code_not_overwritten_with_unclassified(self, db):
        entity = _entity(db)
        acct = _account(db, entity.id)
        # Pre-classified by the parser
        txn = _txn(db, acct.id, "Margin Interest Expense", "-191.17", coa_code="5030")

        from ledger_agent.core.intelligence.classifier import classify_batch
        classify_batch([txn])

        from ledger_agent.core.database import TransactionRepo
        persisted = TransactionRepo.get_by_id(txn.id, db)
        # Pre-classified code must not be overwritten by unclassified sentinel
        assert persisted.coa_code == "5030", (
            "Pre-classified coa_code must be preserved, not overwritten"
        )

    def test_no_empty_coa_after_batch(self, db):
        """R-65: after classify_batch, no transaction has empty coa_code."""
        entity = _entity(db)
        acct = _account(db, entity.id)
        txns = [
            _txn(db, acct.id, "GOOGLE WORKSPACE SUBSCRIPTION", "-12.00"),
            _txn(db, acct.id, "BANK X SERVICE FEE", "-3.50"),
            _txn(db, acct.id, "INTUIT PAYROLL TAX", "-150.00"),
        ]

        from ledger_agent.core.intelligence.classifier import classify_batch
        classify_batch(txns)

        from ledger_agent.core.database import get_conn
        with get_conn(db) as conn:
            empty_count = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE coa_code='' OR coa_code IS NULL"
            ).fetchone()[0]
        assert empty_count == 0, (
            f"Expected 0 unclassified transactions after classify_batch, got {empty_count}"
        )

    def test_classifier_version_written(self, db):
        """R-66: classifier_version must be persisted alongside coa_code."""
        entity = _entity(db)
        acct = _account(db, entity.id)
        txn = _txn(db, acct.id, "TRAN FEE BANK SERVICE", "-3.00")

        from ledger_agent.core.intelligence.classifier import classify_batch, CLASSIFIER_VERSION
        classify_batch([txn])

        from ledger_agent.core.database import get_conn
        with get_conn(db) as conn:
            row = conn.execute(
                "SELECT classifier_version FROM transactions WHERE id=?", (txn.id,)
            ).fetchone()
        assert row is not None
        assert row["classifier_version"] == CLASSIFIER_VERSION, (
            f"Expected classifier_version={CLASSIFIER_VERSION!r}, "
            f"got {row['classifier_version']!r}"
        )

    def test_reassignment_updates_code(self, db):
        """Reclassification must update coa_code and emit reassigned event."""
        entity = _entity(db)
        acct = _account(db, entity.id)
        # Seed with a wrong initial code
        txn = _txn(db, acct.id, "GOOGLE WORKSPACE", "-12.00", coa_code="9999")

        from ledger_agent.core.database import TransactionRepo
        # Simulate a reclassification
        TransactionRepo.update_coa_with_meta(
            txn.id, "5010", "Software & Subscriptions", "1.1", 0.85, db
        )

        persisted = TransactionRepo.get_by_id(txn.id, db)
        assert persisted.coa_code == "5010"


class TestClassifierVersionContract:
    """CLASSIFIER_VERSION is a non-empty string exported from classifier module."""

    def test_classifier_version_is_string(self):
        from ledger_agent.core.intelligence.classifier import CLASSIFIER_VERSION
        assert isinstance(CLASSIFIER_VERSION, str) and CLASSIFIER_VERSION

    def test_update_coa_with_meta_accepts_version_and_confidence(self, db):
        entity = _entity(db)
        acct = _account(db, entity.id)
        txn = _txn(db, acct.id, "SOME EXPENSE", "-10.00")

        from ledger_agent.core.database import TransactionRepo
        TransactionRepo.update_coa_with_meta(
            txn.id, "5080", "Other Operating Expenses", "1.0", 0.72, db
        )

        from ledger_agent.core.database import get_conn
        with get_conn(db) as conn:
            row = conn.execute(
                "SELECT coa_code, classifier_version, confidence FROM transactions WHERE id=?",
                (txn.id,)
            ).fetchone()
        assert row["coa_code"] == "5080"
        assert row["classifier_version"] == "1.0"
        assert float(row["confidence"]) == pytest.approx(0.72, abs=0.001)
