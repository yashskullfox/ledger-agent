"""
core/exceptions.py  –  Custom exception hierarchy
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
