"""
tests/unit/test_form_1065_ltcg.py  –  G-00 groups 2 and 3.

* Group 2: long-term capital gain (4011) / loss (5075) are Schedule K items only —
  they must not also flow into ordinary income / deductions (``LTCG_CODES``).
* ``generate_form_1065(through_period=)`` restricts to statements up to that month and
  skips the annual wash-sale disallowance for a partial year.
* Group 3: ``generate_balance_sheet(year, period)`` only includes P&L through ``period``.

Synthetic tmp_path DB only; pseudonymous entity / institution / ticker names.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

FY = 2026


@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    from ledger_agent.core.database import AccountRepo, EntityRepo, init_db
    from ledger_agent.core.models import Account, AccountType, Entity

    db_file = tmp_path / "ltcg.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    # Point the wash-sale overlay at a file that does not exist so only the
    # synthetic ledger is used unless a test supplies its own CSV.
    monkeypatch.setenv("FI_WASH_SALE_CSV", str(tmp_path / "absent.csv"))
    import ledger_agent.core.accounting.wash_sale as wash_sale

    monkeypatch.setattr(wash_sale, "_DEFAULT_CSV", tmp_path / "absent-default.csv")
    init_db(db_file)
    EntityRepo.upsert(
        Entity(id="ent-ltcg", name="ENTITY_A", entity_type="LLC", state="MO"), db_file
    )
    AccountRepo.upsert(
        Account(
            id="acct-ltcg",
            entity_id="ent-ltcg",
            name="Operating",
            institution="Bank X",
            account_type=AccountType.CHECKING,
            account_number_masked="0000",
        ),
        db_file,
    )
    return db_file


def _txn(db_file, tx_id: str, month: int, amount: str, code: str) -> None:
    from ledger_agent.core.database import TransactionRepo
    from ledger_agent.core.models import Transaction, TransactionType

    amt = Decimal(amount)
    TransactionRepo.bulk_insert(
        [
            Transaction(
                id=tx_id,
                account_id="acct-ltcg",
                date=date(FY, month, 10),
                description=f"Synthetic {tx_id}",
                amount=amt,
                transaction_type=TransactionType.CREDIT if amt > 0 else TransactionType.DEBIT,
                statement_period=f"{FY}-{month:02d}",
                coa_code=code,
                coa_name=code,
            )
        ],
        db_file,
    )


def _snapshot(db_file, month: int) -> None:
    from ledger_agent.core.database import SnapshotRepo
    from ledger_agent.core.models import AccountSnapshot

    SnapshotRepo.upsert(
        AccountSnapshot(
            account_id="acct-ltcg",
            statement_period=f"{FY}-{month:02d}",
            ending_balance=Decimal("100"),
        ),
        db_file,
    )


# ── Group 2: LTCG not double-counted ─────────────────────────────────────────


def test_ltcg_codes_constant():
    from ledger_agent.core.api import LTCG_CODES

    assert {"4011", "5075"} == set(LTCG_CODES)


def test_ltcg_excluded_from_ordinary_and_counted_once_in_schedule_k(ledger_db):
    import ledger_agent.core.api as api

    _txn(ledger_db, "rev", 3, "1000", "4020")    # service revenue
    _txn(ledger_db, "exp", 3, "-100", "5010")    # software
    _txn(ledger_db, "ltg", 3, "500", "4011")     # long-term gain
    _txn(ledger_db, "ltl", 3, "-200", "5075")    # long-term loss

    f = api.generate_form_1065(FY)

    assert f.total_income == Decimal("1000")
    assert f.total_deductions == Decimal("100")
    assert f.ordinary_business_income == Decimal("900")
    # 4011 + 5075 net, exactly once
    assert f.net_long_term_capital_gain == Decimal("300")


def test_ltcg_only_ledger_has_zero_ordinary_income(ledger_db):
    import ledger_agent.core.api as api

    _txn(ledger_db, "ltg", 2, "700", "4011")
    _txn(ledger_db, "ltl", 2, "-300", "5075")

    f = api.generate_form_1065(FY)

    assert f.total_income == Decimal("0")
    assert f.total_deductions == Decimal("0")
    assert f.ordinary_business_income == Decimal("0")
    assert f.net_long_term_capital_gain == Decimal("400")


def test_through_period_excludes_later_months(ledger_db):
    import ledger_agent.core.api as api

    _txn(ledger_db, "rev-mar", 3, "1000", "4020")
    _txn(ledger_db, "rev-apr", 4, "600", "4020")
    _txn(ledger_db, "ltg-apr", 4, "50", "4011")

    q1 = api.generate_form_1065(FY, through_period=f"{FY}-03")
    full = api.generate_form_1065(FY)

    assert q1.ordinary_business_income == Decimal("1000")
    assert q1.net_long_term_capital_gain == Decimal("0")
    assert full.ordinary_business_income == Decimal("1600")
    assert full.net_long_term_capital_gain == Decimal("50")


def test_through_period_outside_fiscal_year_raises(ledger_db):
    import ledger_agent.core.api as api

    _txn(ledger_db, "rev", 3, "1000", "4020")
    with pytest.raises(ValueError):
        api.generate_form_1065(FY, through_period=f"{FY - 1}-12")


def test_wash_sale_disallowance_skipped_for_partial_year(ledger_db, tmp_path, monkeypatch):
    import ledger_agent.core.api as api

    csv_file = tmp_path / "wash.csv"
    csv_file.write_text("ticker,disallowed_loss\nTICKER_SEC1,40\n", encoding="utf-8")
    monkeypatch.setenv("FI_WASH_SALE_CSV", str(csv_file))
    _txn(ledger_db, "rev", 3, "1000", "4020")

    full = api.generate_form_1065(FY)
    dec = api.generate_form_1065(FY, through_period=f"{FY}-12")
    partial = api.generate_form_1065(FY, through_period=f"{FY}-03")

    assert full.net_short_term_capital_gain == Decimal("40")
    assert dec.net_short_term_capital_gain == Decimal("40")
    assert partial.net_short_term_capital_gain == Decimal("0")


# ── Group 3: balance sheet P&L only through `period` ─────────────────────────


def test_balance_sheet_for_march_excludes_april_pl(ledger_db):
    import ledger_agent.core.api as api

    _snapshot(ledger_db, 3)
    _snapshot(ledger_db, 4)
    _txn(ledger_db, "rev-mar", 3, "1000", "4020")
    _txn(ledger_db, "rev-apr", 4, "700", "4020")

    bs_mar = api.generate_balance_sheet(FY, f"{FY}-03")
    bs_apr = api.generate_balance_sheet(FY, f"{FY}-04")
    bs_default = api.generate_balance_sheet(FY)

    assert bs_mar.net_income == Decimal("1000")
    assert bs_apr.net_income == Decimal("1700")
    assert bs_default.net_income == Decimal("1700")


def test_balance_sheet_period_outside_fiscal_year_raises(ledger_db):
    import ledger_agent.core.api as api

    _snapshot(ledger_db, 3)
    _txn(ledger_db, "rev-mar", 3, "1000", "4020")
    with pytest.raises(ValueError):
        api.generate_balance_sheet(FY, f"{FY + 1}-03")
