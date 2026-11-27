"""
accounting/self_audit.py  –  CPA-style self-audit of a fiscal year's figures
============================================================================

Every check returns a ``CheckResult`` with status ``pass`` / ``warn`` / ``fail`` /
``not_applicable`` and machine-readable ``details``.  Details reference accounts by
their opaque internal id and type only — never by account number or institution
name — so the report can be passed through the MCP / bridge privacy layer safely.

Checks
------
statement_tieout       opening + Σ transactions == closing, per bank statement
account_continuity     opening(N) == closing(N-1), per account, consecutive months
period_coverage        every active account has a statement for every month
unclassified           transactions with no / suspense COA code (9999, 5999)
unknown_coa_codes      transactions coded with a code missing from the chart of accounts
transfer_matching      internal transfers pair up across the entity's own accounts
owner_draw_candidates  outgoing transfers to unlisted outside accounts (possible owner draws)
k1_allocation          partner P/L % sum to 100 % and K-1 lines sum to Form 1065
income_consistency     balance-sheet net income vs Form 1065 book income

Pure module: SQLite via ``get_conn`` + other core accounting modules only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

TOLERANCE = Decimal("0.01")
# Account types whose statement ending balance is fully explained by its transactions.
# Brokerage balances also move with market prices; credit-card parsers do not capture an
# opening balance — both are reported as not_applicable for the tie-out.
TIEOUT_TYPES = {"checking", "savings"}
SUSPENSE_CODES = {"9999", "5999"}


@dataclass
class CheckResult:
    name: str
    status: str  # pass | warn | fail | not_applicable
    summary: str
    details: list[dict] = field(default_factory=list)


@dataclass
class AuditReport:
    fiscal_year: int
    status: str = "pass"  # worst status across checks
    checks: list[CheckResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "fiscal_year": self.fiscal_year,
            "status": self.status,
            "checks": [asdict(c) for c in self.checks],
        }


_RANK = {"not_applicable": 0, "pass": 0, "warn": 1, "fail": 2}


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _next_period(p: str) -> str:
    y, m = int(p[:4]), int(p[5:7]) + 1
    if m == 13:
        y, m = y + 1, 1
    return f"{y}-{m:02d}"


def _snapshots(conn, entity_id: str, fiscal_year: int):
    return conn.execute(
        "SELECT s.account_id, a.account_type, s.statement_period, "
        "       s.beginning_balance, s.ending_balance "
        "FROM account_snapshots s JOIN accounts a ON a.id = s.account_id "
        "WHERE a.entity_id = ? AND s.statement_period LIKE ? "
        "ORDER BY s.account_id, s.statement_period",
        (entity_id, f"{fiscal_year}-%"),
    ).fetchall()


def check_statement_tieout(conn, entity_id: str, fiscal_year: int) -> CheckResult:
    breaks, checked, skipped = [], 0, 0
    for aid, typ, per, beg, end in _snapshots(conn, entity_id, fiscal_year):
        b, e = _dec(beg), _dec(end)
        if typ not in TIEOUT_TYPES or b is None or e is None:
            skipped += 1
            continue
        rows = conn.execute(
            "SELECT amount FROM transactions WHERE account_id=? AND statement_period=?",
            (aid, per),
        ).fetchall()
        total = sum((Decimal(str(r[0])) for r in rows), Decimal("0"))
        checked += 1
        diff = (b + total - e).quantize(Decimal("0.01"))
        if abs(diff) > TOLERANCE:
            breaks.append({
                "account_id": aid, "account_type": typ, "period": per,
                "unexplained": str(diff), "transaction_count": len(rows),
            })
    if not checked:
        return CheckResult("statement_tieout", "not_applicable",
                           f"No bank statements with opening balances ({skipped} skipped).")
    status = "fail" if breaks else "pass"
    return CheckResult(
        "statement_tieout", status,
        f"{checked - len(breaks)}/{checked} bank statements tie out "
        f"(opening + transactions = closing); {skipped} not checkable "
        "(brokerage / no opening balance).",
        breaks,
    )


def check_account_continuity(conn, entity_id: str, fiscal_year: int) -> CheckResult:
    breaks, gaps, checked = [], [], 0
    prev: dict[str, tuple[str, Decimal | None]] = {}
    # Seed with the prior year's December so the January opening can be verified.
    prior_dec = [
        r for r in _snapshots(conn, entity_id, fiscal_year - 1)
        if r[2] == f"{fiscal_year - 1}-12"
    ]
    for aid, _typ, per, _beg, end in prior_dec:
        prev[aid] = (per, _dec(end))
    for aid, typ, per, beg, end in _snapshots(conn, entity_id, fiscal_year):
        if aid in prev:
            pper, pend = prev[aid]
            b = _dec(beg)
            if _next_period(pper) != per:
                gaps.append({"account_id": aid, "account_type": typ,
                             "from_period": pper, "to_period": per})
            elif b is not None and pend is not None:
                checked += 1
                diff = (b - pend).quantize(Decimal("0.01"))
                if abs(diff) > TOLERANCE:
                    breaks.append({"account_id": aid, "account_type": typ, "period": per,
                                   "opening_minus_prior_closing": str(diff)})
        prev[aid] = (per, _dec(end))
    if breaks:
        status = "fail"
    elif gaps:
        status = "warn"
    elif checked:
        status = "pass"
    else:
        status = "not_applicable"
    return CheckResult(
        "account_continuity", status,
        f"{checked - len(breaks)}/{checked} month-to-month openings match the prior closing; "
        f"{len(gaps)} gap(s) where a month is missing between statements.",
        breaks + [dict(g, kind="missing_month") for g in gaps],
    )


def check_period_coverage(entity_id: str, fiscal_year: int) -> CheckResult:
    from ledger_agent.core.accounting.period_coverage import compute_period_coverage

    cov = compute_period_coverage(entity_id, fiscal_year)
    missing = [{"account_id": a, "missing_months": m} for a, m in cov.missing.items() if m]
    status = "pass" if cov.is_full_year else "warn"
    return CheckResult(
        "period_coverage", status,
        f"Basis {cov.basis}: months 1–{cov.complete_through_month} complete for every account"
        + (f"; {sum(len(d['missing_months']) for d in missing)} statement(s) missing." if missing else "."),
        missing,
    )


def check_unclassified(txns) -> CheckResult:
    items = [t for t in txns if not t.is_transfer and (not t.coa_code or t.coa_code in SUSPENSE_CODES)]
    total = sum((abs(Decimal(str(t.amount))) for t in items), Decimal("0"))
    by_code: dict[str, int] = {}
    for t in items:
        by_code[t.coa_code or "none"] = by_code.get(t.coa_code or "none", 0) + 1
    return CheckResult(
        "unclassified", "warn" if items else "pass",
        f"{len(items)} transaction(s) unclassified or in suspense (absolute total {total:.2f}).",
        [{"coa_code": k, "count": v} for k, v in sorted(by_code.items())],
    )


def check_unknown_codes(conn, txns) -> CheckResult:
    known = {r[0] for r in conn.execute("SELECT code FROM coa").fetchall()}
    unknown: dict[str, int] = {}
    for t in txns:
        if t.coa_code and t.coa_code not in known and t.coa_code not in SUSPENSE_CODES:
            unknown[t.coa_code] = unknown.get(t.coa_code, 0) + 1
    return CheckResult(
        "unknown_coa_codes", "fail" if unknown else "pass",
        f"{sum(unknown.values())} transaction(s) use codes missing from the chart of accounts.",
        [{"coa_code": k, "count": v} for k, v in sorted(unknown.items())],
    )


def check_transfer_matching(txns) -> CheckResult:
    """Pair internal-transfer legs with ``reconciler.reconcile``.

    ``reconcile`` returns ``(matches, unmatched)`` where each match is a
    ``ReconciliationMatch(outgoing, incoming, delta_days)`` NamedTuple.  It only
    considers ``TRANSFER_OUT`` / ``TRANSFER_IN`` legs that are not already
    ``is_reconciled``, and it mutates the legs it matches — so it runs on copies,
    with the reconciled flag cleared, to keep the audit read-only and complete.
    A matched pair whose two legs sit in the same account is not a transfer
    between the entity's accounts; it is counted and reported.
    """
    import dataclasses

    from ledger_agent.core.intelligence.reconciler import reconcile
    from ledger_agent.core.models import TransactionType

    transfers = [
        dataclasses.replace(t, is_reconciled=False) for t in txns if t.is_transfer
    ]
    if not transfers:
        return CheckResult("transfer_matching", "not_applicable", "No internal transfers.")
    matched, unmatched = reconcile(transfers)
    # Legs flagged is_transfer but typed debit/credit are outside reconcile()'s scope.
    untyped = sum(
        1 for t in transfers
        if t.transaction_type not in (TransactionType.TRANSFER_IN, TransactionType.TRANSFER_OUT)
    )
    same_acct = [
        m for m in matched
        if m.outgoing.account_id and m.outgoing.account_id == m.incoming.account_id
    ]
    details = [
        {"kind": "unmatched", "account_id": t.account_id, "period": t.statement_period,
         "date": str(t.date), "amount": str(t.amount)}
        for t in unmatched
    ] + [
        {"kind": "same_account_pair", "account_id": m.outgoing.account_id,
         "period": m.outgoing.statement_period, "out_date": str(m.outgoing.date),
         "in_date": str(m.incoming.date), "amount": str(abs(m.outgoing.amount))}
        for m in same_acct
    ]
    status = "warn" if unmatched or same_acct else "pass"
    return CheckResult(
        "transfer_matching", status,
        f"{len(matched) - len(same_acct)} transfer pair(s) matched across accounts, "
        f"{len(unmatched)} unmatched leg(s) "
        "(unmatched legs may be third-party payments mis-flagged as internal transfers, "
        "or the other account's statement is missing)."
        + (f" {len(same_acct)} pair(s) matched within the same account." if same_acct else "")
        + (f" {untyped} transfer-flagged leg(s) not typed transfer_in/out were not paired."
           if untyped else ""),
        details,
    )


def check_owner_draw_candidates(txns) -> CheckResult:
    """C-04 / D2: transfers out to an account outside the entity that is not on the
    private owner-draw list. The owner decides whether each is a draw (3040)."""
    from ledger_agent.core.intelligence.owner_draws import (
        OWNER_DRAW_CANDIDATE_TAG,
        account_refs,
        known_account_last4,
    )

    items = []
    own_cache: dict[str, frozenset[str]] = {}
    for t in txns:
        if OWNER_DRAW_CANDIDATE_TAG not in (t.tags or []):
            continue
        # Re-check against the entity's accounts as stored now: an account imported
        # after the transfer was parsed is an internal destination, not a candidate.
        refs = account_refs(f"{t.description} {t.raw_description}")
        if t.account_id not in own_cache:
            own_cache[t.account_id] = known_account_last4(t.account_id or None)
        if refs and all(r in own_cache[t.account_id] for r in refs):
            continue
        items.append(t)
    details = [
        {"account_id": t.account_id, "period": t.statement_period, "date": str(t.date),
         "amount": str(t.amount), "coa_code": t.coa_code}
        for t in items
    ]
    return CheckResult(
        "owner_draw_candidates", "warn" if items else "pass",
        f"{len(items)} transfer(s) out to an unlisted outside account — confirm whether "
        "each is an owner draw (add its last 4 digits to OWNER_DRAW_ACCOUNTS in the "
        "private config) or a payment.",
        details,
    )


def check_k1_allocation(fiscal_year: int, f1065) -> CheckResult:
    from ledger_agent.core import api

    partners = api._build_partners()
    pl_sum = sum((p[2] for p in partners.values()), Decimal("0"))
    cap_sum = sum((p[1] for p in partners.values()), Decimal("0"))
    details = []
    status = "pass"
    if abs(pl_sum - 1) > Decimal("0.0001") or abs(cap_sum - 1) > Decimal("0.0001"):
        status = "fail"
        details.append({"issue": "percentages_do_not_sum_to_100",
                        "profit_loss_pct_sum": str(pl_sum), "capital_pct_sum": str(cap_sum)})
    k1s = [api.generate_k1(fiscal_year, pid) for pid in partners]
    for line, total in (
        ("ordinary_income_loss", f1065.ordinary_business_income),
        ("net_stcg", f1065.net_short_term_capital_gain),
        ("net_ltcg", f1065.net_long_term_capital_gain),
        ("dividend_income", f1065.dividend_income),
        ("interest_income", f1065.interest_income),
    ):
        k_sum = sum((getattr(k, line) for k in k1s), Decimal("0"))
        diff = (k_sum - total).quantize(Decimal("0.01"))
        # per-partner rounding can leave at most 1 cent per partner
        if abs(diff) > Decimal("0.01") * max(len(k1s), 1):
            status = "fail"
            details.append({"line": line, "k1_sum_minus_1065": str(diff)})
        elif diff:
            details.append({"line": line, "rounding_cents": str(diff)})
    return CheckResult(
        "k1_allocation", status,
        f"{len(k1s)} partner K-1(s); allocation "
        + ("ties to Form 1065." if status == "pass" else "does NOT tie to Form 1065."),
        details,
    )


def check_income_consistency(bs, f1065) -> CheckResult:
    book_1065 = (
        f1065.ordinary_business_income + f1065.net_short_term_capital_gain
        + f1065.net_long_term_capital_gain + f1065.dividend_income
        + f1065.interest_income - f1065.investment_interest_expense
    )
    diff = (bs.net_income - book_1065).quantize(Decimal("0.01"))
    status = "pass" if abs(diff) <= Decimal("1.00") else "warn"
    return CheckResult(
        "income_consistency", status,
        "Balance-sheet net income "
        + ("agrees with" if status == "pass" else "differs from")
        + " Form 1065 book income (ordinary + Schedule K portfolio items).",
        [] if status == "pass" else [{"balance_sheet_minus_1065": str(diff),
                                      "note": "wash-sale adjustments, refunds and equity-draw "
                                              "codes are treated differently; review."}],
    )


def run_self_audit(fiscal_year: int) -> AuditReport:
    from ledger_agent.core import api
    from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
    from ledger_agent.core.database import get_conn, init_db

    init_db()
    entity, periods = api._entity_and_periods(fiscal_year)
    report = AuditReport(fiscal_year=fiscal_year)
    if not periods:
        report.status = "fail"
        report.checks.append(CheckResult("data_present", "fail", "No statement data for this year."))
        return report

    txns = api._transactions_for_year(fiscal_year)
    f1065 = api.generate_form_1065(fiscal_year)
    last = api._best_snapshot_period(fiscal_year) or periods[-1]
    bs = BalanceSheetBuilder(entity.id, last, pl_periods=periods).build()

    with get_conn() as conn:
        report.checks.append(check_statement_tieout(conn, entity.id, fiscal_year))
        report.checks.append(check_account_continuity(conn, entity.id, fiscal_year))
        report.checks.append(check_unknown_codes(conn, txns))
    report.checks.append(check_period_coverage(entity.id, fiscal_year))
    report.checks.append(check_unclassified(txns))
    report.checks.append(check_transfer_matching(txns))
    report.checks.append(check_owner_draw_candidates(txns))
    report.checks.append(check_k1_allocation(fiscal_year, f1065))
    report.checks.append(check_income_consistency(bs, f1065))

    worst = max((_RANK[c.status] for c in report.checks), default=0)
    report.status = {0: "pass", 1: "warn", 2: "fail"}[worst]

    try:
        from ledger_agent.core.audit import audit

        audit(
            "report.self_audit",
            entity_id=entity.id,
            fiscal_year=fiscal_year,
            status=report.status,
            checks={c.name: c.status for c in report.checks},
        )
    except Exception:  # audit trail must never break reporting
        pass
    return report
