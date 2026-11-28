from __future__ import annotations

import calendar
import logging
import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from ledger_agent.core.config import NOT_TAX_ADVICE_DISCLAIMER

log = logging.getLogger(__name__)

# ── Module-level partner table (ARCH-19 / CRIT-03) ───────────────────────────
# Keys are the canonical partner_id strings used by CLI/MCP/bridge.
# Each value is (name, capital_pct, profit_loss_pct).
# Capital and P&L splits are independent in partnership accounting (K-1 Part II J).
# Default split for the reference ENTITY_A: partner_1 majority capital / full P&L,
# partner_2 minority capital / no P&L share.  Override via env vars without
# touching code.
def _build_partners() -> dict[str, tuple[str, Decimal, Decimal]]:
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


PARTNERS: dict[str, tuple[str, Decimal, Decimal]] = _build_partners()


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class ImportReport:
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    failed_files: list[str] = field(default_factory=list)
    periods_added: list[str] = field(default_factory=list)

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
    partner_ids: list[str] = field(default_factory=list)
    cost_of_goods_sold: Decimal = Decimal("0")
    gross_profit: Decimal = Decimal("0")
    investment_interest_expense: Decimal = Decimal("0")  # Schedule K line 13b
    disclaimer: str = NOT_TAX_ADVICE_DISCLAIMER


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
    disclaimer: str = NOT_TAX_ADVICE_DISCLAIMER

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
    paid_ytd: Decimal = Decimal("0")
    remaining_balance: Decimal = Decimal("0")
    notes: str = ""
    disclaimer: str = NOT_TAX_ADVICE_DISCLAIMER
    # Period basis (see accounting/period_coverage.py):
    #   full_year | quarter_to_date | partial_quarter | insufficient_data
    basis: str = "full_year"
    through_period: str | None = None      # last statement month included, e.g. "2026-06"
    months_covered: int = 12
    ytd_net_income: Decimal = Decimal("0")  # actual (not annualized) income through_period
    required_to_date: Decimal = Decimal("0")  # cumulative installments due through last full Q
    missing_statements: dict = field(default_factory=dict)  # account_id → [missing months]


