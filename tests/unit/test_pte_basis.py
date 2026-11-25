"""
tests/unit/test_pte_basis.py  –  G-00 groups 4 and 5.

* Group 4: ``pte_estimate(year, basis="auto"|"full_year")`` annualizes quarter-to-date
  income (only fully-covered months) and reports ``basis`` / ``through_period`` /
  ``months_covered`` / ``ytd_net_income`` / ``required_to_date`` / ``missing_statements``;
  installment ``status`` ∈ paid | partial | unpaid | not_required.
* Group 5: ``build_customer_summary`` exposes the period basis and ``overdue_quarters``
  in ``pte_due_signal`` and sets ``PROVISIONAL_PERIOD_<BASIS>`` / ``PTE_OVERDUE`` flags.
* Q-04 (owner decision D1): ``total_annual_tax`` is entity-level state PTET only —
  ``FI_STATE_TAX_RATE`` × (ordinary income + net ST/LT capital gain + dividends + interest
  − investment interest), floored at 0.  Capital gains count even when ordinary income
  is positive.

Synthetic tmp_path DB only; pseudonymous entity / institution names.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

FY = 2026
ENTITY_ID = "ent-pte-basis"
ACCT_ID = "acct-pte-basis"


@pytest.fixture
def pte_db(tmp_path, monkeypatch):
    from ledger_agent.core.database import EntityRepo, init_db
    from ledger_agent.core.models import Entity

    for key, val in (
        ("FI_SE_TAX_RATE", "0.153"),
        ("FI_FED_INCOME_RATE", "0.22"),
        ("FI_STATE_TAX_RATE", "0.05"),
        ("FI_QBI_DEDUCTION", "0.20"),
    ):
        monkeypatch.setenv(key, val)
    # Keep the synthetic Schedule K figures hermetic: a local (gitignored) wash-sale CSV
    # overlay must not add a disallowance to net STCG in full-year runs.
    import ledger_agent.core.accounting.wash_sale as wash_sale

    monkeypatch.setattr(wash_sale, "total_disallowed", lambda *a, **kw: Decimal("0"))
    db_file = tmp_path / "pte_basis.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    init_db(db_file)
    EntityRepo.upsert(
        Entity(id=ENTITY_ID, name="ENTITY_A", entity_type="LLC", state="MO"), db_file
    )
    _account(db_file, ACCT_ID)
    return db_file


def _account(db_file, acct_id: str) -> None:
    from ledger_agent.core.database import AccountRepo
    from ledger_agent.core.models import Account, AccountType

    AccountRepo.upsert(
        Account(
            id=acct_id,
            entity_id=ENTITY_ID,
            name=f"Operating {acct_id}",
            institution="Bank X",
            account_type=AccountType.CHECKING,
            account_number_masked="0000",
        ),
        db_file,
    )


def _month(db_file, month: int, amount: str, code: str = "4020", acct_id: str = ACCT_ID,
           year: int = FY) -> None:
    """One synthetic transaction plus a statement snapshot for ``year-month``."""
    from ledger_agent.core.database import SnapshotRepo, TransactionRepo
    from ledger_agent.core.models import AccountSnapshot, Transaction, TransactionType

    period = f"{year}-{month:02d}"
    amt = Decimal(amount)
    TransactionRepo.bulk_insert(
        [
            Transaction(
                id=f"{acct_id}-{period}",
                account_id=acct_id,
                date=date(year, month, 10),
                description=f"Synthetic {period}",
                amount=amt,
                transaction_type=TransactionType.CREDIT if amt > 0 else TransactionType.DEBIT,
                statement_period=period,
                coa_code=code,
                coa_name=code,
            )
        ],
        db_file,
    )
    SnapshotRepo.upsert(
        AccountSnapshot(account_id=acct_id, statement_period=period, ending_balance=Decimal("10")),
        db_file,
    )


def _txn(db_file, month: int, amount: str, code: str, tag: str, acct_id: str = ACCT_ID) -> None:
    """An extra synthetic transaction in ``FY-month`` (no snapshot; use with ``_month``)."""
    from ledger_agent.core.database import TransactionRepo
    from ledger_agent.core.models import Transaction, TransactionType

    period = f"{FY}-{month:02d}"
    amt = Decimal(amount)
    TransactionRepo.bulk_insert(
        [
            Transaction(
                id=f"{acct_id}-{period}-{tag}",
                account_id=acct_id,
                date=date(FY, month, 20),
                description=f"Synthetic {tag} {period}",
                amount=amt,
                transaction_type=TransactionType.CREDIT if amt > 0 else TransactionType.DEBIT,
                statement_period=period,
                coa_code=code,
                coa_name=code,
            )
        ],
        db_file,
    )


def _seed_q1_plus_april(db_file) -> None:
    for m in (1, 2, 3):
        _month(db_file, m, "1000")
    _month(db_file, 4, "5000")  # April is imported but Q2 is not complete


def _annual_ptet(taxable: str) -> Decimal:
    """Expected entity-level PTET: state rate × annual entity taxable income (Q-04)."""
    from ledger_agent.core.accounting.tax_estimator import STATE_INCOME_RATE

    return (Decimal(taxable) * STATE_INCOME_RATE).quantize(Decimal("0.01"))


# ── Group 4: period basis ────────────────────────────────────────────────────


def test_q1_plus_april_is_quarter_to_date_annualized_x4(pte_db):
    import ledger_agent.core.api as api

    _seed_q1_plus_april(pte_db)
    est = api.pte_estimate(FY)

    assert est.basis == "quarter_to_date"
    assert est.through_period == f"{FY}-03"
    assert est.months_covered == 3
    # April is excluded; Q1 income is annualized ×4
    assert est.ytd_net_income == Decimal("3000")
    assert est.net_income == Decimal("12000")
    assert est.total_annual_tax == _annual_ptet("12000")
    assert est.total_annual_tax > 0
    # one full quarter elapsed → one installment required to date
    assert est.required_to_date == (est.total_annual_tax / 4).quantize(Decimal("0.01"))
    assert est.missing_statements == {}
    assert "quarter_to_date" in est.notes


def test_full_year_basis_gives_unannualized_all_months_result(pte_db):
    import ledger_agent.core.api as api

    _seed_q1_plus_april(pte_db)
    est = api.pte_estimate(FY, basis="full_year")

    assert est.basis == "full_year"
    assert est.through_period == f"{FY}-12"
    assert est.months_covered == 12
    assert est.ytd_net_income == Decimal("8000")
    assert est.net_income == Decimal("8000")
    assert est.total_annual_tax == _annual_ptet("8000")
    assert est.required_to_date == est.total_annual_tax.quantize(Decimal("0.01"))


def test_all_twelve_months_auto_is_full_year(pte_db):
    import ledger_agent.core.api as api

    for m in range(1, 13):
        _month(pte_db, m, "1000")
    auto = api.pte_estimate(FY)
    forced = api.pte_estimate(FY, basis="full_year")

    assert auto.basis == "full_year"
    assert auto.months_covered == 12
    assert auto.net_income == Decimal("12000")
    assert auto.total_annual_tax == forced.total_annual_tax


def test_partial_quarter_annualizes_by_months(pte_db):
    import ledger_agent.core.api as api

    _month(pte_db, 1, "1000")
    _month(pte_db, 2, "1000")
    est = api.pte_estimate(FY)

    assert est.basis == "partial_quarter"
    assert est.through_period == f"{FY}-02"
    assert est.months_covered == 2
    assert est.ytd_net_income == Decimal("2000")
    assert est.net_income == Decimal("12000")  # ×6
    assert est.required_to_date == Decimal("0.00")  # no full quarter yet


def test_missing_statements_reported_and_month_excluded(pte_db):
    import ledger_agent.core.api as api

    _account(pte_db, "acct-second")
    _seed_q1_plus_april(pte_db)
    for m in (1, 2, 3):  # second account has no April statement
        _month(pte_db, m, "-10", code="5010", acct_id="acct-second")
    _month(pte_db, 5, "-10", code="5010", acct_id="acct-second")

    est = api.pte_estimate(FY)
    assert est.basis == "quarter_to_date"
    assert est.through_period == f"{FY}-03"
    # latest month with any data is May, so the first account's May is a gap too
    assert est.missing_statements == {"acct-second": [4], ACCT_ID: [5]}
    assert est.ytd_net_income == Decimal("2970")  # Q1 only: 3 x 1000 revenue - 3 x 10 cost


def test_insufficient_data_raises(pte_db):
    import ledger_agent.core.api as api

    # Account has prior-year data but no January statement → January is a gap.
    _month(pte_db, 12, "1000", year=FY - 1)
    _month(pte_db, 2, "1000")
    with pytest.raises(ValueError):
        api.pte_estimate(FY)


def test_invalid_basis_raises(pte_db):
    import ledger_agent.core.api as api

    _seed_q1_plus_april(pte_db)
    with pytest.raises(ValueError):
        api.pte_estimate(FY, basis="quarterly")


def test_installment_statuses(pte_db):
    import ledger_agent.core.api as api

    _seed_q1_plus_april(pte_db)
    est = api.pte_estimate(FY)
    q1_due = Decimal(str(est.quarterly_payments[0]["amount"]))
    assert {p["status"] for p in est.quarterly_payments} == {"unpaid"}

    api.record_pte_payment(FY, "Q1", Decimal("1"), paid_on=f"{FY}-04-01")
    api.record_pte_payment(FY, "Q2", q1_due, paid_on=f"{FY}-04-01")
    by_q = {p["quarter"]: p["status"] for p in api.pte_estimate(FY).quarterly_payments}
    assert by_q == {"Q1": "partial", "Q2": "paid", "Q3": "unpaid", "Q4": "unpaid"}


def test_no_tax_due_installments_are_not_required(pte_db):
    import ledger_agent.core.api as api

    for m in (1, 2, 3):
        _month(pte_db, m, "-100", code="5010")
    est = api.pte_estimate(FY)
    assert est.total_annual_tax == 0
    assert {p["status"] for p in est.quarterly_payments} == {"not_required"}


# ── Q-04: entity-level PTET ──────────────────────────────────────────────────


def _seed_full_year_with_schedule_k(db_file) -> None:
    """12 x 1000 ordinary revenue plus Schedule K items:
    STCG +500, LTCG +300, dividends +100, interest +50, margin interest -80."""
    for m in range(1, 13):
        _month(db_file, m, "1000")
    _txn(db_file, 6, "500", "4010", "stcg")
    _txn(db_file, 7, "300", "4011", "ltcg")
    _txn(db_file, 8, "100", "4021", "div")
    _txn(db_file, 9, "50", "4031", "int")
    _txn(db_file, 10, "-80", "5030", "margin")


def test_capital_gains_count_when_ordinary_income_positive(pte_db):
    """Old bug: Schedule K items were dropped whenever ordinary income was > 0."""
    import ledger_agent.core.api as api

    _seed_full_year_with_schedule_k(pte_db)
    f = api.generate_form_1065(FY)
    assert f.ordinary_business_income == Decimal("12000")  # sanity: K items not in ordinary

    est = api.pte_estimate(FY)
    # 12000 + 500 + 300 + 100 + 50 - 80
    assert est.basis == "full_year"
    assert est.ytd_net_income == Decimal("12870.00")
    assert est.net_income == Decimal("12870")
    assert est.total_annual_tax == _annual_ptet("12870")
    assert est.total_annual_tax > _annual_ptet("12000")


def test_ptet_is_state_rate_times_taxable_income(pte_db):
    import ledger_agent.core.api as api
    from ledger_agent.core.accounting.tax_estimator import STATE_INCOME_RATE

    _seed_full_year_with_schedule_k(pte_db)
    est = api.pte_estimate(FY)

    assert est.effective_rate == (STATE_INCOME_RATE * 100).quantize(Decimal("0.01"))
    q = (est.total_annual_tax / 4).quantize(Decimal("0.01"))
    assert est.quarterly_payment == q
    assert [p["quarter"] for p in est.quarterly_payments] == ["Q1", "Q2", "Q3", "Q4"]
    assert {Decimal(str(p["amount"])) for p in est.quarterly_payments} == {q}
    assert [p["due_date"] for p in est.quarterly_payments] == [
        "April 15", "June 15", "September 15", "January 15 (next year)",
    ]
    assert est.required_to_date == (est.total_annual_tax / 4 * 4).quantize(Decimal("0.01"))


def test_notes_say_entity_level_ptet_only(pte_db):
    import ledger_agent.core.api as api

    _seed_q1_plus_april(pte_db)
    notes = api.pte_estimate(FY).notes
    assert "Entity-level state PTET only" in notes
    assert "partners' individual estimated taxes are not included" in notes


def test_capital_loss_exceeding_ordinary_income_floors_at_zero(pte_db):
    """Positive ordinary income but a larger capital loss → taxable income 0 → no PTET."""
    import ledger_agent.core.api as api

    for m in (1, 2, 3):
        _month(pte_db, m, "1000")
    _txn(pte_db, 2, "-5000", "5070", "stloss")
    est = api.pte_estimate(FY)

    assert est.basis == "quarter_to_date"
    assert est.ytd_net_income == Decimal("0.00")
    assert est.total_annual_tax == Decimal("0")
    assert est.effective_rate == Decimal("0")
    assert est.required_to_date == Decimal("0.00")
    assert {p["status"] for p in est.quarterly_payments} == {"not_required"}


def test_gains_offset_ordinary_loss(pte_db):
    import ledger_agent.core.api as api

    for m in (1, 2, 3):
        _month(pte_db, m, "-100", code="5010")
    _txn(pte_db, 2, "1000", "4010", "stcg")
    est = api.pte_estimate(FY)

    assert est.ytd_net_income == Decimal("700.00")  # -300 ordinary + 1000 STCG
    assert est.net_income == Decimal("2800")  # x4
    assert est.total_annual_tax == _annual_ptet("2800")


def test_quarter_to_date_annualizes_schedule_k_items(pte_db):
    """Q1 capital gain is annualized ×4; an April gain (Q2 incomplete) is excluded."""
    import ledger_agent.core.api as api

    _seed_q1_plus_april(pte_db)
    _txn(pte_db, 2, "600", "4011", "ltcg")
    _txn(pte_db, 4, "9000", "4010", "stcg-april")
    est = api.pte_estimate(FY)

    assert est.basis == "quarter_to_date"
    assert est.ytd_net_income == Decimal("3600.00")  # 3 x 1000 + 600 LTCG
    assert est.net_income == Decimal("14400")
    assert est.total_annual_tax == _annual_ptet("14400")


# ── Group 5: overdue installments in the customer summary ────────────────────


def test_unpaid_q1_after_due_date_is_overdue(pte_db):
    import ledger_agent.core.api as api

    _seed_q1_plus_april(pte_db)
    s = api.build_customer_summary(FY, as_of=f"{FY}-05-01")
    sig = s.pte_due_signal

    assert sig["overdue_quarters"] == ["Q1"]
    assert "PTE_OVERDUE" in s.confidence_flags
    assert sig["basis"] == "quarter_to_date"
    assert sig["through_period"] == f"{FY}-03"
    assert sig["months_covered"] == 3
    est = api.pte_estimate(FY)
    assert Decimal(sig["required_to_date"]) == est.required_to_date
    assert "PROVISIONAL_PERIOD_QUARTER_TO_DATE" in s.confidence_flags
    assert any("Q1" in a and "Overdue" in a for a in s.next_actions)


def test_partially_paid_q1_after_due_date_is_overdue(pte_db):
    import ledger_agent.core.api as api

    _seed_q1_plus_april(pte_db)
    api.record_pte_payment(FY, "Q1", Decimal("1"), paid_on=f"{FY}-04-10")
    s = api.build_customer_summary(FY, as_of=f"{FY}-05-01")

    assert s.pte_due_signal["overdue_quarters"] == ["Q1"]
    assert "PTE_OVERDUE" in s.confidence_flags


def test_q1_not_overdue_before_due_date(pte_db):
    import ledger_agent.core.api as api

    _seed_q1_plus_april(pte_db)
    s = api.build_customer_summary(FY, as_of=f"{FY}-04-10")

    assert s.pte_due_signal["overdue_quarters"] == []
    assert "PTE_OVERDUE" not in s.confidence_flags


def test_fully_paid_q1_is_not_overdue(pte_db):
    import ledger_agent.core.api as api

    _seed_q1_plus_april(pte_db)
    q1_due = Decimal(str(api.pte_estimate(FY).quarterly_payments[0]["amount"]))
    api.record_pte_payment(FY, "Q1", q1_due, paid_on=f"{FY}-04-10")
    s = api.build_customer_summary(FY, as_of=f"{FY}-05-01")

    assert s.pte_due_signal["overdue_quarters"] == []
    assert "PTE_OVERDUE" not in s.confidence_flags


def test_full_year_summary_has_no_provisional_period_flag(pte_db):
    import ledger_agent.core.api as api

    for m in range(1, 13):
        _month(pte_db, m, "1000")
    s = api.build_customer_summary(FY, as_of=f"{FY}-03-01")

    assert s.pte_due_signal["basis"] == "full_year"
    assert s.pte_due_signal["months_covered"] == 12
    assert not any(f.startswith("PROVISIONAL_PERIOD_") for f in s.confidence_flags)
