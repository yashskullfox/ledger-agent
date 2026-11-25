"""
tests/unit/test_legacy_mcp_server.py  –  P-02: legacy ``mcp_server/`` is private

One test (or more) per legacy tool, on a synthetic tmp database:

* entity, account and institution names and masked account numbers never
  reach the client — they are tokenised;
* ``list_transactions`` returns only whitelisted, redacted fields on every
  path (never ``raw_description`` or a raw SQL row);
* ``classify_transaction`` does not echo its input description;
* ``get_tax_estimate`` returns the same figures as ``api.pte_estimate``;
* errors carry only an opaque ``ref:`` id; redaction failure withholds the
  response (fail closed).

The leak-case matrix for the same tools lives in ``test_llm_egress.py``.
Pseudonymous synthetic values only.
"""
from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

import config as _cfg
from ledger_agent.core import privacy

FY = 2026
PERIOD = f"{FY}-03"
ENTITY_ID = "ent-legacy"
ENTITY_NAME = "ENTITY_A TESTCO LLC"
CHK_NAME = "Operating Checking"
CARD_NAME = "Business Rewards Card"
CHK_INST = "BANK_X"
CARD_INST = "BANK_X4"
CHK_MASK = "****7731"  # redaction: allow (synthetic)
CARD_MASK = "****6620"  # redaction: allow (synthetic)
ACCT_NUMBER = "5500987654321"  # redaction: allow (synthetic)
RAW_ONLY = "RAWONLYMARKER"

# Strings that must never appear in any legacy response.
SECRETS = (ENTITY_NAME, "TESTCO", CHK_NAME, CARD_NAME, CHK_INST, CARD_INST,
           CHK_MASK, CARD_MASK, "7731", "6620", ACCT_NUMBER, RAW_ONLY)

_REF = re.compile(r"ref:[0-9a-f]{8}\b")


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
        SnapshotRepo,
        TransactionRepo,
        init_db,
    )
    from ledger_agent.core.models import (
        Account,
        AccountSnapshot,
        AccountType,
        Entity,
        Transaction,
        TransactionType,
    )

    init_db(db_file)
    EntityRepo.upsert(
        Entity(id=ENTITY_ID, name=ENTITY_NAME, entity_type="LLC", state="MO"), db_file
    )
    AccountRepo.upsert(Account(
        id="acct-legacy-chk", entity_id=ENTITY_ID, name=CHK_NAME, institution=CHK_INST,
        account_type=AccountType.CHECKING, account_number_masked=CHK_MASK,
    ), db_file)
    AccountRepo.upsert(Account(
        id="acct-legacy-card", entity_id=ENTITY_ID, name=CARD_NAME, institution=CARD_INST,
        account_type=AccountType.CREDIT_CARD, account_number_masked=CARD_MASK,
    ), db_file)
    for month in range(1, 13):
        SnapshotRepo.upsert(AccountSnapshot(
            account_id="acct-legacy-chk", statement_period=f"{FY}-{month:02d}",
            ending_balance=Decimal("5000"),
        ), db_file)
        SnapshotRepo.upsert(AccountSnapshot(
            account_id="acct-legacy-card", statement_period=f"{FY}-{month:02d}",
            ending_balance=Decimal("250"),
        ), db_file)
    TransactionRepo.bulk_insert([
        Transaction(
            id="txn-legacy-rev", account_id="acct-legacy-chk", date=date(FY, 3, 2),
            description="Client invoice payment",
            raw_description=f"Client invoice payment {RAW_ONLY}",
            amount=Decimal("12000.00"), transaction_type=TransactionType.CREDIT,
            statement_period=PERIOD,
            coa_code="4020" if classified else "", coa_name="Service Revenue" if classified else "",
        ),
        Transaction(
            id="txn-legacy-acct", account_id="acct-legacy-chk", date=date(FY, 3, 9),
            description=f"ACH DEBIT ACCOUNT {ACCT_NUMBER} ONLINE",
            raw_description=f"ACH DEBIT ACCOUNT {ACCT_NUMBER} ONLINE {RAW_ONLY}",
            amount=Decimal("-300.00"), transaction_type=TransactionType.DEBIT,
            statement_period=PERIOD,
            coa_code="5080" if classified else "",
            coa_name="Other Operating Expenses" if classified else "",
        ),
    ], db_file)


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    db_file = tmp_path / "legacy.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    _seed(db_file)
    return db_file


