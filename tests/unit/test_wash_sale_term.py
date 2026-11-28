"""
tests/unit/test_wash_sale_term.py  –  C-10: wash-sale disallowance follows the loss's term.

A washed loss is added back to the capital-gain line it was booked on:
  * 5075 (long-term, C-02)                 → net LTCG
  * 5070 / uncoded / tagged ``term_unknown`` → net STCG
The CSV overlay routes per row when it carries an optional ``term`` column and is
all short-term otherwise (pre-C-10 behaviour). ``total_disallowed`` stays the sum.

Synthetic tmp_path DB and CSV only; pseudonymous entity / institution / ticker names.
The default (gitignored) CSV path is redirected so a local overlay never leaks in.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

FY = 2026
ENTITY_ID = "ent-ws-term"
ACCT_ID = "acct-ws-term"


@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    import ledger_agent.core.accounting.wash_sale as wash_sale
    from ledger_agent.core.database import AccountRepo, EntityRepo, init_db
    from ledger_agent.core.models import Account, AccountType, Entity

    db_file = tmp_path / "ws_term.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    monkeypatch.setenv("FI_WASH_SALE_CSV", str(tmp_path / "absent.csv"))
    monkeypatch.setattr(wash_sale, "_DEFAULT_CSV", tmp_path / "absent-default.csv")
    init_db(db_file)
    EntityRepo.upsert(
        Entity(id=ENTITY_ID, name="ENTITY_A", entity_type="LLC", state="MO"), db_file
    )
    AccountRepo.upsert(
        Account(
            id=ACCT_ID,
            entity_id=ENTITY_ID,
            name="Brokerage",
            institution="BROKER_Y",
            account_type=AccountType.BROKERAGE,
            account_number_masked="0000",
        ),
        db_file,
    )
    return db_file


def _sell(db_file, tx_id: str, month: int, amount: str, code: str | None, tags: list[str]):
    from ledger_agent.core.database import TransactionRepo
    from ledger_agent.core.models import Transaction, TransactionType

    TransactionRepo.bulk_insert(
        [
            Transaction(
                id=tx_id,
                account_id=ACCT_ID,
                date=date(FY, month, 10),
                description=f"Synthetic sale {tx_id}",
                amount=Decimal(amount),
                transaction_type=TransactionType.SELL,
                statement_period=f"{FY}-{month:02d}",
                coa_code=code,
                coa_name=code,
                tags=tags,
            )
        ],
        db_file,
    )


def _positions(db_file, month: int, holdings: dict[str, str]) -> None:
    """Month-end holdings; ``upsert_period`` replaces the whole account/period."""
    from ledger_agent.core.database import PositionRepo
    from ledger_agent.core.models import Position

    PositionRepo.upsert_period(
        [
            Position(
                account_id=ACCT_ID,
                symbol=symbol,
                name=f"{symbol} synthetic",
                quantity=Decimal(qty),
                price_per_unit=Decimal("10"),
                market_value=Decimal(qty) * Decimal("10"),
                statement_period=f"{FY}-{month:02d}",
            )
            for symbol, qty in holdings.items()
        ],
        db_file,
    )


def _washed(db_file, month: int, *symbols: str) -> None:
    """Holdings grow in the month after the sale → computed path flags a wash sale."""
    _positions(db_file, month, dict.fromkeys(symbols, "10"))
    _positions(db_file, month + 1, dict.fromkeys(symbols, "20"))


def _st_loss(db_file, tx_id, month, amount, symbol, unknown=False):
    tags = ["short", "realised", symbol] + (["term_unknown"] if unknown else [])
    _sell(db_file, tx_id, month, amount, "5070", tags)


def _lt_loss(db_file, tx_id, month, amount, symbol):
    _sell(db_file, tx_id, month, amount, "5075", ["long", "realised", symbol])


def _csv(tmp_path, monkeypatch, text: str):
    p = tmp_path / "wash.csv"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setenv("FI_WASH_SALE_CSV", str(p))
    return p


# ── Computed path: split by term ─────────────────────────────────────────────


def test_short_term_loss_washed_adjusts_stcg_only(ledger_db):
    import ledger_agent.core.api as api
    from ledger_agent.core.accounting.wash_sale import disallowed_by_term

    _st_loss(ledger_db, "st", 3, "-120", "SECA")
    _washed(ledger_db, 3, "SECA")

    assert disallowed_by_term(entity_id=ENTITY_ID, fiscal_year=FY) == (
        Decimal("120"), Decimal("0"),
    )
    f = api.generate_form_1065(FY)
    assert f.net_short_term_capital_gain == Decimal("0")   # -120 loss + 120 add-back
    assert f.net_long_term_capital_gain == Decimal("0")


def test_long_term_loss_washed_adjusts_ltcg_only(ledger_db):
    import ledger_agent.core.api as api
    from ledger_agent.core.accounting.wash_sale import compute_from_ledger, disallowed_by_term

    _lt_loss(ledger_db, "lt", 3, "-200", "SECB")
    _sell(ledger_db, "st-gain", 3, "50", "4010", ["short", "realised", "SECC"])
    _washed(ledger_db, 3, "SECB")

    report = compute_from_ledger(ENTITY_ID, FY)
    assert [f.term for f in report.findings] == ["long"]
    assert (report.disallowed_short, report.disallowed_long) == (Decimal("0"), Decimal("200"))
    assert report.as_dict()["findings"][0]["term"] == "long"

    assert disallowed_by_term(entity_id=ENTITY_ID, fiscal_year=FY) == (
        Decimal("0"), Decimal("200"),
    )
    f = api.generate_form_1065(FY)
    assert f.net_long_term_capital_gain == Decimal("0")    # -200 loss + 200 add-back
    assert f.net_short_term_capital_gain == Decimal("50")  # untouched by the LT wash


def test_mixed_terms_each_go_to_their_own_line(ledger_db):
    import ledger_agent.core.api as api
    from ledger_agent.core.accounting.wash_sale import disallowed_by_term, total_disallowed

    _st_loss(ledger_db, "st", 3, "-120", "SECA")
    _lt_loss(ledger_db, "lt", 5, "-200", "SECB")
    _lt_loss(ledger_db, "lt-kept", 6, "-30", "SECD")   # not washed: stays a loss
    _washed(ledger_db, 3, "SECA")
    _washed(ledger_db, 5, "SECB")

    assert disallowed_by_term(entity_id=ENTITY_ID, fiscal_year=FY) == (
        Decimal("120"), Decimal("200"),
    )
    assert total_disallowed(entity_id=ENTITY_ID, fiscal_year=FY) == Decimal("320")
    f = api.generate_form_1065(FY)
    assert f.net_short_term_capital_gain == Decimal("0")
    assert f.net_long_term_capital_gain == Decimal("-30")


def test_term_unknown_loss_stays_short(ledger_db):
    import ledger_agent.core.api as api
    from ledger_agent.core.accounting.wash_sale import disallowed_by_term

    _st_loss(ledger_db, "unk", 4, "-80", "SECE", unknown=True)
    _washed(ledger_db, 4, "SECE")

    assert disallowed_by_term(entity_id=ENTITY_ID, fiscal_year=FY) == (
        Decimal("80"), Decimal("0"),
    )
    f = api.generate_form_1065(FY)
    assert f.net_short_term_capital_gain == Decimal("0")
    assert f.net_long_term_capital_gain == Decimal("0")


def test_uncoded_loss_counts_as_short(ledger_db):
    from ledger_agent.core.accounting.wash_sale import _load_loss_sales

    _sell(ledger_db, "raw", 3, "-40", None, ["SECF"])
    _lt_loss(ledger_db, "lt", 3, "-60", "SECG")
    terms = {e.symbol: e.term for e in _load_loss_sales(ENTITY_ID, FY)}
    assert terms == {"SECF": "short", "SECG": "long"}


def test_partial_year_skips_both_parts(ledger_db):
    import ledger_agent.core.api as api

    _st_loss(ledger_db, "st", 2, "-120", "SECA")
    _lt_loss(ledger_db, "lt", 2, "-200", "SECB")
    _washed(ledger_db, 2, "SECA", "SECB")

    partial = api.generate_form_1065(FY, through_period=f"{FY}-03")
    dec = api.generate_form_1065(FY, through_period=f"{FY}-12")

    assert partial.net_short_term_capital_gain == Decimal("-120")
    assert partial.net_long_term_capital_gain == Decimal("-200")
    assert dec.net_short_term_capital_gain == Decimal("0")
    assert dec.net_long_term_capital_gain == Decimal("0")


# ── CSV overlay ──────────────────────────────────────────────────────────────


def test_csv_without_term_column_is_all_short(ledger_db, tmp_path, monkeypatch):
    import ledger_agent.core.api as api
    from ledger_agent.core.accounting.wash_sale import (
        disallowed_by_term,
        load_adjustments,
        total_disallowed,
    )

    p = _csv(tmp_path, monkeypatch, "ticker,disallowed_loss\nSECA,40\nSECB,15.50\n")
    _lt_loss(ledger_db, "lt", 3, "-200", "SECB")

    assert load_adjustments(p) == {"SECA": Decimal("40"), "SECB": Decimal("15.50")}
    assert disallowed_by_term(p) == (Decimal("55.50"), Decimal("0"))
    assert total_disallowed(p) == Decimal("55.50")
    f = api.generate_form_1065(FY)
    assert f.net_short_term_capital_gain == Decimal("55.50")
    assert f.net_long_term_capital_gain == Decimal("-200")


def test_csv_with_term_column_routes_per_row(ledger_db, tmp_path, monkeypatch):
    import ledger_agent.core.api as api
    from ledger_agent.core.accounting.wash_sale import (
        disallowed_by_term,
        load_adjustments,
        load_adjustments_by_term,
        reconcile_computed_vs_csv,
        total_disallowed,
    )

    p = _csv(
        tmp_path,
        monkeypatch,
        "ticker,disallowed_loss,term\n"
        "SECA,40,short\n"
        "SECA,25,long\n"
        "SECB,10,LT\n"
        "SECC,5,\n"
        "SECD,3,unknown\n",
    )
    _sell(ledger_db, "st-gain", 3, "100", "4010", ["short", "realised", "SECX"])

    assert load_adjustments_by_term(p) == {
        ("SECA", "short"): Decimal("40"),
        ("SECA", "long"): Decimal("25"),
        ("SECB", "long"): Decimal("10"),
        ("SECC", "short"): Decimal("5"),
        ("SECD", "short"): Decimal("3"),
    }
    # Per-ticker view sums across terms (reconcile + legacy callers).
    assert load_adjustments(p)["SECA"] == Decimal("65")
    assert disallowed_by_term(p) == (Decimal("48"), Decimal("35"))
    assert total_disallowed(p) == Decimal("83")
    assert reconcile_computed_vs_csv(ENTITY_ID, FY, p)["csv_total"] == "83"

    f = api.generate_form_1065(FY)
    assert f.net_short_term_capital_gain == Decimal("148")
    assert f.net_long_term_capital_gain == Decimal("35")

    partial = api.generate_form_1065(FY, through_period=f"{FY}-06")
    assert partial.net_short_term_capital_gain == Decimal("100")
    assert partial.net_long_term_capital_gain == Decimal("0")


def test_csv_overlay_takes_precedence_over_computed(ledger_db, tmp_path, monkeypatch):
    from ledger_agent.core.accounting.wash_sale import disallowed_by_term

    _lt_loss(ledger_db, "lt", 3, "-200", "SECB")
    _washed(ledger_db, 3, "SECB")
    _csv(tmp_path, monkeypatch, "ticker,disallowed_loss\nSECB,70\n")

    assert disallowed_by_term(entity_id=ENTITY_ID, fiscal_year=FY) == (
        Decimal("70"), Decimal("0"),
    )


def test_no_source_returns_zero_pair(tmp_path, monkeypatch):
    import ledger_agent.core.accounting.wash_sale as wash_sale

    monkeypatch.setenv("FI_WASH_SALE_CSV", str(tmp_path / "absent.csv"))
    monkeypatch.setattr(wash_sale, "_DEFAULT_CSV", tmp_path / "absent-default.csv")
    assert wash_sale.disallowed_by_term() == (Decimal("0"), Decimal("0"))
    assert wash_sale.total_disallowed() == Decimal("0")
