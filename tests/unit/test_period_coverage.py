"""
tests/unit/test_period_coverage.py  –  G-00 group 1: month coverage per account.

``accounting/period_coverage.py`` decides how far the books are complete:
``full_year`` / ``quarter_to_date`` / ``partial_quarter`` / ``insufficient_data``.
Synthetic tmp_path DB only; pseudonymous entity / institution names.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

FY = 2026
ENTITY_ID = "ent-cov-a"


@pytest.fixture
def cov_db(tmp_path, monkeypatch):
    from ledger_agent.core.database import EntityRepo, init_db
    from ledger_agent.core.models import Entity

    db_file = tmp_path / "coverage.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    init_db(db_file)
    EntityRepo.upsert(
        Entity(id=ENTITY_ID, name="ENTITY_A", entity_type="LLC", state="MO"), db_file
    )
    return db_file


def _account(db_file, acct_id: str) -> str:
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
    return acct_id


def _snapshots(db_file, acct_id: str, periods: list[str]) -> None:
    from ledger_agent.core.database import SnapshotRepo
    from ledger_agent.core.models import AccountSnapshot

    for p in periods:
        SnapshotRepo.upsert(
            AccountSnapshot(account_id=acct_id, statement_period=p, ending_balance=Decimal("10")),
            db_file,
        )


def _months(year: int, months) -> list[str]:
    return [f"{year}-{m:02d}" for m in months]


def _coverage():
    from ledger_agent.core.accounting.period_coverage import compute_period_coverage

    return compute_period_coverage(ENTITY_ID, FY)


ALL = range(1, 13)


def test_all_twelve_months_for_three_accounts_is_full_year(cov_db):
    for a in ("acct-1", "acct-2", "acct-3"):
        _snapshots(cov_db, _account(cov_db, a), _months(FY, ALL))

    cov = _coverage()
    assert cov.basis == "full_year"
    assert cov.is_full_year
    assert cov.complete_through_month == 12
    assert cov.through_period == f"{FY}-12"
    assert cov.complete_months == list(ALL)
    assert not any(cov.missing.values())


def test_one_account_missing_may_is_quarter_to_date_through_march(cov_db):
    _snapshots(cov_db, _account(cov_db, "acct-1"), _months(FY, ALL))
    _snapshots(cov_db, _account(cov_db, "acct-2"), _months(FY, ALL))
    _snapshots(cov_db, _account(cov_db, "acct-3"), _months(FY, [m for m in ALL if m != 5]))

    cov = _coverage()
    assert cov.basis == "quarter_to_date"
    assert cov.complete_through_month == 4
    assert cov.last_complete_quarter == 1
    assert cov.through_period == f"{FY}-03"
    assert cov.missing["acct-3"] == [5]
    assert 5 not in cov.complete_months
    assert cov.to_dict()["missing"] == {"acct-3": [5]}


def test_one_account_missing_february_is_partial_quarter_through_january(cov_db):
    _snapshots(cov_db, _account(cov_db, "acct-1"), _months(FY, ALL))
    _snapshots(cov_db, _account(cov_db, "acct-2"), _months(FY, ALL))
    _snapshots(cov_db, _account(cov_db, "acct-3"), _months(FY, [m for m in ALL if m != 2]))

    cov = _coverage()
    assert cov.basis == "partial_quarter"
    assert cov.complete_through_month == 1
    assert cov.last_complete_quarter == 0
    assert cov.through_period == f"{FY}-01"
    assert cov.missing["acct-3"] == [2]


def test_entity_formed_in_march_counts_january_and_february_complete(cov_db):
    # No account has any data before March and none has prior-year data.
    for a in ("acct-1", "acct-2"):
        _snapshots(cov_db, _account(cov_db, a), _months(FY, range(3, 13)))

    cov = _coverage()
    assert 1 in cov.complete_months
    assert 2 in cov.complete_months
    assert cov.basis == "full_year"
    assert cov.through_period == f"{FY}-12"


def test_entity_formed_in_march_partial_year_is_quarter_to_date(cov_db):
    _snapshots(cov_db, _account(cov_db, "acct-1"), _months(FY, range(3, 8)))  # Mar..Jul

    cov = _coverage()
    assert cov.complete_months[:2] == [1, 2]
    assert cov.complete_through_month == 7
    assert cov.basis == "quarter_to_date"
    assert cov.through_period == f"{FY}-06"


def test_prior_year_account_without_january_statement_has_january_missing(cov_db):
    _snapshots(cov_db, _account(cov_db, "acct-1"), _months(FY, ALL))
    carried = _account(cov_db, "acct-2")
    _snapshots(cov_db, carried, [f"{FY - 1}-12"])           # open last year
    _snapshots(cov_db, carried, _months(FY, range(2, 13)))  # but no January statement

    cov = _coverage()
    assert cov.missing["acct-2"] == [1]
    assert 1 not in cov.complete_months
    assert cov.complete_through_month == 0
    assert cov.basis == "insufficient_data"
    assert cov.through_period is None


def test_account_opened_mid_year_without_prior_data_is_not_a_gap(cov_db):
    # Contrast with the test above: the same Feb..Dec account with NO prior-year data
    # simply opened in February, so January is not required for it.
    _snapshots(cov_db, _account(cov_db, "acct-1"), _months(FY, ALL))
    _snapshots(cov_db, _account(cov_db, "acct-2"), _months(FY, range(2, 13)))

    cov = _coverage()
    assert cov.missing["acct-2"] == []
    assert cov.basis == "full_year"


def test_transactions_alone_count_as_coverage(cov_db):
    from ledger_agent.core.database import TransactionRepo
    from ledger_agent.core.models import Transaction, TransactionType

    acct = _account(cov_db, "acct-1")
    TransactionRepo.bulk_insert(
        [
            Transaction(
                account_id=acct,
                date=date(FY, m, 10),
                description=f"Synthetic item {m}",
                amount=Decimal("1"),
                transaction_type=TransactionType.CREDIT,
                statement_period=f"{FY}-{m:02d}",
            )
            for m in (1, 2, 3)
        ],
        cov_db,
    )

    cov = _coverage()
    assert cov.covered["acct-1"] == [1, 2, 3]
    assert cov.basis == "quarter_to_date"
    assert cov.through_period == f"{FY}-03"


def test_no_data_is_insufficient(cov_db):
    _account(cov_db, "acct-1")
    cov = _coverage()
    assert cov.basis == "insufficient_data"
    assert cov.through_period is None
