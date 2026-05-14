"""
tests/test_models.py  –  Unit tests for core data models
"""
from __future__ import annotations

from decimal import Decimal

from ledger_agent.core.models import (
    BalanceSheetLine,
    COAEntry, COAType, Entity, )


class TestEntity:
    def test_creates_with_uuid(self):
        e = Entity(name="TEST LLC", entity_type="LLC", state="MO")
        assert e.id
        assert len(e.id) == 36  # UUID4 string

    def test_defaults(self):
        e = Entity(name="TEST LLC", entity_type="LLC", state="MO")
        assert e.ein_masked is None
        assert e.notes == ""


class TestAccount:
    def test_creates_with_uuid(self, make_account):
        a = make_account()
        assert a.id
        assert a.institution == "Truist Bank"

    def test_str_representation(self, make_account):
        a = make_account(name="My Acct")
        s = str(a)
        assert "My Acct" in s or "Truist Bank" in s


class TestTransaction:
    def test_amount_is_decimal(self, make_transaction):
        t = make_transaction(amount="-30.00")
        assert isinstance(t.amount, Decimal)
        assert t.amount == Decimal("-30.00")

    def test_is_transfer_default_false(self, make_transaction):
        t = make_transaction()
        assert t.is_transfer is False

    def test_uuid_generated(self, make_transaction):
        t = make_transaction()
        assert t.id
        assert len(t.id) == 36


class TestPosition:
    def test_market_value_decimal(self, make_position):
        p = make_position(symbol="SNAP", quantity="3000", price="11.29")
        assert isinstance(p.market_value, Decimal)
        assert p.market_value == Decimal("33870.00")

    def test_symbol_set(self, make_position):
        p = make_position(symbol="TSLA", quantity="10", price="200.00")
        assert p.symbol == "TSLA"


class TestBalanceSheetLine:
    def test_creation(self):
        line = BalanceSheetLine(
            "1010", "Business Checking", Decimal("5000.00"),
            COAType.ASSET, indent=2,
        )
        assert line.coa_code == "1010"
        assert line.amount == Decimal("5000.00")
        assert line.is_subtotal is False

    def test_subtotal_flag(self):
        line = BalanceSheetLine(
            "TOTAL", "Total Assets", Decimal("10000.00"),
            COAType.ASSET, is_subtotal=True,
        )
        assert line.is_subtotal is True


class TestCOAEntry:
    def test_keywords_default_empty(self):
        coa = COAEntry(code="5010", name="Software", coa_type=COAType.EXPENSE)
        assert coa.keywords == []

    def test_parent_code_optional(self):
        coa = COAEntry(code="5010", name="Software", coa_type=COAType.EXPENSE,
                       parent_code="5000")
        assert coa.parent_code == "5010" or coa.parent_code == "5000"
