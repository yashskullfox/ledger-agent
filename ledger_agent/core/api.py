from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Tuple

log = logging.getLogger(__name__)

# ── Module-level partner table (ARCH-19 / CRIT-03) ───────────────────────────
# Keys are the canonical partner_id strings used by CLI/MCP/bridge.
# Each value is (name, capital_pct, profit_loss_pct).
# Capital and P&L splits are independent in partnership accounting (K-1 Part II J).
# Defaults match the entity's 2024 LLC agreement: Partner A 99% capital / 100% P&L, Partner B 1% / 0%.
# Override via env vars without touching code.
def _build_partners() -> Dict[str, Tuple[str, Decimal, Decimal]]:
    return {
        "yash": (
            os.environ.get("FI_PARTNER_YASH_NAME", "Partner A"),
            Decimal(os.environ.get("FI_PARTNER_YASH_CAPITAL", "0.99")),
            Decimal(os.environ.get("FI_PARTNER_YASH_PL", "1.00")),
        ),
        "parin": (
            os.environ.get("FI_PARTNER_PARIN_NAME", "Partner B"),
            Decimal(os.environ.get("FI_PARTNER_PARIN_CAPITAL", "0.01")),
            Decimal(os.environ.get("FI_PARTNER_PARIN_PL", "0.00")),
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


# ── Public API ────────────────────────────────────────────────────────────────

def import_statements(
    folder: Path,
    *,
    allow_partial: bool = False,
) -> ImportReport:
    from parsers.registry import ParserRegistry
    from parsers.base import BaseStatementParser
    from core.database import (
        init_db, AccountRepo, EntityRepo, SnapshotRepo,
        TransactionRepo, PositionRepo, ImportedStatementRepo,
    )
    import parsers  # noqa: F401  — triggers auto-discovery in parsers/__init__.py

    # Lazy audit/cleanup import — avoids hard failure if optional modules absent.
    try:
        from core.audit import audit
        from core.cleanup import run_cycle, boot_cleanup
        boot_cleanup()
        audit("import.start", folder=str(folder), allow_partial=allow_partial)
    except Exception:
        audit = lambda *a, **k: None  # type: ignore
        from contextlib import nullcontext
        run_cycle = lambda label: nullcontext()  # type: ignore

    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f"Folder does not exist: {folder}")

    init_db()
    report = ImportReport()

    pdf_files = sorted(folder.rglob("*.pdf"))
    audit("import.discovery", pdf_count=len(pdf_files))
    for pdf_path in pdf_files:
        try:
            if ImportedStatementRepo.exists(str(pdf_path)):
                report.skipped += 1
                continue

            raw_text = BaseStatementParser.extract_text(pdf_path)
            parser_cls = ParserRegistry.detect(raw_text)
            if parser_cls is None:
                log.warning("No parser matched: %s", pdf_path.name)
                report.failed += 1
                report.failed_files.append(pdf_path.name)
                continue

            stmt = parser_cls().parse(pdf_path)

            entities = EntityRepo.list_all()
            if entities:
                entity = entities[0]
            else:
                from core.models import Entity
                _default_name = os.environ.get("FI_DEFAULT_ENTITY_NAME", "Entity")
                entity = Entity(name=_default_name, entity_type="LLC", state="MO")
                EntityRepo.upsert(entity)

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

            if stmt.snapshot:
                snap = stmt.snapshot
                snap.account_id = account.id
                SnapshotRepo.upsert(snap)

            if stmt.transactions:
                for t in stmt.transactions:
                    t.account_id = account.id
                TransactionRepo.bulk_insert(stmt.transactions)

            if stmt.positions:
                for p in stmt.positions:
                    p.account_id = account.id
                PositionRepo.upsert_period(stmt.positions)

            ImportedStatementRepo.record(
                file_path=str(pdf_path),
                parser_id=stmt.parser_id,
                account_id=account.id,
                period=stmt.statement_period or "",
            )

            if stmt.statement_period and stmt.statement_period not in report.periods_added:
                report.periods_added.append(stmt.statement_period)
            report.imported += 1
            log.info("Imported %s (%s)", pdf_path.name, stmt.statement_period)

        except Exception as exc:
            log.error("Failed to import %s: %s", pdf_path.name, exc)
            report.failed += 1
            report.failed_files.append(pdf_path.name)

    # ARCH-27: classify all unclassified transactions after bulk import.
    # Non-interactive batch mode (prompt_fn=None) — unknown transactions get
    # code "9999" and can be corrected via CLI or MCP later.
    try:
        from core.database import TransactionRepo as _TxnRepo
        from intelligence.classifier import classify_batch
        unclassified = _TxnRepo.list_unclassified()
        if unclassified:
            classify_batch(unclassified, prompt_fn=None)
            audit("import.classified", count=len(unclassified))
    except Exception as exc:
        log.warning("classify step skipped: %s", exc)

    audit(
        "import.complete",
        imported=report.imported,
        skipped=report.skipped,
        failed=report.failed,
        periods_added=report.periods_added,
    )
    # Cycle cleanup — purge anything pdfplumber / parsers may have spilled.
    try:
        from core.cleanup import cycle_cleanup
        cycle_cleanup("import_statements")
    except Exception:
        pass
    return report


def generate_balance_sheet(fiscal_year: int):
    from accounting.balance_sheet import BalanceSheetBuilder
    from core.database import init_db

    init_db()
    entity, periods = _entity_and_periods(fiscal_year)
    if not periods:
        raise ValueError(f"No statement data found for fiscal year {fiscal_year}.")

    last_period = periods[-1]
    # Pass all fiscal-year periods so P&L aggregates the full year (not just last month)
    return BalanceSheetBuilder(entity.id, last_period, pl_periods=periods).build()


def _compute_net_ltcg(txns) -> Decimal:
    """Compute net long-term capital gain from classified transactions.

    4011 = Realised Long-Term Trading Gains
    5075 = Realised Long-Term Trading Losses
    Note: 5071 (Legal & Professional Fees) is NOT an LTCG code.
    """
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
    from core.database import init_db

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


def check_period_continuity(entity_id: str, tolerance: Decimal = Decimal("1.00")) -> List[dict]:
    """ARCH-31: Verify that each period's opening balance matches the prior period's closing.

    For every account that has snapshots in consecutive periods, compare
    ``beginning_balance[N+1]`` with ``ending_balance[N]``.  If the delta
    exceeds *tolerance* (default $1.00), a ``prior_period_adjustment``
    transaction is created and returned in the result list.

    Returns a list of adjustment dicts, one per account-period gap detected.
    """
    from core.database import AccountRepo, SnapshotRepo, TransactionRepo, init_db
    from core.models import Transaction, TransactionType
    from datetime import date as _date_type

    init_db()
    adjustments: List[dict] = []
    accounts = AccountRepo.list_for_entity(entity_id)

    all_snapshots = sorted(
        SnapshotRepo.list_for_entity(entity_id),
        key=lambda s: s.statement_period,
    )
    for acct in accounts:
        acct_snaps = [s for s in all_snapshots if s.account_id == acct.id]
        if len(acct_snaps) < 2:
            continue

        for i in range(len(acct_snaps) - 1):
            prev = acct_snaps[i]
            curr = acct_snaps[i + 1]
            if curr.beginning_balance is None:
                continue
            delta = curr.beginning_balance - prev.ending_balance
            if abs(delta) <= tolerance:
                continue

            # Record the adjustment
            try:
                year_str, month_str = curr.statement_period[:4], curr.statement_period[5:7]
                adj_date = _date_type(int(year_str), int(month_str), 1)
            except Exception:
                continue

            adj_txn = Transaction(
                account_id=acct.id,
                date=adj_date,
                description=(
                    f"Prior-period adjustment: {prev.statement_period} close "
                    f"→ {curr.statement_period} open (delta ${delta:+.2f})"
                ),
                raw_description="prior_period_adjustment",
                amount=delta,
                transaction_type=TransactionType.PRIOR_PERIOD_ADJUSTMENT,
                statement_period=curr.statement_period,
                coa_code="3020",  # Retained Earnings
                coa_name="Retained Earnings",
                notes=(
                    f"ARCH-31 auto-generated: prior period {prev.statement_period} "
                    f"ending={prev.ending_balance}, current {curr.statement_period} "
                    f"beginning={curr.beginning_balance}, delta={delta}"
                ),
            )
            TransactionRepo.bulk_insert([adj_txn])
            adjustments.append({
                "account_id": acct.id,
                "account": str(acct),
                "prior_period": prev.statement_period,
                "current_period": curr.statement_period,
                "prior_ending": str(prev.ending_balance),
                "current_opening": str(curr.beginning_balance),
                "delta": str(delta),
                "adjustment_transaction_id": adj_txn.id,
            })
            log.info(
                "ARCH-31: prior_period_adjustment created for %s %s→%s delta=%s",
                acct, prev.statement_period, curr.statement_period, delta,
            )

    return adjustments


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
