"""
ledger_agent/core/exceptions.py  –  Custom exception hierarchy

ARCH-20: Canonical location. The legacy path ``core.exceptions`` is a
backward-compatibility shim that re-exports everything from here.
"""


class FinancialIntelligenceError(Exception):
    """Base exception for the entire project."""


class ParserNotFoundError(FinancialIntelligenceError):
    """Raised when no registered parser can handle a given PDF."""


class ParserError(FinancialIntelligenceError):
    """Raised on unrecoverable errors inside a parser."""


class DatabaseError(FinancialIntelligenceError):
    """Raised when a DB operation fails unexpectedly."""


class DuplicateStatementError(FinancialIntelligenceError):
    """Raised when the same statement (same account + period) is imported again."""


class EntityNotFoundError(FinancialIntelligenceError):
    """Raised when an entity lookup returns no results."""


class AccountNotFoundError(FinancialIntelligenceError):
    """Raised when an account lookup returns no results."""


class ClassificationError(FinancialIntelligenceError):
    """Raised when a transaction cannot be classified even after prompting."""


class ReconciliationError(FinancialIntelligenceError):
    """Raised when cross-account reconciliation detects a discrepancy."""


class ParserGap(FinancialIntelligenceError):
    """ARCH-24: Raised when a required field cannot be extracted from a statement.

    Attributes:
        field_name: The logical field that is absent (e.g. 'gross_asset_value').
        parser_id: The parser that detected the gap (e.g. 'ibkr').
        hint: Optional human-readable hint about where the value should appear.
    """

    def __init__(self, field_name: str, parser_id: str, hint: str = "") -> None:
        self.field_name = field_name
        self.parser_id = parser_id
        self.hint = hint
        msg = f"[{parser_id}] Required field '{field_name}' absent from statement"
        if hint:
            msg += f" ({hint})"
        super().__init__(msg)
