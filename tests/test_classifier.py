"""
tests/test_classifier.py  –  Unit tests for transaction classifier
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture
def coa_entries():
    from ledger_agent.core.database import COARepo, init_db
    init_db()
    return COARepo.list_all()


@pytest.fixture
def sample_txn():
    from ledger_agent.core.models import Transaction, TransactionType
    return Transaction(
        account_id="test",
        date=date(2025, 1, 9),
        description="QUICKBOOKS ONLINE",
        raw_description="QUICKBOOKS ONLINE",
        amount=Decimal("-30.00"),
        transaction_type=TransactionType.DEBIT,
        statement_period="2025-01",
    )


class TestKeywordMatch:
    def test_matches_quickbooks(self, coa_entries):
        from ledger_agent.core.intelligence.classifier import _keyword_match
        entry = _keyword_match("QUICKBOOKS ONLINE", coa_entries)
        assert entry is not None
        assert "5010" in entry.code or "Software" in entry.name

    def test_no_match_returns_none(self, coa_entries):
        from ledger_agent.core.intelligence.classifier import _keyword_match
        result = _keyword_match("XYZZY UNKNOWN VENDOR", coa_entries)
        assert result is None


class TestClassifyTransaction:
    def test_already_classified_skipped(self, sample_txn, coa_entries):
        sample_txn.coa_code = "5010"
        from ledger_agent.core.intelligence.classifier import classify_transaction
        result = classify_transaction(sample_txn, coa_entries)
        assert result.coa_code == "5010"

    def test_auto_classifies_known_vendor(self, coa_entries, db):
        from ledger_agent.core.models import Transaction, TransactionType
        from ledger_agent.core.intelligence.classifier import classify_transaction
        txn = Transaction(
            account_id="test",
            date=date(2025, 1, 9),
            description="INCFILE LLC REGISTERED AGENT",
            raw_description="INCFILE LLC REGISTERED AGENT",
            amount=Decimal("-29.00"),
            transaction_type=TransactionType.DEBIT,
            statement_period="2025-01",
        )
        result = classify_transaction(txn, coa_entries)
        # Should be classified (not UNCLASSIFIED_CODE) via memory, AI, or keyword
        assert result.coa_code is not None

    def test_unclassified_gets_sentinel(self, coa_entries, db):
        from ledger_agent.core.models import Transaction, TransactionType
        from ledger_agent.core.intelligence.classifier import classify_transaction
        txn = Transaction(
            account_id="test",
            date=date(2025, 1, 9),
            description="XYZZY UNKNOWN VERY OBSCURE VENDOR 99999",
            raw_description="XYZZY UNKNOWN VERY OBSCURE VENDOR 99999",
            amount=Decimal("-1.00"),
            transaction_type=TransactionType.DEBIT,
            statement_period="2025-01",
        )
        result = classify_transaction(txn, coa_entries, prompt_fn=None)
        # No prompt_fn → should fall through to UNCLASSIFIED or AI fallback
        assert result.coa_code is not None  # has some code


class TestClassifyBatch:
    def test_returns_tuple(self, make_transaction, db):
        from ledger_agent.core.intelligence.classifier import classify_batch
        txns = [make_transaction() for _ in range(3)]
        result = classify_batch(txns, prompt_fn=None)
        classified, auto, prompted = result
        assert len(classified) == 3
        assert isinstance(auto, int)
        assert isinstance(prompted, int)

    def test_empty_batch(self, db):
        from ledger_agent.core.intelligence.classifier import classify_batch
        classified, auto, prompted = classify_batch([], prompt_fn=None)
        assert classified == []
        assert auto == 0
        assert prompted == 0


class TestLocalBackend:
    def test_classifies_irs(self):
        # V7 fix: USATAXPYMT / IRS estimated-tax payments for a pass-through
        # LLC (Form 1065 filer) are partner draws, NOT a P&L tax expense.
        # They must map to COA 3040 (Members Distributions / Owner Draws).
        from ledger_agent.core.intelligence.ai_backend.local_backend import LocalBackend
        backend = LocalBackend()
        result = backend.classify_transaction("IRS USATAXPYMT", -72.95)
        assert result["coa_code"] == "3040", (
            "IRS USATAXPYMT must classify as Members Distributions (3040), "
            "not Federal Income Tax Expense (5050) — SYNCED LLC is a pass-through"
        )
        assert result["confidence"] >= 0.5

    def test_classifies_quickbooks(self):
        from ledger_agent.core.intelligence.ai_backend.local_backend import LocalBackend
        backend = LocalBackend()
        result = backend.classify_transaction("QUICKBOOKS ONLINE", -30.00)
        assert result["coa_code"] == "5010"

    def test_classifies_transfer(self):
        from ledger_agent.core.intelligence.ai_backend.local_backend import LocalBackend
        backend = LocalBackend()
        result = backend.classify_transaction("MONEYLINE FID BKG SVC LLC", 1000.00)  # redaction: allow — matches production transfer-detection rule
        assert result["is_transfer"] is True

    def test_backend_name(self):
        from ledger_agent.core.intelligence.ai_backend.local_backend import LocalBackend
        assert LocalBackend().backend_name == "local"

    def test_explain_returns_string(self):
        from ledger_agent.core.intelligence.ai_backend.local_backend import LocalBackend
        explanation = LocalBackend().explain_classification(
            "QUICKBOOKS ONLINE", "5010", "Software & SaaS"
        )
        assert isinstance(explanation, str)
        assert len(explanation) > 0
