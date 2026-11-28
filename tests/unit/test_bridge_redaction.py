"""
tests/unit/test_bridge_redaction.py  –  P-11: the Form D bridge redacts string VALUES.

``bridge/jsonrpc_stdio.py`` used to redact the whole response as one ASCII-escaped
``json.dumps`` string: non-ASCII names were ``\\uXXXX`` escapes the detectors never
matched, and a bare 8+ digit JSON integer was tokenised, which broke the JSON so the
response was withheld.  These tests drive ``_dispatch`` in-process against a synthetic
tmp_path database (no Java) and check:

* German / Devanagari partner names (``FI_PARTNER_<n>_NAME``, also inside transaction
  descriptions) never reach a bridge response; the ASCII entity name neither;
* numbers, booleans, null and keys round-trip unchanged — a large integer is not
  tokenised and the response is not withheld;
* ``allow_pii=true`` still returns raw values;
* any redaction error, or an ``audit_egress`` block, is an opaque JSON-RPC error
  (``ref:``, category only — never a value);
* the wire format stays one ASCII line per response (non-ASCII escaped).

Synthetic names and figures only.
"""
from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal

import pytest

from ledger_agent.core import config as _cfg
from ledger_agent.core import privacy

FY = 2026
ENTITY_ID = "ent-bridge-a"
ENTITY_NAME = "Northwind Quillfeather Group LLC"
PARTNER_DE = "Jürgen Müller"
PARTNER_HI = "राहुल शर्मा"
CHK = "acct-bridge-chk"
SAV = "acct-bridge-sav"

DESC_DE = f"Umbuchung an {PARTNER_DE}"
DESC_HI = f"{PARTNER_HI} को भुगतान"
DESC_ENTITY = f"Transfer to {ENTITY_NAME}"

# Name fragments that must never appear in a default (allow_pii=false) response.
_NEEDLES = ("Jürgen", "Müller", "राहुल", "शर्मा", "Northwind", "Quillfeather")


def _hits(obj) -> list[str]:
    """Needles present in the DECODED response (escapes resolved)."""
    text = json.dumps(obj, ensure_ascii=False).casefold()
    return [n for n in _NEEDLES if n.casefold() in text]


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def bdb(tmp_path, monkeypatch):
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

    monkeypatch.setattr(_cfg, "AI_EGRESS_MODE", "redact")
    monkeypatch.setattr(_cfg, "PRIVACY_ENTITY_NAME", "")
    monkeypatch.delenv("FI_PRIVACY_ENTITY_NAME", raising=False)
    for key in list(os.environ):
        if key.startswith("FI_PARTNER_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FI_PARTNER_1_NAME", PARTNER_DE)
    monkeypatch.setenv("FI_PARTNER_2_NAME", PARTNER_HI)

    db_file = tmp_path / "bridge.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    init_db(db_file)
    EntityRepo.upsert(
        Entity(id=ENTITY_ID, name=ENTITY_NAME, entity_type="LLC", state="MO"), db_file
    )
    for aid, name, atype in ((CHK, "Operating Checking", AccountType.CHECKING),
                             (SAV, "Reserve Savings", AccountType.SAVINGS)):
        AccountRepo.upsert(Account(
            id=aid, entity_id=ENTITY_ID, name=name, institution="BANK_X",
            account_type=atype, account_number_masked="0000",
        ), db_file)

    def txn(i, acct, month, amount, ttype, desc, coa, *, transfer=False):
        return Transaction(
            id=f"txn-bridge-{i}", account_id=acct, date=date(FY, month, 10),
            description=desc, raw_description=desc, amount=Decimal(amount),
            transaction_type=ttype, statement_period=f"{FY}-{month:02d}",
            coa_code=coa, is_transfer=transfer,
        )

    TransactionRepo.bulk_insert([
        txn(1, CHK, 1, "5000.00", TransactionType.CREDIT, "Client retainer", "4020"),
        txn(2, CHK, 1, "-300.00", TransactionType.DEBIT, "Cloud hosting", "5010"),
        # Unmatched transfers (different amounts) — their descriptions reach
        # reconcile_year's `issues` strings.
        txn(3, CHK, 2, "-125.00", TransactionType.TRANSFER_OUT, DESC_DE, "9000",
            transfer=True),
        txn(4, SAV, 2, "250.00", TransactionType.TRANSFER_IN, DESC_HI, "9000",
            transfer=True),
        txn(5, CHK, 3, "-75.00", TransactionType.TRANSFER_OUT, DESC_ENTITY, "9000",
            transfer=True),
    ], db_file)
    for acct, end in ((CHK, "4500.00"), (SAV, "250.00")):
        SnapshotRepo.upsert(AccountSnapshot(
            account_id=acct, statement_period=f"{FY}-03", ending_balance=Decimal(end),
        ), db_file)

    privacy._reset_session()
    privacy.refresh_protected_names()
    yield db_file
    privacy._reset_session()
    privacy.refresh_protected_names()


