from __future__ import annotations


class FinancialIntelligenceError(Exception):
    pass


class ParserNotFoundError(FinancialIntelligenceError):
    pass


class ParserError(FinancialIntelligenceError):
    pass


class DatabaseError(FinancialIntelligenceError):
    pass


class DuplicateStatementError(FinancialIntelligenceError):
    pass


class EntityNotFoundError(FinancialIntelligenceError):
    pass


class AccountNotFoundError(FinancialIntelligenceError):
    pass


class ClassificationError(FinancialIntelligenceError):
    pass


class ReconciliationError(FinancialIntelligenceError):
    pass


class ParserGap(FinancialIntelligenceError):

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
