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


class ParserGap(FinancialIntelligenceError):
    """
    Raised when a brokerage parser cannot populate a required field that the
    source statement is expected to report (R-60 / ARCH-24).

    Attributes
    ----------
    institution : str
        Parser institution name (e.g. "Fidelity Investments").
    statement_period : str
        The affected statement period (e.g. "2024-12").
    missing_fields : list[str]
        Names of fields that could not be extracted but are required by the
        statement type (e.g. ``["gross_asset_value", "margin_balance"]``).
    """

    def __init__(
        self,
        institution: str,
        statement_period: str,
        missing_fields: list,
        *,
        source_file: str = "",
    ) -> None:
        self.institution = institution
        self.statement_period = statement_period
        self.missing_fields = list(missing_fields)
        self.source_file = source_file
        super().__init__(
            f"ParserGap [{institution} {statement_period}]: "
            f"required field(s) missing: {', '.join(self.missing_fields)}"
            + (f" — {source_file}" if source_file else "")
        )


class AggregationGap(FinancialIntelligenceError):
    """
    Raised when the balance-sheet aggregator detects that an expected
    account_snapshots row is absent (R-63 / ARCH-26).

    Attributes
    ----------
    period : str
        The fiscal period being aggregated (e.g. "2024-12").
    account_id : str
        The account whose snapshot is missing.
    reason : str
        Human-readable explanation of why the snapshot was expected.
    """

    def __init__(
        self,
        period: str,
        account_id: str,
        reason: str = "",
    ) -> None:
        self.period = period
        self.account_id = account_id
        self.reason = reason
        super().__init__(
            f"AggregationGap [{period}] account_id={account_id!r}"
            + (f": {reason}" if reason else "")
        )
