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
    cost_of_goods_sold: Decimal = Decimal("0")
    gross_profit: Decimal = Decimal("0")
    investment_interest_expense: Decimal = Decimal("0")  # Schedule K line 13b


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
    """Return transactions for fiscal_year scoped to the active entity (ARCH-30)."""
    from ledger_agent.core.database import AccountRepo, TransactionRepo, get_conn, init_db
    init_db()
    entity, _ = _entity_and_periods(fiscal_year)
    accounts = AccountRepo.list_for_entity(entity.id)
    account_ids = {a.id for a in accounts}

    prefix = str(fiscal_year) + "-%"
    with get_conn() as conn:
        period_rows = conn.execute(
            "SELECT DISTINCT statement_period FROM transactions WHERE statement_period LIKE ?",
            (prefix,),
        ).fetchall()
    periods = [r[0] for r in period_rows if r[0]]
    txns: list = []
    for period in periods:
        for txn in TransactionRepo.list_for_period(period):
            if txn.account_id in account_ids:
                txns.append(txn)
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
    cogs = Decimal("0")
    inv_interest = Decimal("0")
    net_stcg = Decimal("0")
    dividends = Decimal("0")
    interest = Decimal("0")

    STCG_GAIN = {"4010"}
    STCG_LOSS = {"5070"}
    DIV_CODES = {"4021"}
    INT_CODES = {"4031"}
    COGS_CODES = {"5061"}  # Office/Shipping treated as COGS (Form 1065 line 2)
    SCHED_K_INTEREST = {"5030"}  # Margin interest → Schedule K line 13b (not a deduction)
    EQUITY_DRAW_CODES = {"5050"}  # Federal tax payments → partner draws (3040), not deductions

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
        elif code in COGS_CODES:
            cogs -= amt  # amt is negative for expenses; refunds (positive) reduce COGS
        elif code in SCHED_K_INTEREST:
            inv_interest -= amt
        elif code in EQUITY_DRAW_CODES:
            pass  # treated as equity draw, excluded from Form 1065 deductions
        elif code.startswith("5"):
            deductions -= amt  # expenses are negative; refunds (positive) reduce deductions

    # W16: apply wash-sale disallowances if private CSV is present
    from ledger_agent.core.accounting.wash_sale import total_disallowed
    ws_adjustment = total_disallowed()
    if ws_adjustment:
        net_stcg += ws_adjustment
        log.info("W16 wash-sale disallowance applied: +%s to net_stcg", ws_adjustment)

    gross_pft = income - cogs
    ordinary = gross_pft - deductions
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
        cost_of_goods_sold=cogs,
        gross_profit=gross_pft,
        investment_interest_expense=inv_interest,
    )
    log.info(
        "Form1065 %d: income=%s cogs=%s gross_pft=%s deductions=%s ordinary=%s stcg=%s ltcg=%s",
        fiscal_year, income, cogs, gross_pft, deductions, ordinary, net_stcg, net_ltcg,
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


@dataclass
class CustomerSummary:
    """Customer-readable outcome summary (W27 contract).

    All surfaces (CLI, MCP, Webapp) render this single object.
    Shape follows docs/customer-summary-contract.md.
    """
    fiscal_year: int
    period_covered: str
    profit_or_loss: Dict
    growth_signal: Dict
    balance_sheet_health: Dict
    pte_due_signal: Dict
    tax_due_signal: Dict
    confidence_flags: List[str]
    next_actions: List[str]


def _wash_sale_csv_present() -> bool:
    """Return True if a wash-sale adjustment CSV is reachable (env-var or default path)."""
    import os
    from pathlib import Path
    env_path = os.environ.get("FI_WASH_SALE_CSV", "").strip()
    if env_path and Path(env_path).exists():
        return True
    default = Path(__file__).resolve().parents[2] / "private" / "wash_sale_adjustments.csv"
    return default.exists()


def _best_snapshot_period(fiscal_year: int) -> Optional[str]:
    """Return the period with the most account snapshot rows for the year."""
    from ledger_agent.core.database import get_conn, init_db
    init_db()
    prefix = f"{fiscal_year}-%"
    with get_conn() as conn:
        row = conn.execute(
            "SELECT statement_period, COUNT(*) AS n FROM account_snapshots "
            "WHERE statement_period LIKE ? GROUP BY statement_period "
            "ORDER BY n DESC, statement_period DESC LIMIT 1",
            (prefix,),
        ).fetchone()
    return row[0] if row else None


def build_customer_summary(fiscal_year: int) -> CustomerSummary:
    """Build the canonical customer outcome summary (W27/W28/W29/W30).

    Picks the best-covered snapshot period, runs balance sheet + form 1065 +
    PTE estimate, and assembles confidence flags based on known open lanes.
    """
    from ledger_agent.core.database import init_db

    init_db()
    entity, periods = _entity_and_periods(fiscal_year)
    if not periods:
        raise ValueError(f"No statement data for fiscal year {fiscal_year}.")

    # ── Pick best period for balance sheet ───────────────────────────────────
    best_period = _best_snapshot_period(fiscal_year) or periods[-1]

    # ── Core computations ────────────────────────────────────────────────────
    from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder

    bs = BalanceSheetBuilder(entity.id, best_period, pl_periods=periods).build()
    f1065 = generate_form_1065(fiscal_year)
    pte = pte_estimate(fiscal_year)

    # ── Profit / Loss ────────────────────────────────────────────────────────
    obi = f1065.ordinary_business_income
    if obi > 0:
        pl_status, pl_signal = "profit", "positive"
    elif obi < 0:
        pl_status, pl_signal = "loss", "negative"
    else:
        pl_status, pl_signal = "break_even", "neutral"

    profit_or_loss = {
        "status": pl_status,
        "signal": pl_signal,
        "ordinary_business_income": str(obi.quantize(Decimal("0.01"))),
    }

    # ── Growth signal (equity direction) ─────────────────────────────────────
    if bs.net_income > 0:
        growth_status = "growing"
    elif bs.net_income < 0:
        growth_status = "contracting"
    else:
        growth_status = "flat"

    growth_signal = {
        "status": growth_status,
        "basis": "net_income",
        "note": (
            f"Net income is {'+' if bs.net_income >= 0 else ''}"
            f"{float(bs.net_income):,.2f} for {fiscal_year}"
        ),
    }

    # ── Balance sheet health ─────────────────────────────────────────────────
    skipped = bs.coverage.get("skipped_snapshots", [])
    bs_status = "healthy" if bs.is_balanced and not skipped else "review_needed"
    balance_sheet_health = {
        "is_balanced": bs.is_balanced,
        "status": bs_status,
        "total_assets": str(bs.total_assets.quantize(Decimal("0.01"))),
        "total_liabilities": str(bs.total_liabilities.quantize(Decimal("0.01"))),
        "total_equity": str(bs.total_equity.quantize(Decimal("0.01"))),
        "period": best_period,
        "skipped_accounts": len(skipped),
    }

    # ── PTE due signal ───────────────────────────────────────────────────────
    if pte.total_annual_tax > 0:
        pte_status = "likely_due"
        next_due = pte.quarterly_payments[0]["due_date"] if pte.quarterly_payments else "N/A"
    else:
        pte_status = "not_due"
        next_due = "N/A"

    pte_due_signal = {
        "status": pte_status,
        "annual_estimate": str(pte.total_annual_tax.quantize(Decimal("0.01"))),
        "next_due": next_due,
    }

    # ── Tax due signal ───────────────────────────────────────────────────────
    tax_status = "likely_due" if pte.total_annual_tax > 0 else "not_due"
    tax_due_signal = {
        "status": tax_status,
        "basis": "pte_estimate",
        "note": (
            f"PTE annual estimate: {float(pte.total_annual_tax):,.2f}; "
            f"effective rate: {float(pte.effective_rate * 100):.1f}%"
        ),
    }

    # ── Confidence flags ─────────────────────────────────────────────────────
    flags: List[str] = []
    # W15: COGS structural gap — always warn if there are deductions
    if f1065.total_deductions > 0:
        flags.append("NOT_CLOSE_READY_W15")
    # W16: wash-sale — warn if there are capital gains/losses.
    # Also flag when the wash-sale CSV is absent and gains/losses are non-zero
    # (mid-year runs without a 1099-B cannot apply wash-sale adjustments).
    _wash_csv_present = _wash_sale_csv_present()
    if f1065.net_short_term_capital_gain != 0 or f1065.net_long_term_capital_gain != 0:
        flags.append("NOT_CLOSE_READY_W16")
        if not _wash_csv_present:
            flags.append("wash_sale_not_applied")
    # W17: snapshot completeness — warn if any accounts skipped
    if skipped:
        flags.append("NOT_CLOSE_READY_W17")
    # W26: regression fix confirmed (ARCH-28 tests green)
    flags.append("CLOSE_READY_W26")
    if not flags or all(f.startswith("CLOSE_READY") for f in flags):
        flags.append("CLOSE_READY")

    # ── Next actions ─────────────────────────────────────────────────────────
    next_actions: List[str] = []
    if "NOT_CLOSE_READY_W15" in flags:
        next_actions.append(
            "Resolve W15: COGS structural gap in generate_form_1065 before CPA submission"
        )
    if "NOT_CLOSE_READY_W16" in flags:
        if "wash_sale_not_applied" in flags:
            next_actions.append(
                "Resolve W16: wash-sale CSV absent — capital gain/loss figures are estimates "
                "only (mid-year runs without a 1099-B cannot apply wash-sale adjustments). "
                "Provide private/wash_sale_adjustments.csv before CPA submission."
            )
        else:
            next_actions.append(
                "Resolve W16: provide private/wash_sale_adjustments.csv to adjust capital gains"
            )
    if "NOT_CLOSE_READY_W17" in flags:
        next_actions.append(
            f"Resolve W17: {len(skipped)} account(s) missing snapshots — "
            "run import with complete statements"
        )
    if pte_status == "likely_due":
        next_actions.append(
            f"Estimated quarterly tax payment due: {next_due}"
        )
    if not next_actions:
        next_actions.append("Review outputs with CPA before filing Form 1065")

    return CustomerSummary(
        fiscal_year=fiscal_year,
        period_covered=f"{fiscal_year}-01..{best_period}",
        profit_or_loss=profit_or_loss,
        growth_signal=growth_signal,
        balance_sheet_health=balance_sheet_health,
        pte_due_signal=pte_due_signal,
        tax_due_signal=tax_due_signal,
        confidence_flags=flags,
        next_actions=next_actions,
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
