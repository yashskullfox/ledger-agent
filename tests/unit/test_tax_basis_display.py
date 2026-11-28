"""
tests/unit/test_tax_basis_display.py  –  Card Q-01: the tax basis is visible in every form.

* Human ``ledger summary`` output carries the basis line (same wording as ``ledger tax``,
  ``renderer.pte_basis_line``) and, when the basis is not full-year, the number of
  missing account-month statements.  ``ledger summary --no-prompt`` JSON is unchanged.
* MCP ``pte_estimate`` accepts an optional ``basis`` (``"auto"`` | ``"full_year"``,
  default ``"auto"``) and passes it to ``api.pte_estimate``.  An invalid value goes
  through the existing error path with a fixed message that never echoes the input.
* The JSON-RPC bridge (Form D) forwards ``basis`` to the same tool.
* Four-form parity: CLI JSON, MCP and bridge all equal ``api.pte_estimate``.

The webapp PTE card is covered by ``webapp/.../ResultsIT.java``.

Synthetic tmp_path DBs only; pseudonymous names; no real data.
"""
from __future__ import annotations

import dataclasses
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

FY = 2026
ENTITY_ID = "ent-q01"
ACCT_A = "acct-q01-a"
ACCT_B = "acct-q01-b"
BASIS_ERROR = "basis must be 'auto' or 'full_year'"
# Synthetic, never-valid basis values.  The marker must never appear in any response.
BOGUS_MARKER = "Zephyrine Quillfeather " + "".join(str(i % 10) for i in range(4, 14))
INVALID_BASES = ["quarterly", "FULL_YEAR", "", 7, ["auto"], {"basis": "auto"}, BOGUS_MARKER]


# ── Synthetic DB ──────────────────────────────────────────────────────────────

def _account(db_file: Path, acct_id: str) -> None:
    from ledger_agent.core.database import AccountRepo
    from ledger_agent.core.models import Account, AccountType

    AccountRepo.upsert(
        Account(
            id=acct_id,
            entity_id=ENTITY_ID,
            name=f"Operating {acct_id}",
            institution="BANK_X",
            account_type=AccountType.CHECKING,
            account_number_masked="0000",
        ),
        db_file,
    )


