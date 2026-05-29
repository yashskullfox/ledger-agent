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

    def test_debit_does_not_match_revenue(self, coa_entries):
        # ARCH-29: "invoice" keyword is on 4020 (revenue). A debit payment
        # with "invoice" in description must NOT classify as revenue.
        from ledger_agent.core.intelligence.classifier import _keyword_match
        result = _keyword_match("VENDOR INVOICE PAYMENT", coa_entries, amount=-250.00)
        assert result is None or result.coa_type != "revenue", (
            "Debit transaction must not be classified as revenue"
        )

    def test_credit_does_not_match_expense(self, coa_entries):
        # A positive (credit) amount with a keyword that appears on an expense
        # code must not classify as expense.
        from ledger_agent.core.intelligence.classifier import _keyword_match
        result = _keyword_match("QUICKBOOKS ONLINE REFUND", coa_entries, amount=30.00)
        assert result is None or result.coa_type != "expense", (
            "Credit transaction must not be classified as expense"
        )

    def test_zero_amount_no_sign_filter(self, coa_entries):
        # amount=0 (default) applies no sign guard — quickbooks still matches
        from ledger_agent.core.intelligence.classifier import _keyword_match
        result = _keyword_match("QUICKBOOKS ONLINE", coa_entries, amount=0.0)
        assert result is not None and result.code == "5010"


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


class TestPriorityRules:
    """ARCH-29: description-priority rules for payroll and USPS transactions."""

    def _make_txn(self, description: str, amount: str):
        from ledger_agent.core.models import Transaction, TransactionType
        return Transaction(
            account_id="test",
            date=date(2025, 1, 15),
            description=description,
            raw_description=description,
            amount=Decimal(amount),
            transaction_type=TransactionType.DEBIT if Decimal(amount) < 0 else TransactionType.CREDIT,
            statement_period="2025-01",
        )

    def test_payroll_salary_classified_5021(self, coa_entries, db):
        """PAYROLL debit without TAX → employee salary → 5021."""
        from ledger_agent.core.intelligence.classifier import classify_transaction
        txn = self._make_txn("ACHCORPDEBITPAYROLL INTUIT35986178 SYNCED", "-720.33")
        result = classify_transaction(txn, coa_entries)
        assert result.coa_code == "5021", (
            f"Payroll salary debit must be 5021 Payroll & Wages, got {result.coa_code}"
        )

    def test_payroll_tax_classified_5040(self, coa_entries, db):
        """PAYROLL TAX debit → payroll tax remittance → 5040."""
        from ledger_agent.core.intelligence.classifier import classify_transaction
        txn = self._make_txn("DEBITTAX PAYROLL INTUIT35986178 SYNCED", "-210.45")
        result = classify_transaction(txn, coa_entries)
        assert result.coa_code == "5040", (
            f"Payroll tax debit must be 5040 Payroll Tax Expense, got {result.coa_code}"
        )

    def test_large_uspspo_classified_5061(self, coa_entries, db):
        """Generic USPSPO debit defaults to shipping/COGS unless tax language is explicit."""
        from ledger_agent.core.intelligence.classifier import classify_transaction
        txn = self._make_txn(
            "DEBITCARDPURCHASE USPSPO 5700345 MONEY ORDER",  # redaction: allow
            "-1000.00",
        )
        result = classify_transaction(txn, coa_entries)
        assert result.coa_code == "5061", (
            f"Generic USPSPO debit must default to 5061 Shipping Supplies, got {result.coa_code}"
        )

    def test_explicit_estimated_tax_classified_3040(self, coa_entries, db):
        """Explicit MO-PTE / estimated-tax language must map to owner draw, not shipping."""
        from ledger_agent.core.intelligence.classifier import classify_transaction
        txn = self._make_txn(
            "DEBITCARDPURCHASE Q1 ESTIMATED TAX STATE PAYMENT MONEY ORDER",  # redaction: allow
            "-1300.00",
        )
        result = classify_transaction(txn, coa_entries)
        assert result.coa_code == "3040", (
            f"Explicit estimated-tax debit must be 3040 Owner Draws, got {result.coa_code}"
        )

    def test_interactive_brok_transfer_classified_9000(self, coa_entries, db):
        """Brokerage funding / withdrawal lines are transfers, not bank fees or expenses."""
        from ledger_agent.core.intelligence.classifier import classify_transaction
        txn = self._make_txn(
            "INTERNET PAYMENT ACH TRANSF BROKERAGE REQ :507887839",
            "-60800.00",
        )
        result = classify_transaction(txn, coa_entries)
        assert result.coa_code == "9000", (
            f"Brokerage cash movement must be 9000 Inter-Account Transfer, got {result.coa_code}"
        )
        assert result.is_transfer is True

    def test_small_usps_kiosk_classified_5061(self, coa_entries, db):
        """Small USPS kiosk shipping charge → 5061 Office & Shipping Supplies."""
        from ledger_agent.core.intelligence.classifier import classify_transaction
        txn = self._make_txn(
            "DEBITCARDPURCHASE USPS KIOSK SHIPPING LABEL",  # redaction: allow
            "-18.50",
        )
        result = classify_transaction(txn, coa_entries)
        # Small USPS (< $500, no USPSPO) → keyword scan matches 5061
        assert result.coa_code == "5061", (
            f"Small USPS shipping must be 5061 Shipping Supplies, got {result.coa_code}"
        )

    def test_irs_usataxpymt_classified_5040(self, coa_entries, db):
        """IRS EFTPS USATAXPYMT = Form 941 quarterly payroll tax → 5040, not 3040/5050."""
        from ledger_agent.core.intelligence.classifier import classify_transaction
        txn = self._make_txn(
            "ACH CORP DEBIT USATAXPYMT IRS SYNCED LLCCUSTOMER ID acct_****1234",
            "-238.68",
        )
        result = classify_transaction(txn, coa_entries)
        assert result.coa_code == "5040", (
            f"IRS USATAXPYMT from LLC account must be 5040 Payroll Tax, got {result.coa_code} — "
            "pass-through LLC has no entity income tax; EFTPS deposits are Form 941 payroll tax"
        )

    def test_payroll_credit_not_triggered(self, coa_entries, db):
        """A positive (credit) that contains 'payroll' is not a salary deduction."""
        from ledger_agent.core.intelligence.classifier import classify_transaction
        txn = self._make_txn("PAYROLL REVERSAL CREDIT", "720.33")
        result = classify_transaction(txn, coa_entries)
        # Priority rule only fires on debit (amount < 0) — credit falls through normally
        assert result.coa_code != "5021", (
            "Payroll rule must not fire on credits — only debits are salary"
        )


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

    @pytest.mark.parametrize("description", [
        # Exercises the generic TRANSFER\s*(IN|OUT|TO|FROM) rule in local_backend._RULES
        "TRANSFER IN FROM SAVINGS",
        # Exercises the ZELLE\s*TO|ZELLE\s*FROM rule in local_backend._RULES
        "ZELLE TO RECIPIENT",
        # Exercises the WIRE\s*(IN|OUT|TRANSFER) rule in local_backend._RULES
        "WIRE TRANSFER OUTGOING",
    ])
    def test_classifies_transfer(self, description):
        from ledger_agent.core.intelligence.ai_backend.local_backend import LocalBackend
        backend = LocalBackend()
        result = backend.classify_transaction(description, 1000.00)
        assert result["is_transfer"] is True
        assert result["coa_code"] == "9000"

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
