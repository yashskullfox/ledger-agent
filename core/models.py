"""
core/models.py  –  Pure-Python dataclass models (no DB coupling)
─────────────────────────────────────────────────────────────────
All monetary values are stored as Python Decimal for exactness.
All IDs are UUID4 strings generated at construction time.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional


# ── Enumerations ──────────────────────────────────────────────────────────────

class AccountType(str, Enum):
    CHECKING      = "checking"
    SAVINGS       = "savings"
    BROKERAGE     = "brokerage"
    MARGIN        = "margin"
    CREDIT_CARD   = "credit_card"
    OTHER         = "other"


class TransactionType(str, Enum):
    DEBIT          = "debit"       # money out
    CREDIT         = "credit"      # money in
    BUY            = "buy"         # securities purchase
    SELL           = "sell"        # securities sale
    TRANSFER_IN    = "transfer_in"
    TRANSFER_OUT   = "transfer_out"
    FEE            = "fee"
    TAX            = "tax"
    MARGIN_INTEREST = "margin_interest"
    DIVIDEND       = "dividend"
    OTHER          = "other"


class StatementType(str, Enum):
    BANK_CHECKING   = "bank_checking"
    BANK_SAVINGS    = "bank_savings"
    BROKERAGE       = "brokerage"
    CREDIT_CARD     = "credit_card"


class COAType(str, Enum):
    ASSET     = "asset"
    LIABILITY = "liability"
    EQUITY    = "equity"
    REVENUE   = "revenue"
    EXPENSE   = "expense"


# ── Entity ────────────────────────────────────────────────────────────────────

@dataclass
class Entity:
    name: str
    entity_type: str                    # LLC, Corp, Sole Prop …
    state: str                          # formation state
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ein_masked: Optional[str] = None    # e.g. "**-***1234"
    notes: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)


# ── Financial Account ─────────────────────────────────────────────────────────

@dataclass
class Account:
    entity_id: str
    name: str
    institution: str
    account_type: AccountType
    account_number_masked: str          # last 4 digits only
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    currency: str = "USD"
    is_active: bool = True
    notes: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)

    def __str__(self) -> str:
        return f"{self.institution} – {self.name} (…{self.account_number_masked})"


# ── Transaction ───────────────────────────────────────────────────────────────

@dataclass
class Transaction:
    account_id: str
    date: date
    description: str                    # cleaned / normalised
    amount: Decimal                     # positive = credit/in, negative = debit/out
    transaction_type: TransactionType
    statement_period: str               # "YYYY-MM"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    raw_description: str = ""           # verbatim from PDF
    coa_code: str = ""                  # Chart-of-Accounts code, e.g. "5010"
    coa_name: str = ""                  # Human label, e.g. "Software Subscriptions"
    tags: List[str] = field(default_factory=list)
    notes: str = ""
    is_reconciled: bool = False
    is_transfer: bool = False           # True → skip in P&L (inter-account move)
    created_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def is_debit(self) -> bool:
        return self.amount < 0

    @property
    def is_credit(self) -> bool:
        return self.amount > 0

    def __str__(self) -> str:
        sign = "+" if self.amount >= 0 else ""
        return f"{self.date}  {sign}{self.amount:>12,.2f}  {self.description}"


# ── Security Position (for brokerage accounts) ────────────────────────────────

@dataclass
class Position:
    account_id: str
    symbol: str
    name: str
    quantity: Decimal
    price_per_unit: Decimal
    market_value: Decimal
    statement_period: str               # "YYYY-MM"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cost_basis: Optional[Decimal] = None
    unrealized_gain_loss: Optional[Decimal] = None
    is_margin: bool = False
    as_of_date: Optional[date] = None
    created_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def unrealized_pct(self) -> Optional[Decimal]:
        if self.cost_basis and self.cost_basis != 0:
            return ((self.market_value - self.cost_basis) / abs(self.cost_basis)) * 100
        return None


# ── Account Snapshot (end-of-period balance) ─────────────────────────────────

@dataclass
class AccountSnapshot:
    account_id: str
    statement_period: str               # "YYYY-MM"
    ending_balance: Decimal             # cash balance for bank; net value for brokerage
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    gross_asset_value: Optional[Decimal] = None   # brokerage: market value of holdings
    margin_balance: Optional[Decimal] = None      # brokerage: margin debt (negative)
    realised_gain_loss: Optional[Decimal] = None  # brokerage: period realised G/L
    beginning_balance: Optional[Decimal] = None
    total_deposits: Optional[Decimal] = None
    total_withdrawals: Optional[Decimal] = None
    total_debits: Optional[Decimal] = None
    total_credits: Optional[Decimal] = None
    created_at: datetime = field(default_factory=datetime.utcnow)


# ── Parsed Statement (container returned by every parser) ─────────────────────

@dataclass
class ParsedStatement:
    parser_id: str                      # e.g. "truist_checking"
    statement_type: StatementType
    institution: str
    account_number_masked: str
    statement_period: str               # "YYYY-MM"
    entity_name: str
    transactions: List[Transaction] = field(default_factory=list)
    positions: List[Position] = field(default_factory=list)
    snapshot: Optional[AccountSnapshot] = None
    raw_text: str = ""                  # full PDF text (debugging)
    source_file: str = ""


# ── Chart of Accounts entry ───────────────────────────────────────────────────

@dataclass
class COAEntry:
    code: str                           # e.g. "5010"
    name: str                           # e.g. "Software & Subscriptions"
    coa_type: COAType
    parent_code: Optional[str] = None   # e.g. "5000" (Expenses parent)
    description: str = ""
    keywords: List[str] = field(default_factory=list)   # for auto-classification


# ── Balance Sheet Line ────────────────────────────────────────────────────────

@dataclass
class BalanceSheetLine:
    coa_code: str
    label: str
    amount: Decimal
    coa_type: COAType
    is_subtotal: bool = False
    indent: int = 0                     # visual indentation level


# ── Realized Trade ────────────────────────────────────────────────────────────

@dataclass
class RealisedTrade:
    account_id: str
    statement_period: str
    symbol: str
    description: str
    gain_loss: Decimal
    term: str                           # "short" | "long"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    settlement_date: Optional[date] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