@pytest.fixture
def legacy_db_unclassified(tmp_path, monkeypatch):
    db_file = tmp_path / "legacy_unclassified.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    _seed(db_file, classified=False)
    return db_file


def _rpc(capsys, method: str, params: dict | None = None) -> dict:
    from mcp_server import server as legacy

    capsys.readouterr()
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": 7, "method": method}
    if params is not None:
        msg["params"] = params
    legacy._dispatch(msg)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1, "legacy server must write exactly one JSON-RPC frame"
    return json.loads(lines[0])


def _call(capsys, tool: str, args: dict | None = None) -> tuple[dict, dict | None]:
    """(raw JSON-RPC response, decoded tool payload or None on error)."""
    resp = _rpc(capsys, "tools/call", {"name": tool, "arguments": args or {}})
    result = resp.get("result") or {}
    if "error" in resp or result.get("isError"):
        return resp, None
    return resp, json.loads(result["content"][0]["text"])


def _assert_private(resp: dict) -> None:
    text = json.dumps(resp, ensure_ascii=False)
    decoded = "\n".join(
        item.get("text", "") for item in (resp.get("result") or {}).get("content") or []
    )
    folded = (text + "\n" + decoded).casefold()
    hits = sum(1 for s in SECRETS if s.casefold() in folded)
    assert hits == 0, f"{hits} identifying value(s) present in legacy response"


# ── protocol ──────────────────────────────────────────────────────────────────

def test_tools_list_unchanged(capsys):
    resp = _rpc(capsys, "tools/list")
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == {"get_balance_sheet", "list_transactions", "get_tax_estimate",
                     "classify_transaction", "list_periods", "get_entity_summary",
                     "privacy_status"}
    tax = next(t for t in resp["result"]["tools"] if t["name"] == "get_tax_estimate")
    assert tax["inputSchema"]["required"] == ["period"]


# ── one test per tool ─────────────────────────────────────────────────────────

def test_get_balance_sheet_tokenises_entity_and_accounts(legacy_db, capsys):
    from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder

    resp, data = _call(capsys, "get_balance_sheet", {"period": PERIOD})
    assert data is not None
    _assert_private(resp)
    assert data["entity"] == "<ENTITY>"
    labels = [line["label"] for line in data["lines"]]
    assert "<INSTITUTION_1> – <ACCOUNT_2>" in labels           # checking (1010)
    assert "Credit Card Payable – <INSTITUTION_2> (<ACCOUNT_1>)" in labels  # card (2030)
    bs = BalanceSheetBuilder(ENTITY_ID, PERIOD).build()
    assert data["total_assets"] == float(bs.total_assets)
    assert data["total_liabilities"] == float(bs.total_liabilities)


@pytest.mark.parametrize("args", [{"period": PERIOD}, {}], ids=["period", "no_period"])
def test_list_transactions_whitelisted_and_redacted(args, legacy_db, capsys):
    """Both paths — incl. the no-period raw-SQL fallback — return clean records."""
    resp, data = _call(capsys, "list_transactions", args)
    assert data is not None
    _assert_private(resp)
    assert data["count"] == 2
    allowed = {"date", "description", "amount", "type", "coa_code", "coa_name", "period"}
    for rec in data["transactions"]:
        assert set(rec) == allowed
    assert any("<ACCT_" in rec["description"] for rec in data["transactions"])


def test_list_transactions_unclassified_path(legacy_db_unclassified, capsys):
    resp, data = _call(capsys, "list_transactions", {})
    assert data is not None
    _assert_private(resp)
    assert data["count"] == 2
    assert all("raw_description" not in rec for rec in data["transactions"])


def test_list_transactions_respects_limit(legacy_db, capsys):
    _resp, data = _call(capsys, "list_transactions", {"limit": 1})
    assert data is not None and data["count"] == 1


def test_get_tax_estimate_matches_api_pte_estimate(legacy_db, capsys):
    from ledger_agent.core import api

    resp, data = _call(capsys, "get_tax_estimate", {"period": PERIOD})
    assert data is not None
    _assert_private(resp)
    est = api.pte_estimate(FY)
    assert data["entity"] == "<ENTITY>"
    assert data["year"] == FY
    assert data["total_annual_tax"] == float(est.total_annual_tax)
    assert data["net_income_annual"] == float(est.net_income)
    assert data["quarterly_payment"] == float(est.quarterly_payment)
    assert data["basis"] == est.basis == "full_year"
    assert len(data["quarterly_payments"]) == 4
    assert data["total_annual_tax"] > 0  # non-vacuous: revenue was seeded


