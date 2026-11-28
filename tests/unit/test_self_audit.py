"""
tests/unit/test_self_audit.py  –  G-01: CPA self-audit checks + the four-form wiring.

Every check in ``accounting/self_audit.py`` gets a passing and a failing synthetic case,
then ``api.audit_year``, the CLI (``ledger audit --no-prompt``), the MCP tool and the
bridge method are exercised end to end, including a privacy check that the MCP / bridge
response never carries the entity name, account names, institution or masked number.

Synthetic tmp_path databases only; pseudonymous names; no real data.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

FY = 2026
ENTITY_ID = "ent-audit-a"
ENTITY_NAME = "ENTITY_A AUDITCASE LLC"
CHK = "acct-audit-chk"
CHK2 = "acct-audit-chk2"
CHK_NAME = "Quillbrook Operating"
CHK2_NAME = "Quillbrook Reserve"
CHK_MASK = "****7316"  # redaction: allow (synthetic)
CHK2_MASK = "****8425"  # redaction: allow (synthetic)


# ── Fixtures / builders ───────────────────────────────────────────────────────

@pytest.fixture
def adb(tmp_path, monkeypatch):
    from ledger_agent.core.database import AccountRepo, EntityRepo, init_db
    from ledger_agent.core.models import Account, AccountType, Entity

    db_file = tmp_path / "audit.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    for k in ("FI_PARTNER_1_PL", "FI_PARTNER_2_PL", "FI_PARTNER_1_CAPITAL",
              "FI_PARTNER_2_CAPITAL"):
        monkeypatch.delenv(k, raising=False)
    init_db(db_file)
    EntityRepo.upsert(
        Entity(id=ENTITY_ID, name=ENTITY_NAME, entity_type="LLC", state="MO"), db_file
    )
    for aid, name, mask in ((CHK, CHK_NAME, CHK_MASK), (CHK2, CHK2_NAME, CHK2_MASK)):
        AccountRepo.upsert(Account(
            id=aid, entity_id=ENTITY_ID, name=name, institution="BANK_X",
            account_type=AccountType.CHECKING, account_number_masked=mask,
        ), db_file)
    return db_file


_SEQ = iter(range(1, 1_000_000))


def _txn(account_id, period, amount, coa="4020", *, ttype=None, day=10, is_transfer=False,
         is_reconciled=False, tags=None, description="Synthetic line"):
    from ledger_agent.core.models import Transaction, TransactionType

    amount = Decimal(str(amount))
    if ttype is None:
        ttype = TransactionType.CREDIT if amount > 0 else TransactionType.DEBIT
    return Transaction(
        id=f"txn-audit-{next(_SEQ)}", account_id=account_id,
        date=date(int(period[:4]), int(period[5:7]), day), description=description,
        raw_description=description, amount=amount, transaction_type=ttype,
        statement_period=period, coa_code=coa, is_transfer=is_transfer,
        is_reconciled=is_reconciled, tags=list(tags or []),
    )


def _insert(*txns):
    from ledger_agent.core.database import TransactionRepo

    TransactionRepo.bulk_insert(list(txns))


def _snap(account_id, period, end, beg=None):
    from ledger_agent.core.database import SnapshotRepo
    from ledger_agent.core.models import AccountSnapshot

    SnapshotRepo.upsert(AccountSnapshot(
        account_id=account_id, statement_period=period, ending_balance=Decimal(str(end)),
        beginning_balance=None if beg is None else Decimal(str(beg)),
    ))


def _conn_check(fn, *args):
    from ledger_agent.core.database import get_conn

    with get_conn() as conn:
        return fn(conn, *args)


def _clean_full_year(account_id=CHK, expense="-10.00"):
    """12 tied-out months of one expense line each (zero tax → no PTE flags)."""
    bal = Decimal("1000.00")
    _snap(account_id, f"{FY - 1}-12", bal)
    for m in range(1, 13):
        per = f"{FY}-{m:02d}"
        _insert(_txn(account_id, per, expense, coa="5010"))
        end = bal + Decimal(expense)
        _snap(account_id, per, end, beg=bal)
        bal = end


def _add_offsetting(coa, period=f"{FY}-03"):
    """+1 / -1 pair with ``coa``: the statement still ties out, P&L is unchanged."""
    desc = f"Offsetting line {coa}"  # distinct per code: bulk_insert dedups identical rows
    _insert(_txn(CHK, period, "1.00", coa=coa, description=desc),
            _txn(CHK, period, "-1.00", coa=coa, description=desc))


# ── statement_tieout ──────────────────────────────────────────────────────────

def test_tieout_pass(adb):
    from ledger_agent.core.accounting.self_audit import check_statement_tieout

    _snap(CHK, f"{FY}-01", "150.00", beg="100.00")
    _insert(_txn(CHK, f"{FY}-01", "70.00"), _txn(CHK, f"{FY}-01", "-20.00", coa="5010"))
    r = _conn_check(check_statement_tieout, ENTITY_ID, FY)
    assert r.status == "pass" and r.details == []


def test_tieout_fail_reports_opaque_ids_only(adb):
    from ledger_agent.core.accounting.self_audit import check_statement_tieout

    _snap(CHK, f"{FY}-01", "160.00", beg="100.00")
    _insert(_txn(CHK, f"{FY}-01", "70.00"), _txn(CHK, f"{FY}-01", "-20.00", coa="5010"))
    r = _conn_check(check_statement_tieout, ENTITY_ID, FY)
    assert r.status == "fail"
    assert r.details == [{"account_id": CHK, "account_type": "checking", "period": f"{FY}-01",
                          "unexplained": "-10.00", "transaction_count": 2}]


def test_tieout_not_applicable_without_opening_balance(adb):
    from ledger_agent.core.accounting.self_audit import check_statement_tieout

    _snap(CHK, f"{FY}-01", "150.00")
    assert _conn_check(check_statement_tieout, ENTITY_ID, FY).status == "not_applicable"


# ── account_continuity ────────────────────────────────────────────────────────

def test_continuity_pass_seeded_by_prior_december(adb):
    from ledger_agent.core.accounting.self_audit import check_account_continuity

    _snap(CHK, f"{FY - 1}-11", "50.00")   # earlier prior-year months are ignored
    _snap(CHK, f"{FY - 1}-12", "100.00")
    _snap(CHK, f"{FY}-01", "120.00", beg="100.00")
    _snap(CHK, f"{FY}-02", "130.00", beg="120.00")
    r = _conn_check(check_account_continuity, ENTITY_ID, FY)
    assert r.status == "pass"
    assert r.summary.startswith("2/2")


def test_continuity_fail_january_vs_prior_december(adb):
    """Prior-year December closing still seeds the January comparison (fix (a))."""
    from ledger_agent.core.accounting.self_audit import check_account_continuity

    _snap(CHK, f"{FY - 1}-12", "100.00")
    _snap(CHK, f"{FY}-01", "120.00", beg="90.00")
    r = _conn_check(check_account_continuity, ENTITY_ID, FY)
    assert r.status == "fail"
    assert r.details == [{"account_id": CHK, "account_type": "checking", "period": f"{FY}-01",
                          "opening_minus_prior_closing": "-10.00"}]


def test_continuity_fail_mid_year(adb):
    from ledger_agent.core.accounting.self_audit import check_account_continuity

    _snap(CHK, f"{FY}-01", "120.00", beg="100.00")
    _snap(CHK, f"{FY}-02", "130.00", beg="125.00")
    r = _conn_check(check_account_continuity, ENTITY_ID, FY)
    assert r.status == "fail"
    assert r.details[0]["period"] == f"{FY}-02"


def test_continuity_missing_month_warns(adb):
    from ledger_agent.core.accounting.self_audit import check_account_continuity

    _snap(CHK, f"{FY}-01", "120.00", beg="100.00")
    _snap(CHK, f"{FY}-03", "130.00", beg="120.00")  # February missing
    r = _conn_check(check_account_continuity, ENTITY_ID, FY)
    assert r.status == "warn"
    assert r.details == [{"account_id": CHK, "account_type": "checking",
                          "from_period": f"{FY}-01", "to_period": f"{FY}-03",
                          "kind": "missing_month"}]


def test_continuity_not_applicable_single_statement(adb):
    from ledger_agent.core.accounting.self_audit import check_account_continuity

    _snap(CHK, f"{FY}-01", "120.00", beg="100.00")
    assert _conn_check(check_account_continuity, ENTITY_ID, FY).status == "not_applicable"


# ── period_coverage ───────────────────────────────────────────────────────────

def test_period_coverage_pass(adb):
    from ledger_agent.core.accounting.self_audit import check_period_coverage

    for m in range(1, 13):
        _snap(CHK, f"{FY}-{m:02d}", "1")
    r = check_period_coverage(ENTITY_ID, FY)
    assert r.status == "pass" and r.details == []


def test_period_coverage_missing_month_warns(adb):
    from ledger_agent.core.accounting.self_audit import check_period_coverage

    for m in range(1, 13):
        if m != 5:
            _snap(CHK, f"{FY}-{m:02d}", "1")
    r = check_period_coverage(ENTITY_ID, FY)
    assert r.status == "warn"
    assert r.details == [{"account_id": CHK, "missing_months": [5]}]


# ── unclassified ──────────────────────────────────────────────────────────────

def test_unclassified_pass_ignores_suspense_transfers():
    from ledger_agent.core.accounting.self_audit import check_unclassified

    txns = [_txn(CHK, f"{FY}-01", "5", coa="4020"),
            _txn(CHK, f"{FY}-01", "-5", coa="9999", is_transfer=True)]
    assert check_unclassified(txns).status == "pass"


def test_unclassified_warns_with_counts_by_code():
    from ledger_agent.core.accounting.self_audit import check_unclassified

    txns = [_txn(CHK, f"{FY}-01", "5", coa=""), _txn(CHK, f"{FY}-01", "-5", coa="9999"),
            _txn(CHK, f"{FY}-01", "-7", coa="9999"), _txn(CHK, f"{FY}-01", "1", coa="4020")]
    r = check_unclassified(txns)
    assert r.status == "warn"
    assert r.details == [{"coa_code": "9999", "count": 2}, {"coa_code": "none", "count": 1}]


# ── unknown_coa_codes ─────────────────────────────────────────────────────────

def test_unknown_codes_pass(adb):
    from ledger_agent.core.accounting.self_audit import check_unknown_codes

    txns = [_txn(CHK, f"{FY}-01", "5", coa="4020"), _txn(CHK, f"{FY}-01", "-5", coa="5999")]
    assert _conn_check(check_unknown_codes, txns).status == "pass"


def test_unknown_codes_fail(adb):
    from ledger_agent.core.accounting.self_audit import check_unknown_codes

    txns = [_txn(CHK, f"{FY}-01", "5", coa="7777"), _txn(CHK, f"{FY}-01", "6", coa="7777")]
    r = _conn_check(check_unknown_codes, txns)
    assert r.status == "fail"
    assert r.details == [{"coa_code": "7777", "count": 2}]


# ── transfer_matching ─────────────────────────────────────────────────────────

def _xfer_pair(out_acct, in_acct, amount="500.00", *, reconciled=False):
    from ledger_agent.core.models import TransactionType

    return [
        _txn(out_acct, f"{FY}-01", f"-{amount}", coa="9000", ttype=TransactionType.TRANSFER_OUT,
             day=10, is_transfer=True, is_reconciled=reconciled),
        _txn(in_acct, f"{FY}-01", amount, coa="9000", ttype=TransactionType.TRANSFER_IN,
             day=11, is_transfer=True, is_reconciled=reconciled),
    ]


def test_transfer_matching_not_applicable_without_transfers():
    from ledger_agent.core.accounting.self_audit import check_transfer_matching

    assert check_transfer_matching([_txn(CHK, f"{FY}-01", "5")]).status == "not_applicable"


def test_transfer_matching_pass_across_accounts():
    from ledger_agent.core.accounting.self_audit import check_transfer_matching

    r = check_transfer_matching(_xfer_pair(CHK, CHK2))
    assert r.status == "pass"
    assert r.summary.startswith("1 transfer pair(s) matched across accounts, 0 unmatched")


def test_transfer_matching_already_reconciled_legs_still_counted_and_not_mutated():
    """Already-reconciled legs are still paired, and the audit never mutates its input."""
    from ledger_agent.core.accounting.self_audit import check_transfer_matching

    legs = _xfer_pair(CHK, CHK2, reconciled=True)
    r = check_transfer_matching(legs)
    assert r.status == "pass" and r.summary.startswith("1 transfer pair")
    fresh = _xfer_pair(CHK, CHK2)
    check_transfer_matching(fresh)
    assert not any(t.is_reconciled for t in fresh)


def test_transfer_matching_unmatched_leg_warns():
    from ledger_agent.core.accounting.self_audit import check_transfer_matching

    legs = _xfer_pair(CHK, CHK2)[:1]
    r = check_transfer_matching(legs)
    assert r.status == "warn"
    assert r.details[0]["kind"] == "unmatched" and r.details[0]["account_id"] == CHK


def test_transfer_matching_same_account_legs_are_unmatched():
    """R-03: reconcile() never pairs two legs in one account — both come back unmatched."""
    from ledger_agent.core.accounting.self_audit import check_transfer_matching

    legs = _xfer_pair(CHK, CHK) + _xfer_pair(CHK, CHK2, amount="200.00")
    r = check_transfer_matching(legs)
    assert r.status == "warn"
    assert r.summary.startswith("1 transfer pair(s) matched across accounts, 2 unmatched")
    assert "within the same account" not in r.summary
    assert not [d for d in r.details if d["kind"] == "same_account_pair"]
    unmatched = [d for d in r.details if d["kind"] == "unmatched"]
    assert [(d["account_id"], d["amount"]) for d in unmatched] == [
        (CHK, "-500.00"), (CHK, "500.00"),
    ]


def test_transfer_matching_reports_untyped_transfer_legs():
    from ledger_agent.core.accounting.self_audit import check_transfer_matching

    legs = _xfer_pair(CHK, CHK2) + [_txn(CHK, f"{FY}-01", "-9", coa="9000", is_transfer=True)]
    r = check_transfer_matching(legs)
    assert "1 transfer-flagged leg(s) not typed transfer_in/out" in r.summary


# ── owner_draw_candidates ─────────────────────────────────────────────────────

def test_owner_draw_candidates_pass_when_destination_is_own_account(adb):
    from ledger_agent.core.accounting.self_audit import check_owner_draw_candidates
    from ledger_agent.core.intelligence.owner_draws import OWNER_DRAW_CANDIDATE_TAG

    t = _txn(CHK, f"{FY}-01", "-300", coa="9000", tags=[OWNER_DRAW_CANDIDATE_TAG],
             description=f"ONLINE TRANSFER TO CHK x{CHK2_MASK[-4:]}")
    assert check_owner_draw_candidates([t, _txn(CHK, f"{FY}-01", "5")]).status == "pass"


def test_owner_draw_candidates_warn_for_outside_account(adb):
    from ledger_agent.core.accounting.self_audit import check_owner_draw_candidates
    from ledger_agent.core.intelligence.owner_draws import OWNER_DRAW_CANDIDATE_TAG

    t = _txn(CHK, f"{FY}-01", "-300", coa="9000", tags=[OWNER_DRAW_CANDIDATE_TAG],
             description="ONLINE TRANSFER TO CHK x6402")
    r = check_owner_draw_candidates([t])
    assert r.status == "warn"
    assert set(r.details[0]) == {"account_id", "period", "date", "amount", "coa_code"}


# ── k1_allocation ─────────────────────────────────────────────────────────────

def _year_with_income():
    _insert(_txn(CHK, f"{FY}-01", "1000.00", coa="4020"),
            _txn(CHK, f"{FY}-01", "-100.00", coa="5010"))


def test_k1_allocation_pass(adb):
    from ledger_agent.core import api
    from ledger_agent.core.accounting.self_audit import check_k1_allocation

    _year_with_income()
    r = check_k1_allocation(FY, api.generate_form_1065(FY))
    assert r.status == "pass"


def test_k1_allocation_fail_when_percentages_do_not_sum_to_one(adb, monkeypatch):
    from ledger_agent.core import api
    from ledger_agent.core.accounting.self_audit import check_k1_allocation

    _year_with_income()
    monkeypatch.setenv("FI_PARTNER_1_PL", "0.60")
    r = check_k1_allocation(FY, api.generate_form_1065(FY))
    assert r.status == "fail"
    assert r.details[0]["issue"] == "percentages_do_not_sum_to_100"
    assert any(d.get("line") == "ordinary_income_loss" for d in r.details)


def test_k1_allocation_fail_when_k1_lines_do_not_tie(adb):
    from ledger_agent.core import api
    from ledger_agent.core.accounting.self_audit import check_k1_allocation

    _year_with_income()
    f = api.generate_form_1065(FY)
    f.interest_income += Decimal("5.00")
    r = check_k1_allocation(FY, f)
    assert r.status == "fail"
    assert r.details == [{"line": "interest_income", "k1_sum_minus_1065": "-5.00"}]


# ── income_consistency ────────────────────────────────────────────────────────

def _f1065(**kw):
    from ledger_agent.core.api import Form1065

    return Form1065(**{k: Decimal(v) for k, v in kw.items()})


def test_income_consistency_pass():
    from ledger_agent.core.accounting.self_audit import check_income_consistency

    f = _f1065(ordinary_business_income="900", interest_income="10",
               investment_interest_expense="4")
    r = check_income_consistency(SimpleNamespace(net_income=Decimal("906.50")), f)
    assert r.status == "pass" and r.details == []


def test_income_consistency_warn():
    from ledger_agent.core.accounting.self_audit import check_income_consistency

    f = _f1065(ordinary_business_income="900")
    r = check_income_consistency(SimpleNamespace(net_income=Decimal("950")), f)
    assert r.status == "warn"
    assert r.details[0]["balance_sheet_minus_1065"] == "50.00"


# ── run_self_audit / api.audit_year ───────────────────────────────────────────

CHECK_NAMES = [
    "statement_tieout", "account_continuity", "unknown_coa_codes", "period_coverage",
    "unclassified", "transfer_matching", "owner_draw_candidates", "trade_term_unknown",
    "k1_allocation", "income_consistency",
]


def test_audit_year_clean_full_year_passes(adb):
    from ledger_agent.core import api
    from ledger_agent.core.config import NOT_TAX_ADVICE_DISCLAIMER

    _clean_full_year()
    rep = api.audit_year(FY)
    assert set(rep) == {"fiscal_year", "status", "checks", "disclaimer"}
    assert rep["disclaimer"] == NOT_TAX_ADVICE_DISCLAIMER
    assert [c["name"] for c in rep["checks"]] == CHECK_NAMES
    statuses = {c["name"]: c["status"] for c in rep["checks"]}
    assert statuses["transfer_matching"] == "not_applicable"
    assert rep["status"] == "pass", statuses


def test_audit_year_worst_status_wins(adb):
    from ledger_agent.core import api

    _clean_full_year()
    _add_offsetting("9999")  # unclassified → warn
    assert api.audit_year(FY)["status"] == "warn"
    _add_offsetting("7777")  # unknown code → fail
    rep = api.audit_year(FY)
    assert rep["status"] == "fail"
    assert {c["name"]: c["status"] for c in rep["checks"]}["unknown_coa_codes"] == "fail"


def test_audit_year_without_data_fails(adb):
    from ledger_agent.core import api

    rep = api.audit_year(FY)
    assert rep["status"] == "fail"
    assert [c["name"] for c in rep["checks"]] == ["data_present"]


# ── customer summary flags ────────────────────────────────────────────────────

def test_summary_clean_year_is_close_ready(adb):
    from ledger_agent.core import api

    _clean_full_year()
    s = api.build_customer_summary(FY)
    assert "CLOSE_READY" in s.confidence_flags
    assert not any(f.startswith("AUDIT_") for f in s.confidence_flags)


def test_summary_audit_warn_flag_and_action(adb):
    from ledger_agent.core import api

    _clean_full_year()
    _add_offsetting("9999")
    s = api.build_customer_summary(FY)
    assert "AUDIT_WARN" in s.confidence_flags and "AUDIT_FAIL" not in s.confidence_flags
    assert any("Self-audit warnings: unclassified" in a for a in s.next_actions)


def test_summary_audit_fail_blocks_close_ready(adb):
    from ledger_agent.core import api

    _clean_full_year()
    _add_offsetting("7777")
    s = api.build_customer_summary(FY)
    assert "AUDIT_FAIL" in s.confidence_flags
    assert "CLOSE_READY" not in s.confidence_flags
    assert any(a.startswith("Self-audit FAILED: unknown_coa_codes") for a in s.next_actions)


def test_summary_runs_audit_once(adb, monkeypatch):
    from ledger_agent.core import api

    _clean_full_year()
    calls = []
    real = api.audit_year
    monkeypatch.setattr(api, "audit_year", lambda y: calls.append(y) or real(y))
    api.build_customer_summary(FY)
    assert calls == [FY]


# ── Form B: CLI ───────────────────────────────────────────────────────────────

def test_cli_audit_json_matches_api(adb, capsys):
    from ledger_agent.cli.main import cmd_audit
    from ledger_agent.core import api

    _clean_full_year()
    _add_offsetting("9999")
    capsys.readouterr()
    assert cmd_audit([str(FY), "--no-prompt"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == json.loads(json.dumps(api.audit_year(FY), default=str))
    assert out["status"] == "warn"


def test_cli_audit_fail_exit_code_and_human_table(adb, capsys):
    from ledger_agent.cli.main import _COMMANDS, cmd_audit

    _clean_full_year()
    _add_offsetting("7777")
    assert _COMMANDS["audit"][0] is cmd_audit and _COMMANDS["audit"][1] == "a"
    assert cmd_audit([str(FY), "--no-prompt"]) == 1
    capsys.readouterr()
    assert cmd_audit([str(FY)]) == 1
    text = capsys.readouterr().out
    assert "unknown_coa_codes" in text and "FAIL" in text


# ── Forms C / D: MCP tool + bridge, incl. privacy ─────────────────────────────

def _seed_leaky_year():
    """Clean year plus transfers / owner-draw candidates whose descriptions carry names
    and account references — none of it may reach the MCP / bridge response."""
    from ledger_agent.core.intelligence.owner_draws import OWNER_DRAW_CANDIDATE_TAG

    _clean_full_year()
    leaky = f"TRANSFER {ENTITY_NAME} {CHK_NAME} acct ending 6402"
    legs = _xfer_pair(CHK, CHK2)[:1] + [
        _txn(CHK, f"{FY}-02", "-77.00", coa="9000", tags=[OWNER_DRAW_CANDIDATE_TAG],
             description=leaky),
    ]
    for t in legs:
        t.description = t.raw_description = leaky
    _insert(*legs)


_NEEDLES = (ENTITY_NAME, "AUDITCASE", CHK_NAME, CHK2_NAME, "Quillbrook", "BANK_X",
            CHK_MASK[-4:], CHK2_MASK[-4:], "6402")


def _mcp(name, args, allow_pii=None):
    from ledger_agent.mcp.server import _dispatch

    params = {"name": name, "arguments": args}
    if allow_pii is not None:
        params["_meta"] = {"allow_pii": allow_pii}
    resp = _dispatch({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params})
    assert "error" not in resp and not resp["result"].get("isError")
    return resp["result"]["content"][0]["text"]


def test_mcp_audit_year_schema_and_call(adb):
    from ledger_agent.mcp.tools import TOOL_SCHEMAS

    schema = next(t for t in TOOL_SCHEMAS if t["name"] == "audit_year")
    assert schema["inputSchema"]["required"] == ["fiscal_year"]
    _clean_full_year()
    out = json.loads(_mcp("audit_year", {"fiscal_year": FY}))
    assert out["status"] == "pass"
    assert [c["name"] for c in out["checks"]] == CHECK_NAMES


def test_mcp_audit_year_default_path_has_no_names_or_account_numbers(adb):
    _seed_leaky_year()
    text = _mcp("audit_year", {"fiscal_year": FY})
    out = json.loads(text)
    statuses = {c["name"]: c["status"] for c in out["checks"]}
    # the leaky rows really are in the report (so the needle check is not vacuous)
    assert statuses["transfer_matching"] == "warn"
    assert statuses["owner_draw_candidates"] == "warn"
    folded = text.casefold()
    hits = sum(1 for n in _NEEDLES if n.casefold() in folded)
    assert hits == 0, f"{hits} name / account needle(s) present in MCP audit_year response"


def test_bridge_audit_year_has_no_names_and_matches_api(adb, capsys):
    from ledger_agent.bridge.jsonrpc_stdio import _TOOL_METHODS, _dispatch
    from ledger_agent.core import api

    assert "audit_year" in _TOOL_METHODS
    _seed_leaky_year()
    capsys.readouterr()
    _dispatch({"jsonrpc": "2.0", "id": 8, "method": "audit_year",
               "params": {"fiscal_year": FY}})
    raw = capsys.readouterr().out.strip()
    resp = json.loads(raw)
    assert "result" in resp, resp.get("error")
    a = api.audit_year(FY)
    assert resp["result"]["status"] == a["status"]
    assert ([(c["name"], c["status"]) for c in resp["result"]["checks"]]
            == [(c["name"], c["status"]) for c in a["checks"]])
    folded = raw.casefold()
    assert sum(1 for n in _NEEDLES if n.casefold() in folded) == 0
