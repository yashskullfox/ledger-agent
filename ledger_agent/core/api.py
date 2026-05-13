from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

@dataclass
class ImportReport:
    """Summary returned by import_statements()."""
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    failed_files: List[str] = field(default_factory=list)
    periods_added: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0


@dataclass
class Form1065:
    """Federal partnership return data (Form 1065 approximation)."""
    fiscal_year: int = 0
    entity_name: str = ""
    ein_masked: str = ""
    total_income: Decimal = Decimal("0")
    total_deductions: Decimal = Decimal("0")
    ordinary_business_income: Decimal = Decimal("0")
    # Schedule K items
    net_short_term_capital_gain: Decimal = Decimal("0")
    net_long_term_capital_gain: Decimal = Decimal("0")
    dividend_income: Decimal = Decimal("0")
    interest_income: Decimal = Decimal("0")
    # Partners
    partner_ids: List[str] = field(default_factory=list)


@dataclass
class ScheduleK1:
    """Partner's share of income (Schedule K-1 approximation)."""
    fiscal_year: int = 0
    partner_id: str = ""
    partner_name: str = ""
    ownership_pct: Decimal = Decimal("0")
    ordinary_income_loss: Decimal = Decimal("0")
    net_stcg: Decimal = Decimal("0")
    net_ltcg: Decimal = Decimal("0")
    dividend_income: Decimal = Decimal("0")
    interest_income: Decimal = Decimal("0")


@dataclass
class PTEEstimate:
    """Pass-Through Entity quarterly tax estimate."""
    fiscal_year: int = 0
    net_income: Decimal = Decimal("0")
    total_annual_tax: Decimal = Decimal("0")
    effective_rate: Decimal = Decimal("0")
    quarterly_payment: Decimal = Decimal("0")
    quarterly_payments: list = field(default_factory=list)
    notes: str = ""


@dataclass
class ReconcileReport:
    """Year-end reconciliation results."""
    fiscal_year: int = 0
    matched: int = 0
    unmatched: int = 0
    total_transfers: Decimal = Decimal("0")
    issues: List[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return len(self.issues) == 0


def _entity_and_periods(fiscal_year: int):
    from core.database import EntityRepo, get_conn, init_db
    init_db()
    entities = EntityRepo.list_all()
    if not entities:
        raise ValueError("No entities found in database — run import_statements() first.")
    entity = entities[0]
    prefix = str(fiscal_year) + "-%"
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT statement_period FROM transactions "
            "WHERE statement_period LIKE ? ORDER BY statement_period",
            (prefix,),
        ).fetchall()
    periods = [r[0] for r in rows if r[0]]
    return entity, periods


def _transactions_for_year(fiscal_year: int):
    from core.database import TransactionRepo, get_conn, init_db
    init_db()
    prefix = str(fiscal_year) + "-%"
    with get_conn() as conn:
        period_rows = conn.execute(
            "SELECT DISTINCT statement_period FROM transactions WHERE statement_period LIKE ?",
            (prefix,),
        ).fetchall()
    periods = [r[0] for r in period_rows if r[0]]
    txns: list = []
    for period in periods:
        txns.extend(TransactionRepo.list_for_period(period))
    return txns