def _bridge(capsys, method, params=None, *, allow_pii=None):
    """Dispatch one request; return (raw wire line, decoded response)."""
    from ledger_agent.bridge.jsonrpc_stdio import _dispatch

    params = dict(params or {})
    if allow_pii is not None:
        params["_meta"] = {"allow_pii": allow_pii}
    capsys.readouterr()
    _dispatch({"jsonrpc": "2.0", "id": 11, "method": method, "params": params})
    out = capsys.readouterr().out
    lines = [ln for ln in out.split("\n") if ln]
    assert len(lines) == 1, "bridge must write exactly one JSON-RPC frame"
    assert out.endswith("\n")
    return lines[0], json.loads(lines[0])


def _result(resp):
    assert "error" not in resp, resp.get("error")
    return resp["result"]


def _fake_tool(monkeypatch, payload: dict):
    """Make the bridge's call_tool return ``payload`` (as call_tool would: JSON text)."""
    import ledger_agent.mcp.tools as tools

    monkeypatch.setattr(tools, "call_tool",
                        lambda name, args, *, allow_pii=False: json.dumps(payload))


# ── Non-ASCII names through real bridge methods ──────────────────────────────

def test_reconcile_year_descriptions_redacted(bdb, capsys):
    import ledger_agent.core.api as api

    raw_issues = " ".join(api.reconcile_year(FY).issues)
    # Non-vacuous: the names really are in the unredacted core output.
    assert PARTNER_DE in raw_issues and PARTNER_HI in raw_issues
    assert ENTITY_NAME in raw_issues

    line, resp = _bridge(capsys, "reconcile_year", {"fiscal_year": FY})
    result = _result(resp)
    assert result["unmatched"] == 3
    assert len(result["issues"]) == 3
    assert _hits(result) == []
    # Still classifiable: the non-name words survive.
    assert "Umbuchung" in " ".join(result["issues"])


@pytest.mark.parametrize("method", ["customer_summary", "audit_year",
                                    "generate_balance_sheet", "generate_form_1065",
                                    "pte_estimate"])
def test_other_methods_carry_no_names(bdb, capsys, method):
    _line, resp = _bridge(capsys, method, {"fiscal_year": FY})
    assert _hits(_result(resp)) == []


def test_k1_carries_no_names(bdb, capsys):
    _line, resp = _bridge(capsys, "generate_k1", {"fiscal_year": FY,
                                                  "partner_id": "partner_1"})
    assert _hits(_result(resp)) == []


def test_customer_summary_and_audit_year_match_api(bdb, capsys):
    import ledger_agent.core.api as api

    _line, resp = _bridge(capsys, "customer_summary", {"fiscal_year": FY})
    cs = _result(resp)
    a = api.build_customer_summary(FY)
    assert cs["confidence_flags"] == list(a.confidence_flags)
    assert (float(cs["profit_or_loss"]["ordinary_business_income"])
            == float(a.profit_or_loss["ordinary_business_income"]))

    _line, resp = _bridge(capsys, "audit_year", {"fiscal_year": FY})
    au = _result(resp)
    a2 = api.audit_year(FY)
    assert au["status"] == a2["status"]
    assert ([(c["name"], c["status"]) for c in au["checks"]]
            == [(c["name"], c["status"]) for c in a2["checks"]])


def test_wire_format_is_one_ascii_line(bdb, capsys, monkeypatch):
    _fake_tool(monkeypatch, {"label": "Büro café", "n": 1})
    line, resp = _bridge(capsys, "customer_summary", {"fiscal_year": FY})
    assert line.isascii()  # non-ASCII is \\u-escaped on the wire, as before
    assert "\\u00fc" in line
    assert _result(resp)["label"] == "Büro café"


def test_ascii_entity_name_redacted(bdb, capsys, monkeypatch):
    _fake_tool(monkeypatch, {"memo": f"Wire from {ENTITY_NAME}",
                             "rows": [{"text": "QUILLFEATHER payout"}]})
    _line, resp = _bridge(capsys, "customer_summary", {"fiscal_year": FY})
    result = _result(resp)
    assert _hits(result) == []
    assert privacy.ENTITY_TOKEN in result["memo"]


def test_nested_non_ascii_names_redacted(bdb, capsys, monkeypatch):
    _fake_tool(monkeypatch, {
        "next_actions": [f"Zahlung an {PARTNER_DE} prüfen", DESC_HI],
        "deep": {"list": [[{"note": f"{PARTNER_HI} / {PARTNER_DE.upper()}"}]]},
    })
    _line, resp = _bridge(capsys, "audit_year", {"fiscal_year": FY})
    result = _result(resp)
    assert _hits(result) == []
    assert result["next_actions"][0].startswith("Zahlung an ")


