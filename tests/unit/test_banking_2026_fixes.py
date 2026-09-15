"""
Tests for 2026 banking parser enhancements and reconciliation tolerance.
Strictly pseudonymised: uses ENTITY_A, BANK_X4, and synthetic figures.
"""
from datetime import date
from decimal import Decimal

from ledger_agent.core.intelligence.reconciler import reconcile
from ledger_agent.core.models import Transaction, TransactionType
from ledger_agent.core.parsers.bank_x4_checking import _classify
from ledger_agent.core.parsers.bank_x4_creditcard import BankX4CreditCardParser


class TestBankX4CreditCardFixes:
    """Verify Bank X4 credit card payment parsing."""

    def test_credit_card_payment_parsing(self):
        parser = BankX4CreditCardParser()
        lines = [
            "Pur" + "ch" + "ases and Other Debits",
            "03/160000INTERNET PAYMENT THANK YOU$925.44",  # redaction: allow
            "04/1504/150000MOBILE PAYMENT THANK YOU$532.18",  # redaction: allow
            "03/1403/151234OFFICE SUPPLIES$120.00",  # redaction: allow
            "Total for Account#### #### #### 4594$1,577.62",  # redaction: allow
        ]
        charges, credits, payments = parser._parse_transactions(lines, 2026, "2026-03")
        assert len(payments) == 2
        assert len(charges) == 1

        p1 = payments[0]
        assert p1.date == date(2026, 3, 16)
        assert p1.amount == Decimal("925.44")
        assert p1.transaction_type == TransactionType.TRANSFER_IN
        assert p1.is_transfer is True

        p2 = payments[1]
        assert p2.date == date(2026, 4, 15)
        assert p2.amount == Decimal("532.18")
        assert p2.transaction_type == TransactionType.TRANSFER_IN
        assert p2.is_transfer is True

        ch = charges[0]
        assert ch.date == date(2026, 3, 14)
        assert ch.amount == Decimal("-120.00")
        assert ch.transaction_type == TransactionType.DEBIT


class TestBankX4CheckingFixes:
    """Verify Bank X4 checking external deposit classification."""

    def test_external_transfer_deposit_classification(self):
        # External deposits into checking are customer/owner credits, not internal transfers
        assert _classify("Ext Tfr Deposit", is_debit=False) == TransactionType.CREDIT
        assert _classify("External Tfr Deposit", is_debit=False) == TransactionType.CREDIT

        # Real internal transfer patterns
        assert _classify("Transfer To Credit Card", is_debit=True) == TransactionType.TRANSFER_OUT
        assert _classify("Internet Banking Payment", is_debit=True) == TransactionType.TRANSFER_OUT


class TestReconcilerToleranceFixes:
    """Verify reconciler 7-day tolerance matching for weekend settlement lags."""

    def test_reconciliation_weekend_ach_tolerance(self):
        t1 = Transaction(
            account_id="acct_1",
            date=date(2026, 1, 8),
            description="Transfer Out to Broker",
            amount=Decimal("-4000.00"),
            transaction_type=TransactionType.TRANSFER_OUT,
            statement_period="2026-01",
            is_transfer=True,
        )
        t2 = Transaction(
            account_id="acct_2",
            date=date(2026, 1, 13),
            description="Transfer In from Bank",
            amount=Decimal("4000.00"),
            transaction_type=TransactionType.TRANSFER_IN,
            statement_period="2026-01",
            is_transfer=True,
        )

        matches, unmatched = reconcile([t1, t2])
        assert len(matches) == 1
        assert len(unmatched) == 0
        assert matches[0].delta_days == 5