def import_statements(
    folder: Path,
    *,
    allow_partial: bool = False,
) -> ImportReport:
    from parsers.registry import ParserRegistry
    from core.database import (
        init_db, AccountRepo, EntityRepo, SnapshotRepo,
        TransactionRepo, PositionRepo, ImportedStatementRepo,
    )
    import parsers  # noqa: F401 — trigger auto-discovery

    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f"Folder does not exist: {folder}")

    init_db()
    report = ImportReport()

    pdf_files = sorted(folder.rglob("*.pdf"))
    for pdf_path in pdf_files:
        try:
            # Check idempotency
            if ImportedStatementRepo.exists(str(pdf_path)):
                report.skipped += 1
                continue

            parser = ParserRegistry.detect(pdf_path)
            if parser is None:
                log.warning("No parser matched: %s", pdf_path.name)
                report.failed += 1
                report.failed_files.append(pdf_path.name)
                continue

            stmt = parser.parse(pdf_path)

            # Ensure entity exists
            entities = EntityRepo.list_all()
            if entities:
                entity = entities[0]
            else:
                from core.models import Entity
                _default_name = os.environ.get("FI_DEFAULT_ENTITY_NAME", "Entity")
                entity = Entity(name=_default_name, entity_type="LLC", state="MO")
                EntityRepo.upsert(entity)

            # Persist account
            account = AccountRepo.find(stmt.institution, stmt.account_number_masked or "****")
            if account is None:
                from core.models import Account, AccountType
                account = Account(
                    entity_id=entity.id,
                    name=stmt.institution,
                    institution=stmt.institution,
                    account_type=AccountType.CHECKING,
                    account_number_masked=stmt.account_number_masked or "****",
                )
                AccountRepo.upsert(account)

            # Persist snapshot
            if stmt.snapshot:
                snap = stmt.snapshot
                snap.account_id = account.id
                SnapshotRepo.upsert(snap)

            # Persist transactions
            if stmt.transactions:
                for t in stmt.transactions:
                    t.account_id = account.id
                TransactionRepo.bulk_insert(stmt.transactions)

            # Persist positions
            if stmt.positions:
                for p in stmt.positions:
                    p.account_id = account.id
                PositionRepo.upsert_period(stmt.positions)

            # Mark as imported (idempotency guard)
            ImportedStatementRepo.record(
                file_path=str(pdf_path),
                parser_id=stmt.parser_id,
                institution=stmt.institution,
                period=stmt.period or "",
                account_last4=stmt.account_number_masked or "",
            )

            if stmt.period and stmt.period not in report.periods_added:
                report.periods_added.append(stmt.period)
            report.imported += 1
            log.info("Imported %s (%s)", pdf_path.name, stmt.period)

        except Exception as exc:
            log.error("Failed to import %s: %s", pdf_path.name, exc)
            report.failed += 1
            report.failed_files.append(pdf_path.name)

    return report


def generate_balance_sheet(fiscal_year: int):
    from accounting.balance_sheet import BalanceSheetBuilder
    from core.database import init_db

    init_db()
    entity, periods = _entity_and_periods(fiscal_year)
    if not periods:
        raise ValueError(f"No statement data found for fiscal year {fiscal_year}.")

    last_period = periods[-1]
    return BalanceSheetBuilder(entity.id, last_period).build()


def generate_form_1065(fiscal_year: int) -> Form1065:
    from core.database import EntityRepo, init_db

    init_db()
    entity, _ = _entity_and_periods(fiscal_year)
    txns = _transactions_for_year(fiscal_year)

    income = Decimal("0")
    deductions = Decimal("0")
    net_stcg = Decimal("0")
    net_ltcg = Decimal("0")
    dividends = Decimal("0")
    interest = Decimal("0")

    # COA ranges: 4xxx = revenue/income, 5xxx = expenses
    # Special Schedule K codes: 4010/5070 (gains/losses), 4021 (div), 4031 (interest)
    STCG_GAIN  = {"4010"}
    STCG_LOSS  = {"5070"}
    DIV_CODES  = {"4021"}
    INT_CODES  = {"4031"}
    REV_CODES  = {str(c) for c in range(4000, 5000)}  # 4000–4999
    EXP_CODES  = {str(c) for c in range(5000, 6000)}  # 5000–5999

    for t in txns:
        if t.is_transfer or not t.coa_code:
            continue
        amt = Decimal(str(t.amount))
        code = t.coa_code
        if code in STCG_GAIN:
            net_stcg += amt
        elif code in STCG_LOSS:
            net_stcg += amt  # losses are negative
        elif code in DIV_CODES:
            dividends += amt
        elif code in INT_CODES:
            interest += amt
        elif code.startswith("4"):
            income += amt
        elif code.startswith("5"):
            deductions += abs(amt)

    ordinary = income - deductions
    f = Form1065(
        fiscal_year=fiscal_year,
        entity_name=entity.name,
        ein_masked=entity.ein_masked or "",
        total_income=income,
        total_deductions=deductions,
        ordinary_business_income=ordinary,
        net_short_term_capital_gain=net_stcg,
        net_long_term_capital_gain=Decimal("0"),  # expand when LTCG data available
        dividend_income=dividends,
        interest_income=interest,
        partner_ids=[entity.id],
    )
    log.info(
        "Form1065 %d: income=%s deductions=%s ordinary=%s stcg=%s",
        fiscal_year, income, deductions, ordinary, net_stcg,
    )
    return f


