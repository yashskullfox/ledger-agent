from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum


class AccountType(str, Enum):
    CHECKING = "checking"
    SAVINGS = "savings"
    BROKERAGE = "brokerage"
    MARGIN = "margin"
    CREDIT_CARD = "credit_card"
    OTHER = "other"


class TransactionType(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"
    BUY = "buy"
    SELL = "sell"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    FEE = "fee"
    TAX = "tax"
    MARGIN_INTEREST = "margin_interest"
    DIVIDEND = "dividend"
    OTHER = "other"
    PRIOR_PERIOD_ADJUSTMENT = "prior_period_adjustment"


class StatementType(str, Enum):
    BANK_CHECKING = "bank_checking"
    BANK_SAVINGS = "bank_savings"
    BROKERAGE = "brokerage"
    CREDIT_CARD = "credit_card"


class COAType(str, Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class PositionType(str, Enum):
    EQUITY = "equity"
    OPTION = "option"
    CASH = "cash"
    FIXED_INCOME = "fixed_income"


@dataclass
class Entity:
    name: str
    entity_type: str
    state: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ein_masked: str | None = None
    notes: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Account:
    entity_id: str
    name: str
    institution: str
    account_type: AccountType
    account_number_masked: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    currency: str = "USD"
    is_active: bool = True
    notes: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        return f"{self.institution} – {self.name} (…{self.account_number_masked})"


@dataclass
class Transaction:
    account_id: str
    date: date
    description: str
    amount: Decimal
    transaction_type: TransactionType
    statement_period: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    raw_description: str = ""
    coa_code: str = ""
    coa_name: str = ""
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    is_reconciled: bool = False
    is_transfer: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_debit(self) -> bool:
        return self.amount < 0

    @property
    def is_credit(self) -> bool:
        return self.amount > 0

    def __str__(self) -> str:
        sign = "+" if self.amount >= 0 else ""
        return f"{self.date}  {sign}{self.amount:>12,.2f}  {self.description}"


@dataclass
class Position:
    account_id: str
    symbol: str
    name: str
    quantity: Decimal
    price_per_unit: Decimal
    market_value: Decimal
    statement_period: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cost_basis: Decimal | None = None
    unrealized_gain_loss: Decimal | None = None
    is_margin: bool = False
    as_of_date: date | None = None
    position_type: PositionType = PositionType.EQUITY
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def unrealized_pct(self) -> Decimal | None:
        if self.cost_basis and self.cost_basis != 0:
            return ((self.market_value - self.cost_basis) / abs(self.cost_basis)) * 100
        return None


@dataclass
class AccountSnapshot:
    account_id: str
    statement_period: str
    ending_balance: Decimal
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    gross_asset_value: Decimal | None = None
    margin_balance: Decimal | None = None
    realised_gain_loss: Decimal | None = None
    beginning_balance: Decimal | None = None
    total_deposits: Decimal | None = None
    total_withdrawals: Decimal | None = None
    total_debits: Decimal | None = None
    total_credits: Decimal | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ParsedStatement:
    parser_id: str
    statement_type: StatementType
    institution: str
    account_number_masked: str
    statement_period: str
    entity_name: str
    transactions: list[Transaction] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    snapshot: AccountSnapshot | None = None
    raw_text: str = ""
    source_file: str = ""


@dataclass
class COAEntry:
    code: str
    name: str
    coa_type: COAType
    parent_code: str | None = None
    description: str = ""
    keywords: list[str] = field(default_factory=list)


@dataclass
class BalanceSheetLine:
    coa_code: str
    label: str
    amount: Decimal
    coa_type: COAType
    is_subtotal: bool = False
    indent: int = 0


@dataclass
class RealisedTrade:
    account_id: str
    statement_period: str
    symbol: str
    description: str
    gain_loss: Decimal
    term: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    settlement_date: date | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
