from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ── Module-level partner table (ARCH-19 / CRIT-03) ───────────────────────────
# Keys are the canonical partner_id strings used by CLI/MCP/bridge.
# Each value is (name, capital_pct, profit_loss_pct).
# Capital and P&L splits are independent in partnership accounting (K-1 Part II J).
# Default split for the reference ENTITY_A: partner_1 majority capital / full P&L,
# partner_2 minority capital / no P&L share.  Override via env vars without
# touching code.
def _build_partners() -> Dict[str, Tuple[str, Decimal, Decimal]]:
    return {
        "partner_1": (
            os.environ.get("FI_PARTNER_1_NAME", "Partner A"),
            Decimal(os.environ.get("FI_PARTNER_1_CAPITAL", "0.99")),
            Decimal(os.environ.get("FI_PARTNER_1_PL", "1.00")),
        ),
        "partner_2": (
            os.environ.get("FI_PARTNER_2_NAME", "Partner B"),
            Decimal(os.environ.get("FI_PARTNER_2_CAPITAL", "0.01")),
            Decimal(os.environ.get("FI_PARTNER_2_PL", "0.00")),
        ),
    }


PARTNERS: Dict[str, Tuple[str, Decimal, Decimal]] = _build_partners()


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class ImportReport:
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
    fiscal_year: int = 0
    entity_name: str = ""
    ein_masked: str = ""
    total_income: Decimal = Decimal("0")
    total_deductions: Decimal = Decimal("0")
    ordinary_business_income: Decimal = Decimal("0")
    net_short_term_capital_gain: Decimal = Decimal("0")
    net_long_term_capital_gain: Decimal = Decimal("0")
    dividend_income: Decimal = Decimal("0")
    interest_income: Decimal = Decimal("0")
    partner_ids: List[str] = field(default_factory=list)


@dataclass
class ScheduleK1:
    fiscal_year: int = 0
    partner_id: str = ""
    partner_name: str = ""
    capital_pct: Decimal = Decimal("0")
    profit_loss_pct: Decimal = Decimal("0")
    ordinary_income_loss: Decimal = Decimal("0")
    net_stcg: Decimal = Decimal("0")
    net_ltcg: Decimal = Decimal("0")
    dividend_income: Decimal = Decimal("0")
    interest_income: Decimal = Decimal("0")

    @property
    def ownership_pct(self) -> Decimal:
        """Deprecated alias for profit_loss_pct — kept for backwards compat."""
        return self.profit_loss_pct


@dataclass
class PTEEstimate:
    fiscal_year: int = 0
    net_income: Decimal = Decimal("0")
    total_annual_tax: Decimal = Decimal("0")
    effective_rate: Decimal = Decimal("0")
    quarterly_payment: Decimal = Decimal("0")
    quarterly_payments: list = field(default_factory=list)
    notes: str = ""


@dataclass
class ReconcileReport:
    fiscal_year: int = 0
    matched: int = 0
    unmatched: int = 0
    total_transfers: Decimal = Decimal("0")
    issues: List[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return len(self.issues) == 0


# ── Internal helpers ──────────────────────────────────────────────────────────

def _entity_and_periods(fiscal_year: int):
    from ledger_agent.core.database import EntityRepo, get_conn, init_db
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
    from ledger_agent.core.database import TransactionRepo, get_conn, init_db
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


# ── Public API ────────────────────────────────────────────────────────────────

def import_statements(
    folder: Path,
    *,
    allow_partial: bool = False,
) -> ImportReport:
    from ledger_agent.core.parsers.base import BaseStatementParser
    from ledger_agent.core.parsers.registry import ParserRegistry
    from ledger_agent.core.database import (
        init_db, AccountRepo, EntityRepo, SnapshotRepo,
        TransactionRepo, PositionRepo, ImportRegistry,
    )
    import ledger_agent.core.parsers  # noqa: F401 — trigger auto-discovery

    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f"Folder does not exist: {folder}")

    init_db()
    report = ImportReport()

    # Ensure default entity exists before processing any file
    entities = EntityRepo.list_all()
    if entities:
        entity = entities[0]
    else:
        from ledger_agent.core.models import Entity
        _default_name = os.environ.get("FI_DEFAULT_ENTITY_NAME", "Entity")
        entity = Entity(name=_default_name, entity_type="LLC", state="MO")
        EntityRepo.upsert(entity)

    pdf_files = sorted(folder.rglob("*.pdf"))
    for pdf_path in pdf_files:
        try:
            raw_text = BaseStatementParser.extract_text(pdf_path)
            parser_cls = ParserRegistry.detect(raw_text)
            if parser_cls is None:
                log.warning("No parser matched: %s", pdf_path.name)
                report.failed += 1
                report.failed_files.append(pdf_path.name)
                continue

            stmt = parser_cls().parse(pdf_path)

            # Get or create account
            from ledger_agent.core.models import Account, AccountType
            acct = AccountRepo.find(stmt.institution, stmt.account_number_masked or "****")
            if acct is None:
                acct = Account(
                    entity_id=entity.id,
                    name=stmt.institution,
                    institution=stmt.institution,
                    account_type=AccountType.CHECKING,
                    account_number_masked=stmt.account_number_masked or "****",
                )
                AccountRepo.upsert(acct)

            # Idempotency: skip if (account, period) already imported
            if ImportRegistry.already_imported(acct.id, stmt.statement_period):
                report.skipped += 1
                continue

            if stmt.transactions:
                for t in stmt.transactions:
                    t.account_id = acct.id
                TransactionRepo.bulk_insert(stmt.transactions)

                # V9 fix: classify unclassified transactions and persist the
                # coa_code back to the DB immediately.  classify_batch() calls
                # TransactionRepo.update_coa() for every txn it assigns, so the
                # DB and the in-memory objects stay in sync.  Without this call
                # the MCP import path left coa_code='' in the DB; reports then
                # silently skipped those rows, producing non-deterministic totals
                # across re-runs whenever heuristics changed.
                to_classify = [t for t in stmt.transactions if not t.coa_code]
                if to_classify:
                    try:
                        from ledger_agent.core.intelligence.classifier import classify_batch
                        classify_batch(to_classify)  # non-interactive: unmatched → 9999
                        log.info(
                            "Classified %d transactions from %s",
                            len(to_classify), pdf_path.name,
                        )
                    except Exception as cls_exc:
                        # Classification is best-effort; don't fail the import.
                        log.warning(
                            "Classification skipped for %s: %s",
                            pdf_path.name, cls_exc,
                        )

            if stmt.positions:
                for p in stmt.positions:
                    p.account_id = acct.id
                PositionRepo.upsert_period(stmt.positions)

            if stmt.snapshot:
                stmt.snapshot.account_id = acct.id
                SnapshotRepo.upsert(stmt.snapshot)

            ImportRegistry.record(str(pdf_path), stmt.parser_id, acct.id, stmt.statement_period)

            if stmt.statement_period and stmt.statement_period not in report.periods_added:
                report.periods_added.append(stmt.statement_period)
            report.imported += 1
            log.info("Imported %s (%s, %d txns)", pdf_path.name, stmt.statement_period,
                     len(stmt.transactions))

        except Exception as exc:
            log.error("Failed to import %s: %s", pdf_path.name, exc)
            report.failed += 1
            report.failed_files.append(pdf_path.name)

    return report