def generate_k1(fiscal_year: int, partner_id: str) -> ScheduleK1:
    f = generate_form_1065(fiscal_year)

    _yash_name  = os.environ.get("FI_PARTNER_YASH_NAME",  "Partner A")
    _parin_name = os.environ.get("FI_PARTNER_PARIN_NAME", "Partner B")
    PARTNERS = {
        "yash":  (_yash_name,  Decimal("0.99")),
        "parin": (_parin_name, Decimal("0.01")),
    }
    key = partner_id.lower().strip()
    name, pct = PARTNERS.get(key, (partner_id, Decimal("1.00")))

    return ScheduleK1(
        fiscal_year=fiscal_year,
        partner_id=partner_id,
        partner_name=name,
        ownership_pct=pct,
        ordinary_income_loss=(f.ordinary_business_income * pct).quantize(Decimal("0.01")),
        net_stcg=(f.net_short_term_capital_gain * pct).quantize(Decimal("0.01")),
        net_ltcg=(f.net_long_term_capital_gain * pct).quantize(Decimal("0.01")),
        dividend_income=(f.dividend_income * pct).quantize(Decimal("0.01")),
        interest_income=(f.interest_income * pct).quantize(Decimal("0.01")),
    )


def pte_estimate(fiscal_year: int) -> PTEEstimate:
    from accounting.tax_estimator import TaxEstimator
    from core.database import init_db

    init_db()
    entity, periods = _entity_and_periods(fiscal_year)
    if not periods:
        raise ValueError(f"No data for fiscal year {fiscal_year}.")

    f = generate_form_1065(fiscal_year)
    estimator = TaxEstimator(entity.name, fiscal_year)
    raw = estimator.estimate_from_net_income(f.ordinary_business_income)

    notes_str = "; ".join(raw.notes) if isinstance(raw.notes, list) else (raw.notes or "")
    return PTEEstimate(
        fiscal_year=fiscal_year,
        net_income=raw.net_income,
        total_annual_tax=raw.total_annual_tax,
        effective_rate=raw.effective_rate,
        quarterly_payment=(raw.total_annual_tax / 4).quantize(Decimal("0.01")),
        quarterly_payments=[
            {"quarter": p.quarter, "due_date": p.due_date, "amount": float(p.amount)}
            for p in raw.quarterly_payments
        ],
        notes=notes_str,
    )


def reconcile_year(fiscal_year: int) -> ReconcileReport:
    from intelligence.reconciler import reconcile
    from core.database import init_db

    init_db()
    entity, _ = _entity_and_periods(fiscal_year)
    txns = _transactions_for_year(fiscal_year)
    transfers = [t for t in txns if t.is_transfer]

    if not transfers:
        return ReconcileReport(fiscal_year=fiscal_year)

    matched_list, unmatched_txns = reconcile(transfers)
    total_xfer = sum(abs(Decimal(str(t.amount))) for t in transfers)
    issues = [
        f"Unmatched transfer {t.date} {t.description!r} ${t.amount}"
        for t in unmatched_txns
    ]

    return ReconcileReport(
        fiscal_year=fiscal_year,
        matched=len(matched_list),
        unmatched=len(unmatched_txns),
        total_transfers=total_xfer,
        issues=issues,
    )