# ── Numbers, booleans, null and keys are not touched ─────────────────────────

def test_large_integer_round_trips_and_is_not_withheld(bdb, capsys, monkeypatch):
    payload = {
        "row_count": 123_456_789_012,
        "big": 98_765_432,
        "nested": [{"n": 87_654_321, "ok": True, "missing": None, "ratio": 0.25}],
        "amount": 12_345_678.5,
        "note": DESC_DE,
    }
    _fake_tool(monkeypatch, payload)
    _line, resp = _bridge(capsys, "customer_summary", {"fiscal_year": FY})
    result = _result(resp)
    assert result["row_count"] == payload["row_count"]
    assert type(result["row_count"]) is int
    assert result["big"] == payload["big"]
    assert result["nested"] == payload["nested"]
    assert result["amount"] == payload["amount"]
    assert set(result) == set(payload)  # keys untouched
    assert _hits(result) == []


def test_digit_run_inside_a_string_is_still_tokenised(bdb, capsys, monkeypatch):
    _fake_tool(monkeypatch, {"memo": "ref 4417 8823 9910 0042 deposit", "count": 44_178_823})
    _line, resp = _bridge(capsys, "customer_summary", {"fiscal_year": FY})
    result = _result(resp)
    assert "4417" not in result["memo"]
    assert result["count"] == 44_178_823


def test_protected_name_in_a_key_fails_closed(bdb, capsys, monkeypatch):
    _fake_tool(monkeypatch, {PARTNER_DE: 1})
    line, resp = _bridge(capsys, "customer_summary", {"fiscal_year": FY})
    assert "result" not in resp
    assert resp["error"]["code"] == -32000
    assert "protected_name" in resp["error"]["message"]
    assert _hits(resp) == [] and "J\\u00fcrgen" not in line


# ── allow_pii ─────────────────────────────────────────────────────────────────

def test_allow_pii_returns_raw(bdb, capsys):
    _line, resp = _bridge(capsys, "reconcile_year", {"fiscal_year": FY}, allow_pii=True)
    issues = " ".join(_result(resp)["issues"])
    assert PARTNER_DE in issues and PARTNER_HI in issues and ENTITY_NAME in issues


def test_allow_pii_still_blocks_api_keys(bdb, capsys, monkeypatch):
    fake_key = "sk-" + "Q" * 30  # built at runtime: synthetic, never a real key
    _fake_tool(monkeypatch, {"memo": fake_key})
    line, resp = _bridge(capsys, "customer_summary", {"fiscal_year": FY}, allow_pii=True)
    assert "result" not in resp and resp["error"]["code"] == -32000
    assert fake_key not in line


# ── Fail closed ───────────────────────────────────────────────────────────────

def test_redaction_error_is_opaque(bdb, capsys, monkeypatch):
    def boom(text, scope="openai", entity_name=""):
        raise ValueError(f"detector crashed on {text!r}")

    monkeypatch.setattr(privacy, "redact", boom)
    line, resp = _bridge(capsys, "reconcile_year", {"fiscal_year": FY})
    assert "result" not in resp
    err = resp["error"]
    assert err["code"] == -32000
    assert err["message"].startswith("bridge_redaction_failed")
    assert "ref:" in err["message"]
    assert "detector" not in line and "Traceback" not in line
    assert _hits(resp) == [] and "Umbuchung" not in line


def test_egress_block_is_category_only(bdb, capsys, monkeypatch):
    # A redactor that silently does nothing: audit_egress must still stop the names.
    monkeypatch.setattr(privacy, "redact",
                        lambda text, scope="openai", entity_name="": (text, {}))
    line, resp = _bridge(capsys, "reconcile_year", {"fiscal_year": FY})
    assert "result" not in resp
    msg = resp["error"]["message"]
    assert resp["error"]["code"] == -32000
    assert msg.startswith("bridge_egress_blocked") and "protected_name" in msg
    assert "ref:" in msg
    assert _hits(resp) == [] and "Umbuchung" not in line


def test_egress_check_error_is_opaque(bdb, capsys, monkeypatch):
    def broken_audit(payload):
        raise TypeError(f"cannot scan {payload!r}")

    monkeypatch.setattr(privacy, "audit_egress", broken_audit)
    line, resp = _bridge(capsys, "reconcile_year", {"fiscal_year": FY})
    assert "result" not in resp and resp["error"]["code"] == -32000
    assert "ref:" in resp["error"]["message"]
    assert "cannot scan" not in line and "Umbuchung" not in line


def test_allow_pii_skips_egress_audit(bdb, capsys, monkeypatch):
    called = []
    monkeypatch.setattr(privacy, "audit_egress", lambda payload: called.append(1))
    _line, resp = _bridge(capsys, "reconcile_year", {"fiscal_year": FY}, allow_pii=True)
    _result(resp)
    assert called == []