@dataclass
class ReconcileReport:
    fiscal_year: int = 0
    matched: int = 0
    unmatched: int = 0
    total_transfers: Decimal = Decimal("0")
    issues: list[str] = field(default_factory=list)
    disclaimer: str = NOT_TAX_ADVICE_DISCLAIMER

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

    prefix = str(fiscal_year) + "-%"
    with get_conn() as conn:
        # Pick the entity that actually has data for this fiscal year.
        # If multiple entities do, prefer the one with the highest txn count.
        row = conn.execute(
            "SELECT a.entity_id, COUNT(*) AS n "
            "FROM transactions t "
            "JOIN accounts a ON a.id = t.account_id "
            "WHERE t.statement_period LIKE ? "
            "GROUP BY a.entity_id "
            "ORDER BY n DESC, a.entity_id ASC "
            "LIMIT 1",
            (prefix,),
        ).fetchone()

        if row:
            target_entity_id = row[0]
            entity = next((e for e in entities if e.id == target_entity_id), entities[0])
        else:
            entity = entities[0]

        rows = conn.execute(
            "SELECT DISTINCT t.statement_period "
            "FROM transactions t "
            "JOIN accounts a ON a.id = t.account_id "
            "WHERE a.entity_id = ? AND t.statement_period LIKE ? "
            "ORDER BY t.statement_period",
            (entity.id, prefix),
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
    if not periods or not account_ids:
        return []
    return TransactionRepo.list_for_periods(periods, account_ids=list(account_ids))


# ── Public API ────────────────────────────────────────────────────────────────

def import_statements(
    folder: Path,
    *,
    allow_partial: bool = False,
) -> ImportReport:
    import ledger_agent.core.parsers  # noqa: F401 — trigger auto-discovery
    from ledger_agent.core.database import (
        AccountRepo,
        EntityRepo,
        ImportRegistry,
        PositionRepo,
        SnapshotRepo,
        TransactionRepo,
        init_db,
    )
    from ledger_agent.core.parsers.base import BaseStatementParser
    from ledger_agent.core.parsers.registry import ParserRegistry

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
    # C-03: entities whose accounts received rows in this run (transfer-candidate pass).
    touched_entities: set[str] = set()
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
            from ledger_agent.core.models import Account, AccountType, StatementType
            _stmt_to_acct_type = {
                StatementType.BANK_CHECKING: AccountType.CHECKING,
                StatementType.BANK_SAVINGS: AccountType.SAVINGS,
                StatementType.BROKERAGE: AccountType.BROKERAGE,
                StatementType.CREDIT_CARD: AccountType.CREDIT_CARD,
            }
            inferred_type = _stmt_to_acct_type.get(stmt.statement_type, AccountType.CHECKING)
            acct = AccountRepo.find(stmt.institution, stmt.account_number_masked or "****")
            if acct is None:
                acct = Account(
                    entity_id=entity.id,
                    name=stmt.institution,
                    institution=stmt.institution,
                    account_type=inferred_type,
                    account_number_masked=stmt.account_number_masked or "****",
                )
                AccountRepo.upsert(acct)
            elif acct.account_type == AccountType.CHECKING and inferred_type != AccountType.CHECKING:
                acct.account_type = inferred_type
                AccountRepo.upsert(acct)

            # Idempotency: skip if (account, period) already imported
            if ImportRegistry.already_imported(acct.id, stmt.statement_period):
                report.skipped += 1
                continue

            if stmt.transactions:
                for t in stmt.transactions:
                    t.account_id = acct.id
                TransactionRepo.bulk_insert(stmt.transactions)
                if acct.entity_id:
                    touched_entities.add(acct.entity_id)

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

                # ARCH-29: Reclassify partner tax payments to equity draws (3040)
                try:
                    stmt_year = None
                    if stmt.statement_period:
                        try:
                            stmt_year = int(stmt.statement_period.split("-")[0])
                        except (ValueError, IndexError) as period_parse_exc:
                            log.debug(
                                "Could not parse statement_period '%s' for %s; "
                                "falling back to transaction dates: %s",
                                stmt.statement_period,
                                pdf_path.name,
                                period_parse_exc,
                            )
                    if stmt_year is None and stmt.transactions:
                        for t in stmt.transactions:
                            if t.date:
                                stmt_year = t.date.year
                                break
                    if stmt_year:
                        from ledger_agent.core.accounting.withholding import (
                            reclassify_partner_withholding,
                        )
                        n_reclassified = reclassify_partner_withholding(entity.id, stmt_year)
                        if n_reclassified:
                            log.info(
                                "Reclassified %d partner withholding txn(s) for FY%d (%s)",
                                n_reclassified, stmt_year, pdf_path.name,
                            )
                except Exception as rw_exc:
                    log.warning(
                        "Withholding reclassification skipped for %s: %s",
                        pdf_path.name, rw_exc,
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

    # C-03: after every statement is in (both legs of a transfer may sit in different
    # PDFs), confirm transfer candidates whose opposite leg is in another account of
    # the same entity.  Import path only — never run from read / report paths.
    for entity_id in sorted(touched_entities):
        try:
            n_confirmed = _confirm_transfer_candidates(entity_id)
            if n_confirmed:
                log.info("Confirmed %d internal transfer leg(s) by opposite-leg match",
                         n_confirmed)
        except Exception as xfer_exc:
            log.warning("Transfer-candidate confirmation skipped: %s", xfer_exc)

    return report


def confirm_transfer_candidates(entity_id: str) -> int:
    """C-12: public import-time entry point for the C-03 transfer-candidate pass.

    For import paths that insert and classify on their own (the CLI ``cmd_import`` and
    quick scan).  Call it once per affected entity after classification and before
    any post-import reconcile.  Entity-scoped and idempotent; returns the number of
    rows promoted to 9000.  Never call it from read / report paths.
    """
    return _confirm_transfer_candidates(entity_id)


def _confirm_transfer_candidates(entity_id: str) -> int:
    """C-03: promote ``transfer_candidate`` rows that have an opposite leg.

    Parsers emit generic ZELLE / WIRE / TRANSFER rows without an own-account cue as
    candidates (``is_transfer=False``), and the classifier codes them like any other
    row.  Here, entity-scoped (ARCH-30), the reconciler looks for the opposite leg:

    1. Existing internal transfers (``is_transfer=1``) are paired among themselves
       first, so a candidate never takes a leg that already has its partner.
    2. Candidates are then paired with each other and with the still-unpaired
       transfers: same absolute amount, different account of this entity, within
       ``MATCH_WINDOW_DAYS``.

    Each matched candidate becomes 9000 / ``is_transfer=True`` (and loses any
    ``owner_draw_candidate`` tag: it stayed inside the entity).  Unmatched candidates
    keep the classifier's result.  Rows coded 3040 (owner draw / distribution) are
    never promoted.  Idempotent: a promoted row is an internal transfer on the next
    run and is paired in step 1.  Returns the number of rows promoted.
    """
    from dataclasses import replace

    from ledger_agent.core.database import AccountRepo, TransactionRepo
    from ledger_agent.core.intelligence.classifier import (
        CLASSIFIER_VERSION,
        TRANSFER_CANDIDATE_TAG,
    )
    from ledger_agent.core.intelligence.owner_draws import OWNER_DRAW_CANDIDATE_TAG
    from ledger_agent.core.intelligence.reconciler import reconcile
    from ledger_agent.core.models import TransactionType

    account_ids = [a.id for a in AccountRepo.list_for_entity(entity_id)]
    rows = TransactionRepo.list_transfer_legs(account_ids, TRANSFER_CANDIDATE_TAG)
    if not rows:
        return 0

    def _as_leg(t):
        # The reconciler pairs by type; type each leg by its sign (some stored
        # 9000 rows are typed debit/credit).  Copies only: rows are not mutated.
        kind = TransactionType.TRANSFER_OUT if t.amount < 0 else TransactionType.TRANSFER_IN
        return replace(t, transaction_type=kind)

    transfers = [_as_leg(t) for t in rows if t.is_transfer]
    candidates = [
        _as_leg(t) for t in rows
        if not t.is_transfer and t.coa_code != "3040"
        and TRANSFER_CANDIDATE_TAG in (t.tags or [])
    ]
    if not candidates:
        return 0

    paired, _ = reconcile(transfers)
    taken = {t.id for m in paired for t in (m.outgoing, m.incoming)}
    pool = candidates + [t for t in transfers if t.id not in taken]
    matches, _ = reconcile(pool)

    candidate_ids = {t.id for t in candidates}
    promoted = 0
    for m in matches:
        for leg in (m.outgoing, m.incoming):
            if leg.id not in candidate_ids:
                continue
            TransactionRepo.update_coa_with_meta(
                leg.id, "9000", "Inter-Account Transfer",
                CLASSIFIER_VERSION, 1.0, is_transfer=True,
            )
            if OWNER_DRAW_CANDIDATE_TAG in (leg.tags or []):
                TransactionRepo.set_tags(
                    leg.id, [tag for tag in leg.tags if tag != OWNER_DRAW_CANDIDATE_TAG]
                )
            promoted += 1
    return promoted


def generate_balance_sheet(fiscal_year: int, period: str | None = None):
    from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
    from ledger_agent.core.database import get_conn, init_db
    from ledger_agent.core.exceptions import AggregationGap

    init_db()
    entity, periods = _entity_and_periods(fiscal_year)
    if not periods:
        raise ValueError(f"No statement data found for fiscal year {fiscal_year}.")

    last_period = period or _best_snapshot_period(fiscal_year) or periods[-1]
    if period is not None:
        if not period.startswith(f"{fiscal_year}-"):
            raise ValueError(f"period {period!r} is outside fiscal year {fiscal_year}.")
        # A month-end / quarter-end balance sheet only includes P&L earned up to that month.
        pl_periods = [p for p in periods if p <= period]
    else:
        pl_periods = periods
    bs = BalanceSheetBuilder(entity.id, last_period, pl_periods=pl_periods).build()
    skipped = bs.coverage.get("skipped_snapshots", [])

    # An "AggregationGap" is only a real gap if the account HAS other data in
    # the same fiscal year and is missing the terminal-period snapshot. If the
    # account has zero fiscal-year activity (snapshots + transactions), then
    # it simply wasn't open during that year — reclassify as a vacuous skip
    # rather than a hard error. Prevents opening a new account in FY2026 from
    # breaking the FY2024 / FY2025 balance sheet.
    year_prefix = f"{fiscal_year}-%"
    real_gaps: list[dict] = []
    acct_ids = list({s.get("account_id") for s in skipped if s.get("account_id")})
    snap_counts: dict[str, int] = {}
    txn_counts: dict[str, int] = {}
    if acct_ids:
        placeholders = ",".join("?" for _ in acct_ids)
        with get_conn() as conn:
            snap_rows = conn.execute(
                f"SELECT account_id, COUNT(*) FROM account_snapshots "
                f"WHERE account_id IN ({placeholders}) AND statement_period LIKE ? "
                f"GROUP BY account_id",
                (*acct_ids, year_prefix),
            ).fetchall()
            snap_counts = {r[0]: r[1] for r in snap_rows}

            txn_rows = conn.execute(
                f"SELECT account_id, COUNT(*) FROM transactions "
                f"WHERE account_id IN ({placeholders}) AND statement_period LIKE ? "
                f"GROUP BY account_id",
                (*acct_ids, year_prefix),
            ).fetchall()
            txn_counts = {r[0]: r[1] for r in txn_rows}

    for s in skipped:
        acct_id = s.get("account_id")
        if not acct_id:
            real_gaps.append(s)
            continue
        snap_n = snap_counts.get(acct_id, 0)
        txn_n = txn_counts.get(acct_id, 0)
        if snap_n == 0 and txn_n == 0:
            # Account not open during this fiscal year — annotate the
            # coverage manifest and drop from the raise-worthy list.
            s["reason"] = f"account inactive in FY{fiscal_year} (0 snapshots, 0 txns)"
            continue
        real_gaps.append(s)

    # Update the coverage manifest so downstream consumers see the refined
    # classification without losing audit trail.
    bs.coverage["skipped_snapshots"] = real_gaps + [
        s for s in skipped if s not in real_gaps
    ]

    if real_gaps:
        first = real_gaps[0]
        raise AggregationGap(
            last_period,
            first.get("account_id", "unknown"),
            first.get("reason", "account snapshot missing"),
        )
    return bs


# Long-term capital gain (4011) / loss (5075). These are Schedule K items only and must
# never also flow into ordinary income or deductions (they used to, via the "4"/"5" prefix
# fall-through in generate_form_1065 — double count).
LTCG_CODES = frozenset({"4011", "5075"})


def _compute_net_ltcg(txns) -> Decimal:
    """Compute net long-term capital gain from classified transactions."""
    total = Decimal("0")
    for t in txns:
        if t.is_transfer or not t.coa_code:
            continue
        if t.coa_code in LTCG_CODES:
            total += Decimal(str(t.amount))
    return total


def generate_form_1065(fiscal_year: int, through_period: str | None = None) -> Form1065:
    """Form 1065 figures for the fiscal year.

    ``through_period`` ("YYYY-MM") restricts income/deductions to statements up to and
    including that month — used for quarter-to-date tax estimates.  ``None`` = full year.
    """
    from ledger_agent.core.database import init_db

    init_db()
    entity, periods = _entity_and_periods(fiscal_year)
    if not periods:
        raise ValueError(f"No statement data found for fiscal year {fiscal_year}.")
    txns = _transactions_for_year(fiscal_year)
    if through_period is not None:
        if not through_period.startswith(f"{fiscal_year}-"):
            raise ValueError(f"through_period {through_period!r} outside fiscal year {fiscal_year}.")
        txns = [t for t in txns if (t.statement_period or "") <= through_period]

    income = Decimal("0")
    deductions = Decimal("0")
    cogs = Decimal("0")
    inv_interest = Decimal("0")
    net_stcg = Decimal("0")
    dividends = Decimal("0")
    interest = Decimal("0")

    stcg_gain = {"4010"}
    stcg_loss = {"5070"}
    div_codes = {"4021"}
    int_codes = {"4031", "4040"}
    cogs_codes = {"5061"}  # Office/Shipping treated as COGS (Form 1065 line 2)
    sched_k_interest = {"5030"}  # Margin interest → Schedule K line 13b (not a deduction)
    equity_draw_codes = {"5050"}  # Federal tax payments → partner draws (3040), not deductions

    for t in txns:
        if t.is_transfer or not t.coa_code:
            continue
        amt = Decimal(str(t.amount))
        code = t.coa_code
        if code in stcg_gain:
            net_stcg += amt
        elif code in stcg_loss:
            net_stcg += amt
        elif code in div_codes:
            dividends += amt
        elif code in int_codes:
            interest += amt
        elif code in LTCG_CODES:
            pass  # long-term gains/losses → Schedule K line 9a via _compute_net_ltcg only
        elif code.startswith("4"):
            income += amt
        elif code in cogs_codes:
            cogs -= amt  # amt is negative for expenses; refunds (positive) reduce COGS
        elif code in sched_k_interest:
            inv_interest -= amt
        elif code in equity_draw_codes:
            pass  # treated as equity draw, excluded from Form 1065 deductions
        elif code.startswith("5"):
            deductions -= amt  # expenses are negative; refunds (positive) reduce deductions

    # W16: apply wash-sale disallowances. Preference order:
    #   1. private/wash_sale_adjustments.csv  (authoritative — from 1099-B)
    #   2. computed from own ledger (SELL@loss + position deltas ±30d)
    #   3. zero (WARNING logged by the module)
    # C-10: each disallowed loss goes back to the line its loss was booked on —
    # long-term (5075) → net LTCG, short-term / term_unknown → net STCG.
    from ledger_agent.core.accounting.wash_sale import disallowed_by_term
    ws_short, ws_long = disallowed_by_term(
        entity_id=entity.id,
        fiscal_year=fiscal_year,
    )
    if through_period is not None and not through_period.endswith("-12"):
        # Annual 1099-B wash-sale figures cannot be apportioned to a partial year.
        ws_short = ws_long = Decimal("0")

    gross_pft = income - cogs
    ordinary = gross_pft - deductions
    net_ltcg = _compute_net_ltcg(txns)
    if ws_short:
        net_stcg += ws_short
        log.info("W16 wash-sale disallowance applied: +%s to net_stcg", ws_short)
    if ws_long:
        net_ltcg += ws_long
        log.info("W16 wash-sale disallowance applied: +%s to net_ltcg", ws_long)

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


def record_pte_payment(
    fiscal_year: int,
    quarter: str,
    amount: Decimal | str | float,
    paid_on: str | None = None,
    source: str = "manual",
) -> dict:
    """Record an entity-level external PTE tax payment (W31 / T7)."""
    from ledger_agent.core.database import EntityRepo, PTEPaymentRepo, init_db

    init_db()
    q_norm = quarter.strip().upper()
    if q_norm not in ("Q1", "Q2", "Q3", "Q4"):
        raise ValueError(f"quarter must be one of Q1, Q2, Q3, Q4; got {quarter!r}")
    amt_dec = Decimal(str(amount)).quantize(Decimal("0.01"))
    if amt_dec < Decimal("0"):
        raise ValueError(f"amount must be non-negative; got {amount!r}")

    entities = EntityRepo.list_all()
    if not entities:
        raise ValueError("No entities found in database — run import_statements() first.")
    try:
        entity, _ = _entity_and_periods(fiscal_year)
    except Exception:
        entity = entities[0]

    rec = PTEPaymentRepo.upsert(
        entity_id=entity.id,
        fiscal_year=fiscal_year,
        quarter=q_norm,
        amount=amt_dec,
        paid_on=paid_on,
        source=source,
    )
    try:
        from ledger_agent.core.audit import audit

        audit(
            "pte.payment_recorded",
            entity_id=entity.id,
            fiscal_year=fiscal_year,
            quarter=q_norm,
            amount=str(amt_dec),
            paid_on=paid_on,
            source=source,
        )
    except Exception as aud_exc:
        log.debug("Audit emission skipped for pte.payment_recorded: %s", aud_exc)
    return rec


def _installment_status(paid: Decimal, required: Decimal) -> str:
    """paid ≥ required → paid; 0 < paid < required → partial; else unpaid."""
    if paid <= 0:
        return "unpaid" if required > 0 else "not_required"
    return "paid" if paid >= required else "partial"


def pte_estimate(fiscal_year: int, basis: str = "auto") -> PTEEstimate:
    """Entity-level state PTET estimate for the fiscal year (owner decision D1, card Q-04).

    ``total_annual_tax`` = FI_STATE_TAX_RATE × entity taxable income (ordinary business
    income + net ST/LT capital gain + dividends + interest − investment interest, floored
    at 0).  Partners' individual estimated taxes are not included.

    ``basis="auto"`` (default) looks at which months have statements for EVERY active
    account (accounting/period_coverage.py):

    * all 12 months complete → full-year figures;
    * otherwise → income through the last fully-covered quarter (or, before Q1 is
      complete, through the last fully-covered month), annualized to project the year.
      Partially imported months are excluded so they cannot distort the estimate.

    ``basis="full_year"`` forces the old behaviour (every imported month, no annualizing).
    """
    from ledger_agent.core.accounting.period_coverage import compute_period_coverage
    from ledger_agent.core.accounting.tax_estimator import TaxEstimator, entity_taxable_income
    from ledger_agent.core.database import PTEPaymentRepo, init_db

    if basis not in ("auto", "full_year"):
        raise ValueError("basis must be 'auto' or 'full_year'")
    init_db()
    entity, periods = _entity_and_periods(fiscal_year)
    if not periods:
        raise ValueError(f"No data for fiscal year {fiscal_year}.")

    cov = compute_period_coverage(entity.id, fiscal_year)
    eff_basis = "full_year" if basis == "full_year" or cov.is_full_year else cov.basis
    if eff_basis == "insufficient_data":
        raise ValueError(
            f"No fully-covered month in {fiscal_year}: every active account needs a statement "
            "for January before an estimate can be made."
        )
    through = None if eff_basis == "full_year" else cov.through_period
    months = 12 if through is None else cov.through_month
    annualize = (Decimal(12) / Decimal(months)) if months < 12 else Decimal("1")

    f = generate_form_1065(fiscal_year, through_period=through)
    estimator = TaxEstimator(entity.name, fiscal_year)
    # Q-04 / owner decision D1: entity-level state PTET only.  Schedule K portfolio items
    # always count (they used to be dropped whenever ordinary income was positive).
    taxable_base = entity_taxable_income(
        ordinary_business_income=f.ordinary_business_income,
        net_short_term_capital_gain=f.net_short_term_capital_gain,
        net_long_term_capital_gain=f.net_long_term_capital_gain,
        dividend_income=f.dividend_income,
        interest_income=f.interest_income,
        investment_interest_expense=f.investment_interest_expense,
    )
    raw = estimator.estimate_entity_ptet(taxable_base, annualize_factor=annualize)

    recorded_rows = PTEPaymentRepo.list_for_year(entity.id, fiscal_year)
    paid_by_quarter: dict[str, Decimal] = {}
    for r in recorded_rows:
        q = str(r.get("quarter", "")).upper()
        paid_by_quarter[q] = paid_by_quarter.get(q, Decimal("0.00")) + Decimal(
            str(r.get("amount", "0"))
        )
    paid_ytd = sum(paid_by_quarter.values(), Decimal("0.00")).quantize(Decimal("0.01"))
    remaining_balance = max(raw.total_annual_tax - paid_ytd, Decimal("0.00")).quantize(
        Decimal("0.01")
    )

    notes_list = list(raw.notes) if isinstance(raw.notes, list) else ([raw.notes] if raw.notes else [])
    quarters_done = 4 if eff_basis == "full_year" else (months // 3)
    required_to_date = (raw.total_annual_tax / 4 * quarters_done).quantize(Decimal("0.01"))
    if eff_basis != "full_year":
        notes_list.insert(
            0,
            f"Basis: {eff_basis} through {through} ({months} of 12 months fully covered); "
            f"annual figures are projected ×{annualize.quantize(Decimal('0.01'))}. "
            "Import the missing statements for full-year figures.",
        )
    if paid_ytd > 0:
        notes_list.append(
            f"Recorded external PTE payments YTD: ${paid_ytd:,.2f}; "
            f"remaining estimated balance: ${remaining_balance:,.2f}"
        )
    notes_str = "; ".join(notes_list)
    return PTEEstimate(
        fiscal_year=fiscal_year,
        net_income=raw.net_income,
        total_annual_tax=raw.total_annual_tax,
        effective_rate=raw.effective_rate,
        quarterly_payment=(raw.total_annual_tax / 4).quantize(Decimal("0.01")),
        quarterly_payments=[
            {
                "quarter": p.quarter,
                "due_date": p.due_date,
                "amount": float(p.amount),
                "paid_amount": float(paid_by_quarter.get(p.quarter, Decimal("0.00"))),
                "status": _installment_status(
                    paid_by_quarter.get(p.quarter, Decimal("0.00")), p.amount
                ),
            }
            for p in raw.quarterly_payments
        ],
        paid_ytd=paid_ytd,
        remaining_balance=remaining_balance,
        notes=notes_str,
        basis=eff_basis,
        through_period=through or f"{fiscal_year}-12",
        months_covered=months,
        ytd_net_income=Decimal(taxable_base).quantize(Decimal("0.01")),
        required_to_date=required_to_date,
        missing_statements={k: v for k, v in cov.missing.items() if v},
    )


@dataclass
class CustomerSummary:
    """Customer-readable outcome summary (W27 contract).

    All surfaces (CLI, MCP, Webapp) render this single object.
    Shape follows docs/customer-summary-contract.md.
    """
    fiscal_year: int
    period_covered: str
    profit_or_loss: dict
    growth_signal: dict
    balance_sheet_health: dict
    pte_due_signal: dict
    tax_due_signal: dict
    confidence_flags: list[str]
    next_actions: list[str]
    disclaimer: str = NOT_TAX_ADVICE_DISCLAIMER


def _wash_sale_csv_present() -> bool:
    """Return True if a wash-sale adjustment CSV is reachable (env-var or default path)."""
    env_path = os.environ.get("FI_WASH_SALE_CSV", "").strip()
    if env_path and Path(env_path).exists():
        return True
    default = Path(__file__).resolve().parents[2] / "private" / "wash_sale_adjustments.csv"
    return default.exists()


def _is_year_end_period(period: str, fiscal_year: int) -> bool:
    return period == f"{fiscal_year}-12"


def _has_material_snapshot_gap(skipped: list[dict], period: str) -> bool:
    if not skipped:
        return False
    from ledger_agent.core.database import get_conn

    account_ids = [s.get("account_id") for s in skipped if s.get("account_id")]
    if not account_ids:
        return False

    qmarks = ",".join("?" * len(account_ids))
    params = [period, *account_ids]
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM transactions "
            f"WHERE statement_period = ? AND account_id IN ({qmarks})",
            params,
        ).fetchone()
    return bool(row and row[0] > 0)


def _best_snapshot_period(fiscal_year: int) -> str | None:
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


def _resolve_as_of_date(
    as_of: date | str | None,
    latest_period: str | None,
    fiscal_year: int,
) -> date | None:
    if as_of is not None:
        if hasattr(as_of, "date") and callable(as_of.date):
            return as_of.date()
        if isinstance(as_of, date):
            return as_of
        if isinstance(as_of, str) and as_of.strip():
            as_of_clean = as_of.strip()
            try:
                return date.fromisoformat(as_of_clean)
            except ValueError:
                parts = as_of_clean.split("-")
                if len(parts) == 2:
                    try:
                        y, m = int(parts[0]), int(parts[1])
                        _, last_day = calendar.monthrange(y, m)
                        return date(y, m, last_day)
                    except (ValueError, IndexError) as exc:
                        log.debug(
                            "Unable to parse as_of '%s'; falling back to env/period.",
                            as_of_clean,
                            exc_info=exc,
                        )

    env_key = "_".join(["FI", "AS", "OF", "DATE"])
    env_as_of = os.environ.get(env_key, "").strip()
    if env_as_of:
        try:
            return date.fromisoformat(env_as_of)
        except ValueError:
            parts = env_as_of.split("-")
            if len(parts) == 2:
                try:
                    y, m = int(parts[0]), int(parts[1])
                    _, last_day = calendar.monthrange(y, m)
                    return date(y, m, last_day)
                except (ValueError, IndexError) as exc:
                    log.debug(
                        "Unable to parse %s '%s'; falling back to latest_period.",
                        env_key,
                        env_as_of,
                        exc_info=exc,
                    )

    if latest_period:
        try:
            parts = latest_period.split("-")
            if len(parts) >= 2:
                y, m = int(parts[0]), int(parts[1])
                _, last_day = calendar.monthrange(y, m)
                return date(y, m, last_day)
        except (ValueError, IndexError) as exc:
            log.debug(
                "Unable to parse latest_period '%s'; falling back to fiscal year end.",
                latest_period,
                exc_info=exc,
            )

    return date(fiscal_year, 12, 31)


def _overdue_pte_quarters(pte: PTEEstimate, fiscal_year: int, as_of_date: date | None) -> list[str]:
    """Quarters whose due date is before as_of_date and that are not fully paid."""
    if pte.total_annual_tax <= 0 or as_of_date is None:
        return []
    from ledger_agent.core.accounting.tax_estimator import get_quarterly_due_date

    out = []
    for q_entry in pte.quarterly_payments:
        q = q_entry.get("quarter")
        if q_entry.get("status") in ("unpaid", "partial") and get_quarterly_due_date(
            q, fiscal_year
        ) < as_of_date:
            out.append(q)
    return out


def _compute_next_pte_due(
    pte: PTEEstimate,
    fiscal_year: int,
    as_of_date: date | None,
) -> str:
    if pte.total_annual_tax <= 0 or as_of_date is None:
        return "N/A"

    from ledger_agent.core.accounting.tax_estimator import (
        QUARTERLY_DUE_DATES,
        get_quarterly_due_date,
    )

    for q in ("Q1", "Q2", "Q3", "Q4"):
        q_entry = next((p for p in pte.quarterly_payments if p.get("quarter") == q), None)
        if q_entry and q_entry.get("status") in ("paid", "not_required"):
            continue
        due_d = get_quarterly_due_date(q, fiscal_year)
        if due_d >= as_of_date:
            if q_entry and q_entry.get("due_date"):
                return str(q_entry["due_date"])
            return QUARTERLY_DUE_DATES.get(q, "N/A")

    return "N/A"


def build_customer_summary(
    fiscal_year: int,
    as_of: date | str | None = None,
) -> CustomerSummary:
    from ledger_agent.core.database import init_db

    init_db()
    entity, periods = _entity_and_periods(fiscal_year)
    if not periods:
        raise ValueError(f"No statement data for fiscal year {fiscal_year}.")

    latest_period = periods[-1]
    best_period = _best_snapshot_period(fiscal_year) or latest_period

    from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder

    bs = BalanceSheetBuilder(entity.id, best_period, pl_periods=periods).build()
    f1065 = generate_form_1065(fiscal_year)
    pte = pte_estimate(fiscal_year)

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

    raw_skipped = bs.coverage.get("skipped_snapshots", [])
    skipped: list[dict] = []
    if raw_skipped:
        from ledger_agent.core.database import get_conn

        year_prefix = f"{fiscal_year}-%"
        acct_ids = list({s.get("account_id") for s in raw_skipped if s.get("account_id")})
        snap_counts: dict[str, int] = {}
        txn_counts: dict[str, int] = {}
        if acct_ids:
            placeholders = ",".join("?" for _ in acct_ids)
            with get_conn() as conn:
                snap_rows = conn.execute(
                    f"SELECT account_id, COUNT(*) FROM account_snapshots "
                    f"WHERE account_id IN ({placeholders}) AND statement_period LIKE ? "
                    f"GROUP BY account_id",
                    (*acct_ids, year_prefix),
                ).fetchall()
                snap_counts = {r[0]: r[1] for r in snap_rows}
                txn_rows = conn.execute(
                    f"SELECT account_id, COUNT(*) FROM transactions "
                    f"WHERE account_id IN ({placeholders}) AND statement_period LIKE ? "
                    f"GROUP BY account_id",
                    (*acct_ids, year_prefix),
                ).fetchall()
                txn_counts = {r[0]: r[1] for r in txn_rows}
        for s in raw_skipped:
            aid = s.get("account_id")
            if aid and snap_counts.get(aid, 0) == 0 and txn_counts.get(aid, 0) == 0:
                continue
            skipped.append(s)

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

    as_of_date = _resolve_as_of_date(as_of, latest_period, fiscal_year)
    if pte.total_annual_tax > 0:
        pte_status = "likely_due" if pte.remaining_balance > 0 else "paid"
        next_due = _compute_next_pte_due(pte, fiscal_year, as_of_date) if pte.remaining_balance > 0 else "N/A"
    else:
        pte_status = "not_due"
        next_due = "N/A"

    pte_due_signal = {
        "status": pte_status,
        "annual_estimate": str(pte.total_annual_tax.quantize(Decimal("0.01"))),
        "paid_ytd": str(pte.paid_ytd.quantize(Decimal("0.01"))),
        "remaining_balance": str(pte.remaining_balance.quantize(Decimal("0.01"))),
        "next_due": next_due,
        # period basis: full_year | quarter_to_date | partial_quarter
        "basis": pte.basis,
        "through_period": pte.through_period,
        "months_covered": pte.months_covered,
        "required_to_date": str(pte.required_to_date.quantize(Decimal("0.01"))),
        "overdue_quarters": _overdue_pte_quarters(pte, fiscal_year, as_of_date),
    }

    tax_status = "likely_due" if pte.total_annual_tax > 0 else "not_due"
    tax_due_signal = {
        "status": tax_status,
        "basis": "pte_estimate",
        "note": (
            f"PTE annual estimate: {float(pte.total_annual_tax):,.2f}; "
            f"effective rate: {float(pte.effective_rate):.1f}%"
        ),
    }

    # G-01: CPA-style self-audit (run once per summary).
    audit_report = audit_year(fiscal_year)
    audit_status = audit_report.get("status", "pass")
    audit_problem_checks = [
        c["name"] for c in audit_report.get("checks", [])
        if c.get("status") == audit_status and audit_status in ("warn", "fail")
    ]

    # R-03: internal-transfer legs with no counterpart in another account (read-only;
    # only the count reaches the summary — no descriptions, dates or amounts).
    unmatched_transfer_legs = reconcile_year(fiscal_year).unmatched

    flags: list[str] = []
    _wash_csv_present = _wash_sale_csv_present()
    if f1065.net_short_term_capital_gain != 0 or f1065.net_long_term_capital_gain != 0:
        if not _wash_csv_present:
            if not _is_year_end_period(latest_period, fiscal_year):
                flags.append("PROVISIONAL_W16_MIDYEAR")
                flags.append("wash_sale_not_applied")
            else:
                flags.append("NOT_CLOSE_READY_W16")
                flags.append("wash_sale_not_applied")
    if skipped:
        if _has_material_snapshot_gap(skipped, best_period):
            flags.append("NOT_CLOSE_READY_W17")
        else:
            flags.append("PROVISIONAL_W17_STALE_ACCOUNT")
    if pte.basis != "full_year":
        # Tax figures cover only fully-documented months; the year cannot be closed.
        flags.append(f"PROVISIONAL_PERIOD_{pte.basis.upper()}")
    if pte_due_signal["overdue_quarters"]:
        flags.append("PTE_OVERDUE")
    if audit_status == "fail":
        flags.append("AUDIT_FAIL")
    elif audit_status == "warn":
        flags.append("AUDIT_WARN")
    if unmatched_transfer_legs:
        flags.append("UNMATCHED_TRANSFERS")
    flags.append("CLOSE_READY_W26")
    # AUDIT_WARN / UNMATCHED_TRANSFERS (both warnings) do not block close-readiness;
    # AUDIT_FAIL always does.
    if "AUDIT_FAIL" not in flags and all(
        f.startswith("CLOSE_READY") or f in ("AUDIT_WARN", "UNMATCHED_TRANSFERS")
        for f in flags
    ):
        flags.append("CLOSE_READY")

    next_actions: list[str] = []
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
    if "PROVISIONAL_W16_MIDYEAR" in flags:
        next_actions.append(
            "Mid-year run: capital gains are provisional until year-end wash-sale details are available."
        )
    if "NOT_CLOSE_READY_W17" in flags:
        next_actions.append(
            f"Resolve W17: {len(skipped)} account(s) missing snapshots — "
            "run import with complete statements"
        )
    if "PROVISIONAL_W17_STALE_ACCOUNT" in flags:
        next_actions.append(
            f"Balance sheet uses available snapshots; {len(skipped)} stale account(s) had no activity in {best_period}."
        )
    if pte.basis != "full_year":
        n_missing = sum(len(v) for v in pte.missing_statements.values())
        next_actions.append(
            f"Tax figures are {pte.basis.replace('_', '-')} through {pte.through_period} "
            f"({pte.months_covered}/12 months complete for every account). "
            f"Import the {n_missing} missing monthly statement(s) to get full-year figures."
        )
    if pte_due_signal["overdue_quarters"]:
        next_actions.append(
            "Overdue estimated tax installment(s): "
            + ", ".join(pte_due_signal["overdue_quarters"])
            + " — not fully paid by their due date."
        )
    if audit_status == "fail":
        next_actions.append(
            "Self-audit FAILED: " + ", ".join(audit_problem_checks)
            + " — run `ledger audit " + str(fiscal_year) + "` and resolve before filing."
        )
    elif audit_status == "warn":
        next_actions.append(
            "Self-audit warnings: " + ", ".join(audit_problem_checks)
            + " — run `ledger audit " + str(fiscal_year) + "` to review."
        )
    if unmatched_transfer_legs:
        next_actions.append(
            f"{unmatched_transfer_legs} internal transfer leg(s) have no matching "
            "counterpart — check the other account's statement or whether it is a "
            "third-party payment."
        )
    if pte_status == "likely_due" and next_due != "N/A":
        next_actions.append(
            f"Estimated quarterly tax payment due: {next_due}"
        )
    if not next_actions:
        next_actions.append("Review outputs with CPA before filing Form 1065")

    return CustomerSummary(
        fiscal_year=fiscal_year,
        period_covered=f"{fiscal_year}-01..{latest_period}",
        profit_or_loss=profit_or_loss,
        growth_signal=growth_signal,
        balance_sheet_health=balance_sheet_health,
        pte_due_signal=pte_due_signal,
        tax_due_signal=tax_due_signal,
        confidence_flags=flags,
        next_actions=next_actions,
    )


def audit_year(fiscal_year: int) -> dict:
    """CPA-style self-audit of the fiscal year's figures (G-01).

    Thin wrapper over ``accounting.self_audit.run_self_audit``.  Returns
    ``{"fiscal_year", "status", "checks": [{name, status, summary, details}], "disclaimer"}``
    where ``status`` is the worst check status (``pass`` / ``warn`` / ``fail``).
    Details reference accounts by opaque id and type only.
    """
    from ledger_agent.core.accounting.self_audit import run_self_audit

    out = run_self_audit(fiscal_year).to_dict()
    out["disclaimer"] = NOT_TAX_ADVICE_DISCLAIMER
    return out


def reconcile_year(fiscal_year: int, *, persist: bool = False) -> ReconcileReport:
    """Pair the active entity's internal-transfer legs for ``fiscal_year`` (R-03).

    Only legs of the entity picked by ``_entity_and_periods`` are read (ARCH-30), and
    two legs pair only when they sit in different accounts (see ``reconciler``).

    Read-only by default, so report paths (MCP, bridge, summary, parity tests) never
    write.  ``persist=True`` stores ``is_reconciled=1`` on both legs of every match.
    """
    from ledger_agent.core.database import TransactionRepo, init_db
    from ledger_agent.core.intelligence.reconciler import matched_leg_ids, reconcile

    init_db()
    txns = _transactions_for_year(fiscal_year)
    transfers = [t for t in txns if t.is_transfer]

    if not transfers:
        return ReconcileReport(fiscal_year=fiscal_year)

    matched_list, unmatched_txns = reconcile(transfers)
    if persist and matched_list:
        TransactionRepo.mark_reconciled(matched_leg_ids(matched_list))
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