def _month(db_file: Path, month: int, amount: str, acct_id: str, code: str = "4020") -> None:
    """One synthetic transaction plus a statement snapshot for ``FY-month``."""
    from ledger_agent.core.database import SnapshotRepo, TransactionRepo
    from ledger_agent.core.models import AccountSnapshot, Transaction, TransactionType

    period = f"{FY}-{month:02d}"
    amt = Decimal(amount)
    TransactionRepo.bulk_insert(
        [
            Transaction(
                id=f"{acct_id}-{period}-{code}",
                account_id=acct_id,
                date=date(FY, month, 10),
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
        AccountSnapshot(account_id=acct_id, statement_period=period,
                        ending_balance=Decimal("10")),
        db_file,
    )


def _new_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    # Keep Schedule K hermetic: a local (gitignored) wash-sale CSV must not change
    # full-year net STCG.
    import ledger_agent.core.accounting.wash_sale as wash_sale
    from ledger_agent.core.database import EntityRepo, init_db
    from ledger_agent.core.models import Entity

    monkeypatch.setattr(
        wash_sale, "disallowed_by_term", lambda *a, **kw: (Decimal("0"), Decimal("0"))
    )
    db_file = tmp_path / name
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    init_db(db_file)
    EntityRepo.upsert(
        Entity(id=ENTITY_ID, name="ENTITY_A", entity_type="LLC", state="MO"), db_file
    )
    _account(db_file, ACCT_A)
    _account(db_file, ACCT_B)
    return db_file


@pytest.fixture
def qtd_db(tmp_path, monkeypatch) -> Path:
    """Both accounts cover Jan–Mar; only account A has April → quarter_to_date through
    FY-03 (3/12), 1 missing account-month.  The large April amount makes the
    ``full_year`` basis differ from ``auto``."""
    db_file = _new_db(tmp_path, monkeypatch, "q01_qtd.db")
    for m in (1, 2, 3):
        _month(db_file, m, "1000", ACCT_A)
        _month(db_file, m, "-100", ACCT_B, code="5010")
    _month(db_file, 4, "90000", ACCT_A)
    return db_file


@pytest.fixture
def full_db(tmp_path, monkeypatch) -> Path:
    """All 12 months for both accounts → full_year basis."""
    db_file = _new_db(tmp_path, monkeypatch, "q01_full.db")
    for m in range(1, 13):
        _month(db_file, m, "2000", ACCT_A)
        _month(db_file, m, "-500", ACCT_B, code="5010")
    return db_file


def _flat(text: str) -> str:
    """Collapse whitespace (rich may wrap long lines in a captured console)."""
    return re.sub(r"\s+", " ", text)


def _api_pte_json(basis: str = "auto") -> dict:
    import ledger_agent.core.api as api

    return json.loads(json.dumps(dataclasses.asdict(api.pte_estimate(FY, basis=basis)),
                                 default=str))


def _mcp_expected(basis: str = "auto") -> dict:
    """``api.pte_estimate`` through the MCP serialiser (Decimal → JSON number)."""
    import ledger_agent.core.api as api
    from ledger_agent.mcp.tools import _ok

    out = json.loads(_ok(api.pte_estimate(FY, basis=basis)))
    out.pop("disclaimer", None)
    return out


def _same_figures(form: dict, api_json: dict, keys) -> None:
    """Field-by-field equality; money is compared by value (str in A/B, number in C/D)."""
    for k in keys:
        a, b = form[k], api_json[k]
        if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, str):
            assert Decimal(str(a)) == Decimal(b), k
        else:
            assert a == b, k


def _summary(capsys, *args: str) -> tuple[int, str]:
    from ledger_agent.cli.main import cmd_summary

    capsys.readouterr()
    rc = cmd_summary([str(FY), *args])
    return rc, capsys.readouterr().out


def _mcp_tool(arguments: dict) -> dict:
    from ledger_agent.mcp.tools import call_tool

    out = json.loads(call_tool("pte_estimate", arguments))
    out.pop("disclaimer", None)
    return out


def _mcp_dispatch(arguments: dict) -> tuple[str, dict]:
    import ledger_agent.mcp.server as server

    resp = server._dispatch({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "pte_estimate", "arguments": arguments},
    })
    assert resp is not None
    return json.dumps(resp, ensure_ascii=False), resp


def _bridge(capsys, params: dict) -> tuple[str, dict]:
    from ledger_agent.bridge.jsonrpc_stdio import _dispatch

    capsys.readouterr()
    _dispatch({"jsonrpc": "2.0", "id": 9, "method": "pte_estimate", "params": dict(params)})
    out = capsys.readouterr().out
    lines = [ln for ln in out.split("\n") if ln]
    assert len(lines) == 1, "bridge must write exactly one JSON-RPC frame"
    return lines[0], json.loads(lines[0])


# ── 1. Human `ledger summary` shows the basis ────────────────────────────────

def test_summary_human_shows_qtd_basis_and_missing_count(qtd_db, capsys):
    import ledger_agent.core.api as api
    from ledger_agent.core.reports.renderer import pte_basis_line, pte_missing_statement_count

    est = api.pte_estimate(FY)
    assert est.basis == "quarter_to_date"  # non-vacuous fixture
    rc, out = _summary(capsys)
    assert rc == 0
    flat = _flat(out)
    expected = f"Basis: quarter_to_date through {FY}-03 (3/12 months complete)"
    assert expected == pte_basis_line(est)  # same wording as `ledger tax`
    assert expected in flat
    assert pte_missing_statement_count(est) == 1
    assert "Missing statements: 1 account-month(s)" in flat
    # The basis line sits in the PTE / Tax block, before the confidence flags.
    assert flat.index("PTE / Tax:") < flat.index(expected) < flat.index("Confidence:")