def generate_balance_sheet(fiscal_year: int):
    from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
    from ledger_agent.core.database import init_db

    init_db()
    entity, periods = _entity_and_periods(fiscal_year)
    if not periods:
        raise ValueError(f"No statement data found for fiscal year {fiscal_year}.")

    last_period = periods[-1]
    return BalanceSheetBuilder(entity.id, last_period, pl_periods=periods).build()


def _compute_net_ltcg(txns) -> Decimal:
    """Compute net long-term capital gain from classified transactions."""
    LTCG_GAIN = {"4011"}
    LTCG_LOSS = {"5075"}
    total = Decimal("0")
    for t in txns:
        if t.is_transfer or not t.coa_code:
            continue
        if t.coa_code in LTCG_GAIN or t.coa_code in LTCG_LOSS:
            total += Decimal(str(t.amount))
    return total


def generate_form_1065(fiscal_year: int) -> Form1065:
    from ledger_agent.core.database import init_db

    init_db()
    entity, _ = _entity_and_periods(fiscal_year)
    txns = _transactions_for_year(fiscal_year)

    income = Decimal("0")
    deductions = Decimal("0")
    net_stcg = Decimal("0")
    dividends = Decimal("0")
    interest = Decimal("0")

    STCG_GAIN = {"4010"}
    STCG_LOSS = {"5070"}
    DIV_CODES = {"4021"}
    INT_CODES = {"4031"}

    for t in txns:
        if t.is_transfer or not t.coa_code:
            continue
        amt = Decimal(str(t.amount))
        code = t.coa_code
        if code in STCG_GAIN:
            net_stcg += amt
        elif code in STCG_LOSS:
            net_stcg += amt
        elif code in DIV_CODES:
            dividends += amt
        elif code in INT_CODES:
            interest += amt
        elif code.startswith("4"):
            income += amt
        elif code.startswith("5"):
            deductions += abs(amt)

    ordinary = income - deductions
    net_ltcg = _compute_net_ltcg(txns)

    f = Form1065(
        fiscal_year=fiscal_year,
        entity_name=entity.name,
        ein_masked=entity.ein_masked or "",
        total_income=income,
        total_deductions=deductions,
        ordinary_business_income=ordinary,
        net_short_term_capital_gain=net_stcg,
        net_long_term_capital_gain=net_ltcg,
        dividend_income=dividends,
        interest_income=interest,
        partner_ids=list(PARTNERS.keys()),
    )
    log.info(
        "Form1065 %d: income=%s deductions=%s ordinary=%s stcg=%s ltcg=%s",
        fiscal_year, income, deductions, ordinary, net_stcg, net_ltcg,
    )
    return f


def generate_k1(fiscal_year: int, partner_id: str) -> ScheduleK1:
    f = generate_form_1065(fiscal_year)

    # Re-read PARTNERS each call so env-var overrides are picked up at runtime
    partners = _build_partners()
    key = partner_id.lower().strip()

    if key in partners:
        name, capital_pct, pl_pct = partners[key]
    else:
        name, capital_pct, pl_pct = partner_id, Decimal("1.00"), Decimal("1.00")

    return ScheduleK1(
        fiscal_year=fiscal_year,
        partner_id=partner_id,
        partner_name=name,
        capital_pct=capital_pct,
        profit_loss_pct=pl_pct,
        ordinary_income_loss=(f.ordinary_business_income * pl_pct).quantize(Decimal("0.01")),
        net_stcg=(f.net_short_term_capital_gain * pl_pct).quantize(Decimal("0.01")),
        net_ltcg=(f.net_long_term_capital_gain * pl_pct).quantize(Decimal("0.01")),
        dividend_income=(f.dividend_income * pl_pct).quantize(Decimal("0.01")),
        interest_income=(f.interest_income * pl_pct).quantize(Decimal("0.01")),
    )


def pte_estimate(fiscal_year: int) -> PTEEstimate:
    from ledger_agent.core.accounting.tax_estimator import TaxEstimator
    from ledger_agent.core.database import init_db

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
    from ledger_agent.core.intelligence.reconciler import reconcile
    from ledger_agent.core.database import init_db

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