def test_get_tax_estimate_no_data_is_clean_error(legacy_db, capsys):
    resp, data = _call(capsys, "get_tax_estimate", {"period": "2019-01"})
    assert data is not None and "error" in data
    _assert_private(resp)


def test_classify_transaction_does_not_echo_description(legacy_db, capsys):
    resp, data = _call(capsys, "classify_transaction",
                       {"description": f"ACH DEBIT ACCOUNT {ACCT_NUMBER} {ENTITY_NAME}"})
    assert data is not None
    _assert_private(resp)
    assert "description" not in data
    assert set(data) == {"coa_code", "coa_name", "confidence", "source"}


def test_list_periods_tokenises_entity(legacy_db, capsys):
    resp, data = _call(capsys, "list_periods")
    assert data is not None
    _assert_private(resp)
    assert data["entity"] == "<ENTITY>"
    assert len(data["periods"]) == 12


def test_get_entity_summary_tokenises_everything(legacy_db, capsys):
    resp, data = _call(capsys, "get_entity_summary")
    assert data is not None
    _assert_private(resp)
    assert data["entity"]["name"] == "<ENTITY>"
    assert data["entity"]["state"] == "MO"
    accounts = data["accounts"]
    assert [a["name"] for a in accounts] == ["<ACCOUNT_1>", "<ACCOUNT_2>"]
    assert {a["institution"] for a in accounts} == {"<INSTITUTION_1>", "<INSTITUTION_2>"}
    assert all(a["masked"] == "<ACCT_MASKED>" for a in accounts)
    assert data["period_count"] == 12


def test_privacy_status_answers(legacy_db, capsys):
    resp, data = _call(capsys, "privacy_status")
    assert data is not None and "error" not in data
    _assert_private(resp)


# ── errors and fail-closed ────────────────────────────────────────────────────

def test_tool_error_is_opaque_ref(legacy_db, capsys, monkeypatch):
    from mcp_server import server as legacy

    def _boom(_args):
        raise RuntimeError(f"synthetic failure {ENTITY_NAME}")

    monkeypatch.setitem(legacy._HANDLERS, "get_entity_summary", _boom)
    resp, data = _call(capsys, "get_entity_summary")
    assert data is None and resp["result"]["isError"] is True
    text = resp["result"]["content"][0]["text"]
    assert _REF.search(text)
    assert "Traceback" not in text and "synthetic failure" not in text
    _assert_private(resp)


def test_redaction_failure_withholds_response(legacy_db, capsys, monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("redactor unavailable")

    monkeypatch.setattr(privacy, "redact", _boom)
    resp, data = _call(capsys, "list_transactions", {"period": PERIOD})
    assert data is None
    assert "result" not in resp
    assert resp["error"]["code"] == -32000
    assert _REF.search(resp["error"]["message"])
    _assert_private(resp)


def test_api_key_in_result_withholds_response(legacy_db, capsys, monkeypatch):
    from mcp_server import server as legacy

    fake_key = "sk-" + "A" * 24  # synthetic, matches the API-key detector shape
    monkeypatch.setitem(legacy._HANDLERS, "list_periods", lambda _a: {"note": fake_key})
    resp, data = _call(capsys, "list_periods")
    assert data is None and resp["error"]["code"] == -32000
    assert fake_key not in json.dumps(resp)


def test_dispatch_loop_error_is_opaque(capsys, monkeypatch):
    from mcp_server import server as legacy

    lines = iter(['{"jsonrpc":"2.0","id":1,"method":"ping"}\n', ""])
    monkeypatch.setattr(legacy.sys.stdin, "readline", lambda: next(lines), raising=False)

    def _boom(_msg):
        raise RuntimeError(f"dispatch exploded {ENTITY_NAME}")

    monkeypatch.setattr(legacy, "_dispatch", _boom)
    monkeypatch.setattr(legacy.logging, "basicConfig", lambda **_kw: None)
    capsys.readouterr()
    legacy.main()
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    resp = json.loads(out[0])
    assert resp["error"]["code"] == -32603
    assert _REF.search(resp["error"]["message"])
    assert "Traceback" not in out[0] and "exploded" not in out[0]