def test_summary_human_full_year_basis_is_neutral(full_db, capsys):
    rc, out = _summary(capsys)
    assert rc == 0
    flat = _flat(out)
    assert f"Basis: full_year through {FY}-12 (12/12 months complete)" in flat
    assert "Missing statements" not in flat


@pytest.mark.parametrize("fixture", ["qtd_db", "full_db"])
def test_summary_no_prompt_json_unchanged(fixture, request, capsys):
    """JSON output is the plain CustomerSummary — no basis/missing text is added."""
    import ledger_agent.core.api as api

    request.getfixturevalue(fixture)
    rc, out = _summary(capsys, "--no-prompt")
    assert rc == 0
    cli = json.loads(out)  # stdout is JSON only
    expected = json.loads(json.dumps(dataclasses.asdict(api.build_customer_summary(FY)),
                                     default=str))
    assert cli == expected
    assert "Basis:" not in out and "Missing statements" not in out


# ── 2. MCP `pte_estimate` basis argument ──────────────────────────────────────

def test_mcp_schema_has_optional_basis_enum():
    from ledger_agent.mcp.tools import PTE_BASIS_VALUES, TOOL_SCHEMAS

    schema = next(t for t in TOOL_SCHEMAS if t["name"] == "pte_estimate")["inputSchema"]
    assert set(schema["properties"]) == {"fiscal_year", "basis"}
    basis = schema["properties"]["basis"]
    assert basis["type"] == "string"
    assert basis["enum"] == ["auto", "full_year"] == list(PTE_BASIS_VALUES)
    assert basis["default"] == "auto"
    assert schema["required"] == ["fiscal_year"]  # basis stays optional


def test_mcp_tools_list_advertises_basis():
    import ledger_agent.mcp.server as server

    resp = server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp is not None
    tool = next(t for t in resp["result"]["tools"] if t["name"] == "pte_estimate")
    assert tool["inputSchema"]["properties"]["basis"]["enum"] == ["auto", "full_year"]


def test_mcp_full_year_basis_equals_api(qtd_db):
    import ledger_agent.core.api as api

    mcp = _mcp_tool({"fiscal_year": FY, "basis": "full_year"})
    est = api.pte_estimate(FY, basis="full_year")
    assert mcp == _mcp_expected("full_year")
    assert mcp["basis"] == "full_year"
    assert Decimal(str(mcp["total_annual_tax"])) == est.total_annual_tax
    # Non-vacuous: on this DB the full-year figure differs from the auto (QTD) one.
    assert Decimal(str(mcp["total_annual_tax"])) != api.pte_estimate(FY).total_annual_tax


@pytest.mark.parametrize("arguments", [{"fiscal_year": FY}, {"fiscal_year": FY, "basis": None},
                                       {"fiscal_year": FY, "basis": "auto"}])
def test_mcp_default_basis_is_auto(qtd_db, arguments):
    mcp = _mcp_tool(arguments)
    assert mcp == _mcp_expected("auto")
    assert mcp["basis"] == "quarter_to_date"


def test_mcp_passes_basis_to_api(qtd_db, monkeypatch):
    import ledger_agent.core.api as api

    seen: list[str] = []
    real = api.pte_estimate

    def _spy(year, basis="auto"):
        seen.append(basis)
        return real(year, basis=basis)

    monkeypatch.setattr(api, "pte_estimate", _spy)
    _mcp_tool({"fiscal_year": FY, "basis": "full_year"})
    _mcp_tool({"fiscal_year": FY})
    assert seen == ["full_year", "auto"]


@pytest.mark.parametrize("bad", INVALID_BASES, ids=repr)
def test_mcp_invalid_basis_raises_fixed_message(qtd_db, bad):
    from ledger_agent.mcp.tools import call_tool

    with pytest.raises(ValueError) as excinfo:
        call_tool("pte_estimate", {"fiscal_year": FY, "basis": bad})
    assert str(excinfo.value) == BASIS_ERROR


