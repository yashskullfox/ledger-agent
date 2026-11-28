"""
tests/unit/test_trade_term.py  –  C-02: long-term vs short-term realised trades.

* Broker Y states the term on each label ("Short-term gain:", "Long-term loss:").
* Broker Z states it in the trade's Code column (``LT`` / ``ST``); the plain layout
  without a Code column has no term → booked short-term + ``term_unknown`` tag.
* Long-term → 4011 / 5075 (Schedule K line 9a only); short-term → 4010 / 5070.
* ``generate_form_1065`` puts long-term trades into ``net_long_term_capital_gain`` only.
* The self-audit ``trade_term_unknown`` check counts the untermed trades.

Neither statement layout carries an acquisition date, so there is no holding-period
date arithmetic (and no 1-year boundary case) to test.

Synthetic inline text and tmp_path DBs only; pseudonymous names and figures.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ledger_agent.core.models import TransactionType
from ledger_agent.core.parsers.broker_y_brokerage import BrokerYBrokerageParser
from ledger_agent.core.parsers.broker_z import BrokerZParser, _term_from_codes

FY = 2026

_BROKER_Y_HEADER = (
    "ENTITY_A LLC\n"
    "Account Z23-123456\n"  # redaction: allow (synthetic)
    f"For Period January 1, {FY} – January 31, {FY}\n"
    "Beginning Account Value $10,000.00\n"  # redaction: allow (synthetic)
    "Ending Account Value $12,500.00\n"  # redaction: allow (synthetic)
    "Market Value of Holdings $12,500.00\n"  # redaction: allow (synthetic)
)

_BROKER_Z_HEADER = (
    "Activity Statement\n"
    "Account: U1234567\n"  # redaction: allow (synthetic)
    "Name: ENTITY_A LLC\n"
    f"Period: {FY}-01-01 to {FY}-01-31\n"
    "Net Asset Value 50,000.00\n"  # redaction: allow (synthetic)
    "Starting Cash 1,000.00\n"  # redaction: allow (synthetic)
    "Ending Cash 1,000.00\n"  # redaction: allow (synthetic)
)

_BROKER_Z_TRADES_HEAD = (
    "Trades\n"
    "Symbol Date/Time Quantity T. Price C. Price Proceeds Comm/Fee Basis Realized P/L "
    "MTM P/L Code\n"
    "Stocks\n"
    "USD\n"
)


def _sells(parsed):
    return [t for t in parsed.transactions if t.transaction_type == TransactionType.SELL]


def _parse_y(monkeypatch, body: str):
    parser = BrokerYBrokerageParser()
    monkeypatch.setattr(parser, "extract_text", MagicMock(return_value=_BROKER_Y_HEADER + body))
    return _sells(parser.parse(Path("dummy.pdf")))


def _parse_z(monkeypatch, trades: str):
    parser = BrokerZParser()
    text = _BROKER_Z_HEADER + _BROKER_Z_TRADES_HEAD + trades + "Dividends\n"
    monkeypatch.setattr(parser, "extract_text", MagicMock(return_value=text))
    return _sells(parser.parse(Path("dummy.pdf")))


# ── Broker Y: term from the gain/loss label ──────────────────────────────────

def test_broker_y_short_and_long_gain_and_loss_codes(monkeypatch):
    sells = _parse_y(monkeypatch, (
        "s 01/12  SECA COMMON STOCK You Sold  Short-term gain: $320.00\n"  # redaction: allow
        "s 01/13  SECB COMMON STOCK You Sold  Short-term loss: $40.00\n"  # redaction: allow
        "s 01/14  SECC COMMON STOCK You Sold  Long-term gain: $500.00\n"  # redaction: allow
        "s 01/15  SECD COMMON STOCK You Sold  Long-term loss: $150.00\n"  # redaction: allow
    ))
    got = {t.tags[2]: (t.coa_code, t.amount, t.tags) for t in sells}
    assert got["SECA"] == ("4010", Decimal("320.00"), ["short", "realised", "SECA"])
    assert got["SECB"] == ("5070", Decimal("-40.00"), ["short", "realised", "SECB"])
    assert got["SECC"] == ("4011", Decimal("500.00"), ["long", "realised", "SECC"])
    assert got["SECD"] == ("5075", Decimal("-150.00"), ["long", "realised", "SECD"])
    names = {t.coa_code: t.coa_name for t in sells}
    assert names["4011"] == "Long-Term Capital Gain"
    assert names["5075"] == "Long-Term Capital Loss"


def test_broker_y_label_on_continuation_line(monkeypatch):
    sells = _parse_y(monkeypatch, (
        "s 01/20  SECE COMMON STOCK You Sold\n"
        "   Long-term gain: $75.00\n"  # redaction: allow
    ))
    assert [(t.coa_code, t.amount) for t in sells] == [("4011", Decimal("75.00"))]


def test_broker_y_mixed_term_sale_books_each_term_separately(monkeypatch):
    """A sale of lots held both <1y and >1y must not land entirely in one term."""
    sells = _parse_y(monkeypatch, (
        "s 01/21  SECF COMMON STOCK You Sold  Short-term loss: $30.00  "  # redaction: allow
        "Long-term gain: $90.00\n"  # redaction: allow
    ))
    assert sorted((t.coa_code, t.amount) for t in sells) == [
        ("4011", Decimal("90.00")), ("5070", Decimal("-30.00")),
    ]
    assert not any("term_unknown" in t.tags for t in sells)


def test_broker_y_trade_terms():
    trades = BrokerYBrokerageParser()._parse_trades(
        "s 01/12  SECA COMMON STOCK You Sold  Short-term gain: $10.00\n"  # redaction: allow
        "s 01/14  SECC COMMON STOCK You Sold  Long-term loss: $20.00\n",  # redaction: allow
        f"{FY}-01", FY,
    )
    assert [(t.term, t.gain_loss) for t in trades] == [
        ("short", Decimal("10.00")), ("long", Decimal("-20.00")),
    ]


# ── Broker Z: term from the Code column ──────────────────────────────────────

@pytest.mark.parametrize(("codes", "term"), [
    ("C;LT", "long"), ("LT;C", "long"), ("C;ST", "short"), ("ST", "short"),
    ("C", "unknown"), ("O", "unknown"), (None, "unknown"), ("", "unknown"),
    ("C;LT;ST", "unknown"),  # mixed lots on one row cannot be split
    ("C;LTX", "unknown"),    # token match only, never a substring
])
def test_term_from_codes(codes, term):
    assert _term_from_codes(codes) == term


def test_broker_z_stock_rows_use_code_column(monkeypatch):
    sells = _parse_z(monkeypatch, (
        f"{FY}-01-12,\n"
        "SECA -100 20.0000 20.0000 2,000.00 -1.00 -1,500.00 499.00 0.00 C;ST\n"  # redaction: allow
        f"{FY}-01-13,\n"
        "SECB -100 20.0000 20.0000 2,000.00 -1.00 -2,100.00 -101.00 0.00 C;ST\n"  # redaction: allow
        f"{FY}-01-14,\n"
        "SECC -100 20.0000 20.0000 2,000.00 -1.00 -1,000.00 999.00 0.00 C;LT\n"  # redaction: allow
        f"{FY}-01-15,\n"
        "SECD -100 20.0000 20.0000 2,000.00 -1.00 -2,300.00 -301.00 0.00 C;LT\n"  # redaction: allow
        f"{FY}-01-16,\n"
        "SECE -100 20.0000 20.0000 2,000.00 -1.00 -1,900.00 99.00 0.00 C\n"  # redaction: allow
    ))
    got = {t.tags[2]: (t.coa_code, t.amount, t.tags) for t in sells}
    assert got["SECA"] == ("4010", Decimal("499.00"), ["short", "realised", "SECA"])
    assert got["SECB"] == ("5070", Decimal("-101.00"), ["short", "realised", "SECB"])
    assert got["SECC"] == ("4011", Decimal("999.00"), ["long", "realised", "SECC"])
    assert got["SECD"] == ("5075", Decimal("-301.00"), ["long", "realised", "SECD"])
    # no LT/ST code → short-term coding + term_unknown tag
    assert got["SECE"] == (
        "4010", Decimal("99.00"), ["short", "realised", "SECE", "term_unknown"],
    )


def test_broker_z_option_total_uses_detail_row_codes(monkeypatch):
    sells = _parse_z(monkeypatch, (
        "Equity and Index Options\n"
        "USD\n"
        f"{FY}-01-05,\n"
        "OPTA 20FEB26 18 P -2 1.2500 1.2000 250.00 -7.00 0.00 243.00 10.00 C;LT\n"  # redaction: allow
        "13:51:45\n"
        "Total OPTA 20FEB26 18 P 0 250.00 -7.00 0.00 243.00 10.00\n"  # redaction: allow
        f"{FY}-01-06,\n"
        "OPTB 20MAR26 17 C 5 0.5000 0.4000 -250.00 -3.00 0.00 -60.00 5.00 C;ST\n"  # redaction: allow
        "Total OPTB 20MAR26 17 C 0 -250.00 -3.00 0.00 -60.00 5.00\n"  # redaction: allow
        f"{FY}-01-07,\n"
        "OPTC 20MAR26 17 C 5 0.5000 0.4000 -250.00 -3.00 0.00 -60.00 5.00 C\n"  # redaction: allow
        "Total OPTC 20MAR26 17 C 0 250.00 -3.00 0.00 40.00 5.00\n"  # redaction: allow
    ))
    got = {t.tags[2]: (t.coa_code, t.amount, t.tags[-1]) for t in sells}
    assert got["OPTA 20FEB26 18 P"] == ("4011", Decimal("243.00"), "OPTA 20FEB26 18 P")
    assert got["OPTB 20MAR26 17 C"] == ("5070", Decimal("-60.00"), "OPTB 20MAR26 17 C")
    # codes from OPTB's group must not leak into OPTC's
    assert got["OPTC 20MAR26 17 C"] == ("4010", Decimal("40.00"), "term_unknown")


def test_broker_z_layout_without_code_column_is_term_unknown(monkeypatch):
    parser = BrokerZParser()
    text = (
        _BROKER_Z_HEADER
        + "Trades\n"
        "Symbol  Date/Time             Quantity  Price    Proceeds   P/L\n"
        f"SECA    {FY}-01-10 09:30:00   1000      11.50    11500.00   -250.00\n"  # redaction: allow
        "Cash Report\n"
    )
    monkeypatch.setattr(parser, "extract_text", MagicMock(return_value=text))
    sells = _sells(parser.parse(Path("dummy.pdf")))
    assert [(t.coa_code, t.tags) for t in sells] == [
        ("5070", ["short", "realised", "SECA", "term_unknown"]),
    ]


# ── Form 1065: long-term only on Schedule K, once ────────────────────────────

@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    from ledger_agent.core.database import AccountRepo, EntityRepo, init_db
    from ledger_agent.core.models import Account, AccountType, Entity

    db_file = tmp_path / "term.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    monkeypatch.setenv("FI_WASH_SALE_CSV", str(tmp_path / "absent.csv"))
    # C-10: a local (gitignored) default CSV must not reach the Schedule K lines.
    import ledger_agent.core.accounting.wash_sale as wash_sale

    monkeypatch.setattr(wash_sale, "_DEFAULT_CSV", tmp_path / "absent-default.csv")
    init_db(db_file)
    EntityRepo.upsert(Entity(id="ent-term", name="ENTITY_A", entity_type="LLC", state="MO"),
                      db_file)
    AccountRepo.upsert(Account(
        id="acct-term", entity_id="ent-term", name="Brokerage", institution="BROKER_Y",
        account_type=AccountType.BROKERAGE, account_number_masked="0000",
    ), db_file)
    return db_file


def test_form_1065_long_term_trades_only_in_net_ltcg(ledger_db, monkeypatch):
    import ledger_agent.core.api as api
    from ledger_agent.core.database import TransactionRepo

    sells = _parse_y(monkeypatch, (
        "s 01/12  SECA COMMON STOCK You Sold  Short-term gain: $300.00\n"  # redaction: allow
        "s 01/13  SECB COMMON STOCK You Sold  Short-term loss: $50.00\n"  # redaction: allow
        "s 01/14  SECC COMMON STOCK You Sold  Long-term gain: $800.00\n"  # redaction: allow
        "s 01/15  SECD COMMON STOCK You Sold  Long-term loss: $200.00\n"  # redaction: allow
    ))
    for t in sells:
        t.account_id = "acct-term"
    TransactionRepo.bulk_insert(sells, ledger_db)

    f = api.generate_form_1065(FY)

    assert f.net_long_term_capital_gain == Decimal("600.00")
    assert f.net_short_term_capital_gain == Decimal("250.00")
    assert f.total_income == Decimal("0")
    assert f.total_deductions == Decimal("0")
    assert f.ordinary_business_income == Decimal("0")


def test_wash_sale_still_finds_ticker_on_term_unknown_loss(ledger_db, monkeypatch):
    """wash_sale._load_loss_sales takes the last upper-case tag as the ticker; the
    lower-case ``term_unknown`` tag appended after the symbol must not hide it."""
    from ledger_agent.core.accounting.wash_sale import _load_loss_sales
    from ledger_agent.core.database import TransactionRepo

    parser = BrokerZParser()
    text = (
        _BROKER_Z_HEADER + _BROKER_Z_TRADES_HEAD
        + f"{FY}-01-16,\n"
        "SECE -100 20.0000 20.0000 2,000.00 -1.00 -2,100.00 -101.00 0.00 C\n"  # redaction: allow
        "Dividends\n"
    )
    monkeypatch.setattr(parser, "extract_text", MagicMock(return_value=text))
    sells = _sells(parser.parse(Path("dummy.pdf")))
    assert sells[0].tags[-1] == "term_unknown"
    for t in sells:
        t.account_id = "acct-term"
    TransactionRepo.bulk_insert(sells, ledger_db)

    events = _load_loss_sales("ent-term", FY)
    assert [(e.symbol, e.loss_amount) for e in events] == [("SECE", Decimal("101.00"))]


# ── Self-audit: trade_term_unknown ───────────────────────────────────────────

def _txn(code, tags):
    from ledger_agent.core.models import Transaction

    return Transaction(
        account_id="acct-term", date=date(FY, 1, 10), description="Synthetic sale",
        amount=Decimal("10"), transaction_type=TransactionType.SELL,
        statement_period=f"{FY}-01", coa_code=code, tags=tags,
    )


def test_self_audit_counts_term_unknown_trades():
    from ledger_agent.core.accounting.self_audit import check_trade_term_unknown

    txns = [
        _txn("4010", ["short", "realised", "SECA", "term_unknown"]),
        _txn("5070", ["short", "realised", "SECB", "term_unknown"]),
        _txn("4010", ["short", "realised", "SECC", "term_unknown"]),
        _txn("4011", ["long", "realised", "SECD"]),
        _txn("4010", ["short", "realised", "SECE"]),
    ]
    r = check_trade_term_unknown(txns)
    assert r.name == "trade_term_unknown"
    assert r.status == "warn"
    assert r.summary.startswith("3 realised trade(s)")
    assert r.details == [{"coa_code": "4010", "count": 2}, {"coa_code": "5070", "count": 1}]


def test_self_audit_term_check_passes_when_all_terms_known():
    from ledger_agent.core.accounting.self_audit import check_trade_term_unknown

    r = check_trade_term_unknown([_txn("4011", ["long", "realised", "SECD"]), _txn("4020", [])])
    assert r.status == "pass" and r.details == []


def test_audit_year_reports_term_unknown_from_parsed_statement(ledger_db, monkeypatch):
    import ledger_agent.core.api as api
    from ledger_agent.core.database import TransactionRepo

    parser = BrokerZParser()
    text = (
        _BROKER_Z_HEADER + _BROKER_Z_TRADES_HEAD
        + f"{FY}-01-16,\n"
        "SECE -100 20.0000 20.0000 2,000.00 -1.00 -1,900.00 99.00 0.00 C\n"  # redaction: allow
        "Dividends\n"
    )
    monkeypatch.setattr(parser, "extract_text", MagicMock(return_value=text))
    sells = _sells(parser.parse(Path("dummy.pdf")))
    for t in sells:
        t.account_id = "acct-term"
    TransactionRepo.bulk_insert(sells, ledger_db)

    rep = api.audit_year(FY)
    check = next(c for c in rep["checks"] if c["name"] == "trade_term_unknown")
    assert check["status"] == "warn"
    assert check["details"] == [{"coa_code": "4010", "count": 1}]
