"""
tests/unit/test_legacy_mcp_isolation.py  –  card R-08 (ARCH-30 entity isolation)

The legacy ``mcp_server/server.py`` must answer every request for ONE entity, even
when another entity has accounts, transactions, snapshots and positions in the SAME
statement period:

* target entity = the optional ``entity_id`` argument (id or exact name), else the
  rule of ``api._entity_and_periods`` (most transactions in the fiscal year; tools
  without a period use the fiscal year of the latest transaction period);
* ``list_transactions`` (period, unclassified and most-recent paths), the balance
  sheet, ``list_periods`` and ``get_entity_summary`` carry only that entity's data;
* ``get_tax_estimate`` shows ``api.pte_estimate`` figures only for the fiscal year's
  PTE entity, and a fixed "not available" message (no figures) for any other;
* an unknown entity argument gets a fixed error that does not echo the value;
* P-02 privacy is unchanged: names tokenised, responses pass ``audit_egress``.

Synthetic tmp_path databases and pseudonymous names only; no real data.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from ledger_agent.core import config as _cfg
from ledger_agent.core import privacy

FY = 2026
PERIOD = f"{FY}-01"

ENT_A = "ent-r08-a"
ENT_B = "ent-r08-b"

# Descriptions are lower-case so they survive response redaction unchanged and can be
# matched (upper-case words become <COUNTERPARTY_n> tokens).  A has more transactions,
# so it is the fiscal year's default / PTE entity; B has snapshots for fewer months.
SEED: dict[str, dict[str, Any]] = {
    ENT_A: {
        "name": "ENTITY_A HOLDINGS LLC",
        "state": "MO",
        "chk": "acct-r08-a-chk",
        "brk": "acct-r08-a-brk",
        "institution": "BANK_X",
        "broker": "BROKER_Y",
        "txns": [
            ("alpha consulting invoice", "4020", "5100.00"),
            ("alpha software plan", "5010", "-61.00"),
            ("alpha postage", "5061", "-23.00"),
        ],
        "chk_balance": "2400",
        "brk_balance": "1500",
        "position": ("TICKER_SEC1", "10", "150", "1500"),
        "months": 12,
    },
    ENT_B: {
        "name": "ENTITY_B VENTURES LLC",
        "state": "CA",
        "chk": "acct-r08-b-chk",
        "brk": "acct-r08-b-brk",
        "institution": "BANK_X2",
        "broker": "BROKER_Z",
        "txns": [
            ("bravo design retainer", "4020", "9300.00"),
            ("bravo monthly hosting", "5010", "-77.00"),
        ],
        "chk_balance": "8800",
        "brk_balance": "2700",
        "position": ("TICKER_SEC2", "30", "90", "2700"),
        "months": 6,
    },
}
ALL_NAMES = tuple(SEED[e][k] for e in SEED for k in ("name", "institution", "broker"))


@pytest.fixture(autouse=True)
def _privacy_env(monkeypatch):
    monkeypatch.setattr(_cfg, "AI_EGRESS_MODE", "redact")
    monkeypatch.setattr(_cfg, "PRIVACY_ENTITY_NAME", "")
    monkeypatch.delenv("FI_PRIVACY_ENTITY_NAME", raising=False)
    privacy._reset_session()
    yield
    privacy._reset_session()


def _seed(db_file, *, classified: bool = True) -> None:
    from ledger_agent.core.database import (
        AccountRepo,
        EntityRepo,
        PositionRepo,
        SnapshotRepo,
        TransactionRepo,
        init_db,
    )
    from ledger_agent.core.models import (
        Account,
        AccountSnapshot,
        AccountType,
        Entity,
        Position,
        Transaction,
        TransactionType,
    )

    init_db(db_file)
    for eid, s in SEED.items():
        EntityRepo.upsert(
            Entity(id=eid, name=s["name"], entity_type="LLC", state=s["state"]), db_file
        )
        AccountRepo.upsert(Account(
            id=s["chk"], entity_id=eid, name=f"{s['name']} OPERATING",
            institution=s["institution"], account_type=AccountType.CHECKING,
            account_number_masked="0000",
        ), db_file)
        AccountRepo.upsert(Account(
            id=s["brk"], entity_id=eid, name=f"{s['broker']} Margin",
            institution=s["broker"], account_type=AccountType.BROKERAGE,
            account_number_masked="0000",
        ), db_file)
        TransactionRepo.bulk_insert([
            Transaction(
                id=f"txn-{eid}-{i}", account_id=s["chk"], date=date(FY, 1, 5 + i),
                description=desc, raw_description=desc, amount=Decimal(amt),
                transaction_type=TransactionType.CREDIT if Decimal(amt) > 0
                else TransactionType.DEBIT,
                statement_period=PERIOD,
                coa_code=code if classified else "",
                coa_name="synthetic" if classified else "",
            )
            for i, (desc, code, amt) in enumerate(s["txns"])
        ], db_file)
        for month in range(1, s["months"] + 1):
            for acct, bal in ((s["chk"], s["chk_balance"]), (s["brk"], s["brk_balance"])):
                SnapshotRepo.upsert(AccountSnapshot(
                    account_id=acct, statement_period=f"{FY}-{month:02d}",
                    ending_balance=Decimal(bal),
                ), db_file)
        sym, qty, price, mv = s["position"]
        PositionRepo.upsert_period([Position(
            account_id=s["brk"], symbol=sym, name=sym, quantity=Decimal(qty),
            price_per_unit=Decimal(price), market_value=Decimal(mv),
            statement_period=PERIOD,
        )], db_file)


@pytest.fixture
def two_entity_db(tmp_path, monkeypatch):
    db_file = tmp_path / "r08_two.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    _seed(db_file)
    return db_file


@pytest.fixture
def two_entity_db_unclassified(tmp_path, monkeypatch):
    db_file = tmp_path / "r08_two_unclassified.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    _seed(db_file, classified=False)
    return db_file


def _other(eid: str) -> str:
    return ENT_B if eid == ENT_A else ENT_A


def _args_for(eid: str | None, **extra) -> dict[str, Any]:
    """No entity argument for the default (A); explicit ``entity_id`` otherwise."""
    return dict(extra) if eid is None else {**extra, "entity_id": eid}


def _call(capsys, tool: str, args: dict | None = None) -> tuple[dict, dict | None]:
    from mcp_server import server as legacy

    capsys.readouterr()
    legacy._dispatch({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                      "params": {"name": tool, "arguments": args or {}}})
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1, "legacy server must write exactly one JSON-RPC frame"
    resp = json.loads(lines[0])
    result = resp.get("result") or {}
    if "error" in resp or result.get("isError"):
        return resp, None
    return resp, json.loads(result["content"][0]["text"])


def _assert_private(resp: dict, data: dict | None) -> None:
    text = json.dumps(resp, ensure_ascii=False)
    if data is not None:
        text += "\n" + json.dumps(data, ensure_ascii=False)
    folded = text.casefold()
    hits = sum(1 for n in ALL_NAMES if n.casefold() in folded)
    assert hits == 0, f"{hits} identifying value(s) present in legacy response"
    if data is not None:
        privacy.audit_egress(data)  # does not raise


def _amounts(seq) -> list[float]:
    return sorted(float(x) for x in seq)


# Default (no argument) → A; explicit entity_id → that entity.
TARGETS = [(None, ENT_A), (ENT_A, ENT_A), (ENT_B, ENT_B)]
TARGET_IDS = ["default", "explicit_a", "explicit_b"]


# ── 0. Non-vacuity ────────────────────────────────────────────────────────────

def test_seed_really_mixes_entities_in_one_period(two_entity_db):
    from ledger_agent.core import api
    from ledger_agent.core.database import TransactionRepo

    rows = TransactionRepo.list_for_period(PERIOD)
    assert len(rows) == len(SEED[ENT_A]["txns"]) + len(SEED[ENT_B]["txns"])
    assert api._entity_and_periods(FY)[0].id == ENT_A


# ── 1. list_transactions ─────────────────────────────────────────────────────

def _assert_only_own_transactions(data: dict, eid: str) -> None:
    own, other = SEED[eid], SEED[_other(eid)]
    recs = data["transactions"]
    assert data["count"] == len(recs) == len(own["txns"])
    assert {r["description"] for r in recs} == {d for d, _, _ in own["txns"]}
    assert _amounts(r["amount"] for r in recs) == _amounts(a for _, _, a in own["txns"])
    text = json.dumps(data, ensure_ascii=False)
    for desc, _, _ in other["txns"]:
        assert desc not in text


@pytest.mark.parametrize("arg, eid", TARGETS, ids=TARGET_IDS)
def test_list_transactions_period_only_target_entity(two_entity_db, capsys, arg, eid):
    resp, data = _call(capsys, "list_transactions", _args_for(arg, period=PERIOD))
    assert data is not None
    _assert_only_own_transactions(data, eid)
    _assert_private(resp, data)


@pytest.mark.parametrize("arg, eid", TARGETS, ids=TARGET_IDS)
def test_list_transactions_recent_path_only_target_entity(two_entity_db, capsys, arg, eid):
    """No period, everything classified → the most-recent fallback (was raw SELECT *)."""
    resp, data = _call(capsys, "list_transactions", _args_for(arg))
    assert data is not None
    _assert_only_own_transactions(data, eid)
    _assert_private(resp, data)


@pytest.mark.parametrize("arg, eid", TARGETS, ids=TARGET_IDS)
def test_list_transactions_unclassified_path_only_target_entity(
    two_entity_db_unclassified, capsys, arg, eid
):
    resp, data = _call(capsys, "list_transactions", _args_for(arg))
    assert data is not None
    _assert_only_own_transactions(data, eid)
    assert all(r["coa_code"] == "" for r in data["transactions"])
    _assert_private(resp, data)


def test_list_transactions_recent_path_limit_newest_first(two_entity_db, capsys):
    _resp, data = _call(capsys, "list_transactions", {"limit": 2})
    assert data is not None and data["count"] == 2
    newest = SEED[ENT_A]["txns"][-2:]  # seeded on consecutive dates, oldest first
    assert {r["description"] for r in data["transactions"]} == {d for d, _, _ in newest}


# ── 2. Balance sheet, periods, entity summary ─────────────────────────────────

@pytest.mark.parametrize("arg, eid", TARGETS, ids=TARGET_IDS)
def test_balance_sheet_only_target_entity(two_entity_db, capsys, arg, eid):
    from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder

    resp, data = _call(capsys, "get_balance_sheet", _args_for(arg, period=PERIOD))
    assert data is not None
    own = SEED[eid]
    expected_assets = float(own["chk_balance"]) + float(own["position"][3])
    assert data["total_assets"] == pytest.approx(expected_assets)
    bs = BalanceSheetBuilder(eid, PERIOD).build()
    assert data["total_assets"] == float(bs.total_assets)
    assert data["net_income"] == float(bs.net_income)
    assert data["entity"] == "<ENTITY>"
    _assert_private(resp, data)


@pytest.mark.parametrize("arg, eid", TARGETS, ids=TARGET_IDS)
def test_list_periods_only_target_entity(two_entity_db, capsys, arg, eid):
    resp, data = _call(capsys, "list_periods", _args_for(arg))
    assert data is not None
    months = SEED[eid]["months"]
    assert data["periods"] == [f"{FY}-{m:02d}" for m in range(months, 0, -1)]
    assert data["entity"] == "<ENTITY>"
    _assert_private(resp, data)


@pytest.mark.parametrize("arg, eid", TARGETS, ids=TARGET_IDS)
def test_entity_summary_only_target_entity(two_entity_db, capsys, arg, eid):
    resp, data = _call(capsys, "get_entity_summary", _args_for(arg))
    assert data is not None
    assert data["entity"]["name"] == "<ENTITY>"
    assert data["entity"]["state"] == SEED[eid]["state"]
    assert data["period_count"] == SEED[eid]["months"]
    assert [a["name"] for a in data["accounts"]] == ["<ACCOUNT_1>", "<ACCOUNT_2>"]
    _assert_private(resp, data)


# ── 3. Tax estimate ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("arg", [None, ENT_A], ids=["default", "explicit_a"])
def test_tax_estimate_for_pte_entity_matches_api(two_entity_db, capsys, arg):
    from ledger_agent.core import api

    resp, data = _call(capsys, "get_tax_estimate", _args_for(arg, period=PERIOD))
    assert data is not None and "error" not in data
    est = api.pte_estimate(FY)
    assert data["total_annual_tax"] == float(est.total_annual_tax)
    assert data["net_income_annual"] == float(est.net_income)
    assert data["total_annual_tax"] > 0  # non-vacuous: A's revenue was seeded
    _assert_private(resp, data)


def test_tax_estimate_for_other_entity_is_fixed_message(two_entity_db, capsys):
    from mcp_server import server as legacy

    resp, data = _call(capsys, "get_tax_estimate", {"period": PERIOD, "entity_id": ENT_B})
    assert data is not None
    assert data["error"] == legacy.TAX_NOT_AVAILABLE
    assert set(data) == {"entity", "period", "year", "error"}  # no figures at all
    assert data["entity"] == "<ENTITY>"
    _assert_private(resp, data)


# ── 4. Entity resolution rule ─────────────────────────────────────────────────

def test_default_entity_follows_api_rule_deterministically(two_entity_db):
    from ledger_agent.core import api
    from mcp_server import server as legacy

    expected = api._entity_and_periods(FY)[0].id
    for args in ({}, {"period": PERIOD}):
        picks = {legacy._resolve_entity(args, legacy._year_of(args.get("period")))
                 .id for _ in range(3)}
        assert picks == {expected}


def test_no_period_tools_use_latest_years_most_active_entity(two_entity_db, capsys):
    """B is the only entity with data in the latest fiscal year → it is the default."""
    from ledger_agent.core.database import TransactionRepo
    from ledger_agent.core.models import Transaction, TransactionType

    TransactionRepo.bulk_insert([Transaction(
        id="txn-r08-b-next", account_id=SEED[ENT_B]["chk"], date=date(FY + 1, 1, 3),
        description="bravo next year fee", raw_description="bravo next year fee",
        amount=Decimal("-15.00"), transaction_type=TransactionType.DEBIT,
        statement_period=f"{FY + 1}-01", coa_code="5010", coa_name="synthetic",
    )])
    _resp, summary = _call(capsys, "get_entity_summary")
    assert summary is not None and summary["entity"]["state"] == SEED[ENT_B]["state"]
    _resp, periods = _call(capsys, "list_periods")
    assert periods is not None and len(periods["periods"]) == SEED[ENT_B]["months"]
    # A period in FY still resolves to FY's most active entity (A).
    _resp, data = _call(capsys, "list_transactions", {"period": PERIOD})
    assert data is not None
    _assert_only_own_transactions(data, ENT_A)


def test_entity_selectable_by_exact_name_case_insensitive(two_entity_db, capsys):
    resp, data = _call(capsys, "list_transactions",
                       {"period": PERIOD, "entity_id": SEED[ENT_B]["name"].lower()})
    assert data is not None
    _assert_only_own_transactions(data, ENT_B)
    _assert_private(resp, data)


def test_entity_alias_argument_is_honoured(two_entity_db, capsys):
    _resp, data = _call(capsys, "list_transactions", {"period": PERIOD, "entity": ENT_B})
    assert data is not None
    _assert_only_own_transactions(data, ENT_B)


@pytest.mark.parametrize("tool, extra", [
    ("get_balance_sheet", {"period": PERIOD}),
    ("list_transactions", {"period": PERIOD}),
    ("list_transactions", {}),
    ("get_tax_estimate", {"period": PERIOD}),
    ("list_periods", {}),
    ("get_entity_summary", {}),
])
def test_unknown_entity_is_fixed_error_without_echo(two_entity_db, capsys, tool, extra):
    from mcp_server import server as legacy

    probe = "nonexistent-entity-zq"
    resp, data = _call(capsys, tool, {**extra, "entity_id": probe})
    assert data is not None and data["error"] == legacy.UNKNOWN_ENTITY
    assert probe not in json.dumps(resp)
    assert "transactions" not in data and "total_assets" not in data
    _assert_private(resp, data)


def test_tools_list_entity_id_optional(capsys):
    from mcp_server import server as legacy

    by_name = {t["name"]: t["inputSchema"] for t in legacy.TOOLS}
    for name in ("get_balance_sheet", "list_transactions", "get_tax_estimate",
                 "list_periods", "get_entity_summary"):
        assert "entity_id" in by_name[name]["properties"]
        assert "entity_id" not in by_name[name].get("required", [])
    assert "entity_id" not in by_name["classify_transaction"]["properties"]


# ── 5. Entity-scoped repo reads used by the server ────────────────────────────

def test_repo_list_unclassified_scoped_by_accounts(two_entity_db_unclassified):
    from ledger_agent.core.database import TransactionRepo

    total = len(SEED[ENT_A]["txns"]) + len(SEED[ENT_B]["txns"])
    assert len(TransactionRepo.list_unclassified()) == total  # unscoped call unchanged
    b_rows = TransactionRepo.list_unclassified(account_ids=[SEED[ENT_B]["chk"]])
    assert {t.account_id for t in b_rows} == {SEED[ENT_B]["chk"]}
    assert len(b_rows) == len(SEED[ENT_B]["txns"])
    assert TransactionRepo.list_unclassified(account_ids=[]) == []


def test_repo_list_recent_scoped_newest_first(two_entity_db):
    from ledger_agent.core.database import TransactionRepo

    a_chk = SEED[ENT_A]["chk"]
    rows = TransactionRepo.list_recent([a_chk, SEED[ENT_A]["brk"]], 2)
    assert [t.account_id for t in rows] == [a_chk, a_chk]
    assert rows[0].date > rows[1].date
    assert TransactionRepo.list_recent([], 5) == []
    assert TransactionRepo.list_recent([a_chk], 0) == []


def test_repo_latest_period(two_entity_db, tmp_path):
    from ledger_agent.core.database import TransactionRepo, init_db

    assert TransactionRepo.latest_period() == PERIOD
    empty = tmp_path / "r08_empty.db"
    init_db(empty)
    assert TransactionRepo.latest_period(empty) is None