@pytest.mark.parametrize("bad", INVALID_BASES, ids=repr)
def test_mcp_server_invalid_basis_is_client_error_without_echo(qtd_db, bad):
    text, resp = _mcp_dispatch({"fiscal_year": FY, "basis": bad})
    assert resp["result"]["isError"] is True
    assert resp["result"]["content"][0]["text"] == BASIS_ERROR
    assert "ref:" not in text
    assert BOGUS_MARKER not in text and "Zephyrine" not in text


# ── 3. Bridge (Form D) forwards basis ────────────────────────────────────────

def test_bridge_forwards_basis(qtd_db, capsys, monkeypatch):
    import ledger_agent.mcp.tools as tools

    seen: list[dict] = []
    real = tools.call_tool

    def _spy(name, args, *, allow_pii=False):
        seen.append(dict(args))
        return real(name, args, allow_pii=allow_pii)

    monkeypatch.setattr(tools, "call_tool", _spy)
    _line, resp = _bridge(capsys, {"fiscal_year": FY, "basis": "full_year"})
    assert "error" not in resp, resp.get("error")
    assert seen == [{"fiscal_year": FY, "basis": "full_year"}]
    result = resp["result"]
    assert result["basis"] == "full_year"
    _same_figures(result, _api_pte_json("full_year"),
                  ("total_annual_tax", "net_income", "through_period", "months_covered"))


def test_bridge_default_basis_is_auto(qtd_db, capsys):
    _line, resp = _bridge(capsys, {"fiscal_year": FY})
    assert "error" not in resp, resp.get("error")
    assert resp["result"]["basis"] == "quarter_to_date"
    _same_figures(resp["result"], _api_pte_json("auto"), ("total_annual_tax",))


@pytest.mark.parametrize("bad", ["quarterly", BOGUS_MARKER])
def test_bridge_invalid_basis_fails_closed(qtd_db, capsys, bad):
    line, resp = _bridge(capsys, {"fiscal_year": FY, "basis": bad})
    assert "result" not in resp
    assert resp["error"]["code"] == -32603
    assert re.fullmatch(r"Internal error \(ref:[0-9a-f]{8}\)", resp["error"]["message"])
    assert BOGUS_MARKER not in line and "Zephyrine" not in line


# ── 4. Four-form parity for both bases ───────────────────────────────────────

@pytest.mark.parametrize("fixture", ["qtd_db", "full_db"])
def test_four_form_pte_parity(fixture, request, capsys):
    """Form A (api) = Form B (CLI JSON) = Form C (MCP) = Form D (bridge) for the
    default basis, and A = C = D for ``basis="full_year"``."""
    from ledger_agent.cli.main import cmd_tax

    request.getfixturevalue(fixture)
    keys = ("basis", "through_period", "months_covered", "total_annual_tax",
            "net_income", "paid_ytd", "remaining_balance")

    api_auto = _api_pte_json("auto")
    capsys.readouterr()
    assert cmd_tax([str(FY), "--no-prompt"]) == 0
    cli_auto = json.loads(capsys.readouterr().out)
    assert cli_auto == api_auto
    _same_figures(_mcp_tool({"fiscal_year": FY}), api_auto, keys)
    _line, resp = _bridge(capsys, {"fiscal_year": FY})
    _same_figures(resp["result"], api_auto, keys)

    api_full = _api_pte_json("full_year")
    _same_figures(_mcp_tool({"fiscal_year": FY, "basis": "full_year"}), api_full, keys)
    _line, resp = _bridge(capsys, {"fiscal_year": FY, "basis": "full_year"})
    _same_figures(resp["result"], api_full, keys)
    # Non-vacuous: the four forms really carry a positive tax figure.
    assert Decimal(api_auto["total_annual_tax"]) > 0
