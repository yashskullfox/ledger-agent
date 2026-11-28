"""
tests/unit/test_cli_transfer_candidates.py  –  card C-12.

The CLI import paths (``commands.cmd_import`` and ``quick_scan.cmd_quick_scan``)
insert and classify on their own.  After classification they must run the C-03
transfer-candidate pass (``api.confirm_transfer_candidates``) once per affected
entity, so a generic ZELLE / TRANSFER candidate whose opposite leg sits in another
account of the SAME entity becomes 9000 / ``is_transfer``:

* a self-transfer between two accounts of one entity → both legs 9000;
* a third-party candidate stays a non-transfer;
* a look-alike pair that straddles two entities is never paired;
* re-running is idempotent;
* ``cmd_import`` confirms BEFORE its post-import reconcile, so the newly
  confirmed legs are reconciled (R-03 ``is_reconciled``) in the same run.

Synthetic data only: fresh per-test DB (``db`` fixture), monkeypatched statement
text and prompts, no exports written.  Pseudonymous names only.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ledger_agent.cli import commands, quick_scan
from ledger_agent.core import api
from ledger_agent.core.intelligence import memory as memory_mod
from ledger_agent.core.intelligence import owner_draws
from ledger_agent.core.intelligence.classifier import TRANSFER_CANDIDATE_TAG
from ledger_agent.core.parsers import bank_x2_checking
from ledger_agent.core.parsers.bank_x2_checking import BankX2CheckingParser

TAG = TRANSFER_CANDIDATE_TAG
NOT_INTERNAL = {"9000", "3010", "1000"}
NAMES = {"ent-a": "ENTITY_A HOLDINGS LLC", "ent-b": "ENTITY_B VENTURES LLC"}

OUT_DESC = "ZELLE TO SAVINGS"
IN_DESC = "ZELLE FROM CHECKING"
THIRD_DESC = "ZELLE FROM ALICE"


# ── synthetic statements ──────────────────────────────────────────────────────

def _x2_statement(account_no: str, credits: list[tuple[str, str, str]],
                  debits: list[tuple[str, str, str]]) -> str:
    """Synthetic Bank X2 statement text (rows: (MM/DD, description, amount))."""
    lines = [
        "ENTITY_A HOLDINGS LLC",
        f"Account Number: {account_no}",
        "Statement Period: 01/01/2025 through 01/31/2025",
        "Opening Balance $100.00",  # redaction: allow
        "Deposits and other credits",
        "Date   Description                                    Amount",
    ]
    lines += [f"{d}  {desc:<45}  {amt}" for d, desc, amt in credits]
    lines += [
        "Total deposits and other credits                      0.00",
        "Withdrawals and other debits",
        "Date   Description                                    Amount",
    ]
    lines += [f"{d}  {desc:<45}  {amt}" for d, desc, amt in debits]
    lines += [
        "Total withdrawals and other debits                    0.00",
        "Ending Balance $100.00",  # redaction: allow
    ]
    return "\n".join(lines) + "\n"


SELF_TRANSFER = {
    "a.pdf": _x2_statement(
        "111111111111",  # redaction: allow
        credits=[("01/20", THIRD_DESC, "300.00")],
        debits=[("01/10", OUT_DESC, "700.00")],
    ),
    "b.pdf": _x2_statement(
        "222222222222",  # redaction: allow
        credits=[("01/11", IN_DESC, "700.00")],
        debits=[],
    ),
}


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Fresh classification memory; no private own-account / owner-draw cues."""
    monkeypatch.setattr(
        memory_mod, "_memory", memory_mod.ClassificationMemory(tmp_path / "mem.json")
    )
    monkeypatch.setitem(bank_x2_checking._CFG, "intra_xfer_keywords", [])
    monkeypatch.setattr(owner_draws, "OWNER_DRAW_LAST4", frozenset())


@pytest.fixture
def cli(db, tmp_path, monkeypatch):
    """Drive the CLI import paths non-interactively over synthetic statements.

    Text extraction and parser detection are monkeypatched; parsing, insert,
    classification, the C-12 pass and the post-import reconcile run for real.
    Prompts: entity choice settable, re-import answer settable, every
    classification prompt skipped (→ suspense), exports declined.
    """
    from ledger_agent.cli import prompts as prompts_mod
    from ledger_agent.core.parsers.base import BaseStatementParser
    from ledger_agent.core.parsers.registry import ParserRegistry

    texts: dict[str, str] = {}
    state = SimpleNamespace(entity_name=None, reimport=False, selects=[])
    monkeypatch.setattr(
        BaseStatementParser, "extract_text", staticmethod(lambda p: texts[Path(p).name])
    )
    monkeypatch.setattr(
        ParserRegistry, "detect_or_raise", classmethod(lambda cls, t: BankX2CheckingParser)
    )

    def _select(prompt, choices, default=None):
        state.selects.append(prompt)
        if prompt == "Select entity:":
            return state.entity_name
        if prompt == "Export options:":
            return "No thanks"
        return default if default is not None else choices[0]

    skip = lambda txn, coa: None  # noqa: E731 — "[SKIP] – classify later"
    for mod in (commands, prompts_mod):
        monkeypatch.setattr(mod, "ask_select", _select)
        monkeypatch.setattr(mod, "prompt_classify", skip)
    monkeypatch.setattr(commands, "ask_confirm", lambda prompt, default=True: state.reimport)
    monkeypatch.setattr(commands, "wizard_entity", lambda: pytest.fail("setup wizard ran"))

    # quick scan's report stage is out of scope here: keep it inert.
    from ledger_agent.core.accounting import balance_sheet
    from ledger_agent.core.reports import renderer

    class _NoBS:
        def __init__(self, *a, **k):
            pass

        def build(self):
            return None

    monkeypatch.setattr(balance_sheet, "BalanceSheetBuilder", _NoBS)
    monkeypatch.setattr(renderer, "render_balance_sheet", lambda bs: None)
    monkeypatch.setattr(quick_scan, "_show_tax_estimate", lambda year: None)

    folder = tmp_path / "stmts"
    folder.mkdir()

    def _write(statements: dict[str, str]) -> list[Path]:
        paths = []
        for name, text in statements.items():
            texts[name] = text
            path = folder / name
            path.write_bytes(b"%PDF-synthetic")
            paths.append(path)
        return paths

    def run(path_kind: str, statements: dict[str, str]) -> None:
        paths = _write(statements)
        if path_kind == "cmd_import":
            for path in paths:
                commands.cmd_import(str(path))
        else:
            quick_scan.cmd_quick_scan(str(folder))

    state.run = run
    return state


def _entity(entity_id: str):
    from ledger_agent.core.database import EntityRepo
    from ledger_agent.core.models import Entity
    EntityRepo.upsert(Entity(id=entity_id, name=NAMES[entity_id], entity_type="LLC",
                             state="MO"))


def _account(acct_id: str, entity_id: str, last4: str):
    from ledger_agent.core.database import AccountRepo
    from ledger_agent.core.models import Account, AccountType
    AccountRepo.upsert(Account(
        id=acct_id, entity_id=entity_id, name=f"Acct {acct_id}",
        institution=BankX2CheckingParser.INSTITUTION,
        account_type=AccountType.CHECKING, account_number_masked=last4,
    ))


def _accounts_by_last4() -> dict[str, str]:
    from ledger_agent.core.database import get_conn
    with get_conn() as conn:
        return dict(conn.execute("SELECT account_number_masked, id FROM accounts").fetchall())


def _rows(account_id: str):
    from ledger_agent.core.database import TransactionRepo, get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE account_id=?", (account_id,)
        ).fetchall()
    return {r["description"]: TransactionRepo._row_to_model(r) for r in rows}


def _snapshot():
    from ledger_agent.core.database import get_conn
    with get_conn() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT id, coa_code, is_transfer, is_reconciled, tags "
            "FROM transactions ORDER BY id"
        ).fetchall()]


PATHS = ["cmd_import", "quick_scan"]


# ── 1. self-transfer confirmed, third party untouched ─────────────────────────

@pytest.mark.parametrize("path_kind", PATHS)
def test_self_transfer_confirmed_third_party_not(cli, path_kind):
    _entity("ent-a")
    cli.run(path_kind, SELF_TRANSFER)

    accts = _accounts_by_last4()
    a, b = _rows(accts["1111"]), _rows(accts["2222"])
    # Non-vacuous: the parser really emitted candidates for all three rows.
    for row in (a[OUT_DESC], b[IN_DESC], a[THIRD_DESC]):
        assert TAG in row.tags

    for leg in (a[OUT_DESC], b[IN_DESC]):
        assert leg.coa_code == "9000"
        assert leg.is_transfer is True

    third = a[THIRD_DESC]
    assert third.coa_code not in NOT_INTERNAL
    assert third.is_transfer is False


# ── 2. cross-entity look-alike is never paired ────────────────────────────────

@pytest.mark.parametrize("path_kind", PATHS)
def test_cross_entity_look_alike_not_paired(cli, path_kind):
    _entity("ent-a")
    _entity("ent-b")
    _account("acct-a", "ent-a", "1111")
    _account("acct-b", "ent-b", "2222")
    cli.entity_name = NAMES["ent-a"]
    cli.run(path_kind, {
        "a.pdf": _x2_statement(
            "111111111111",  # redaction: allow
            credits=[], debits=[("01/10", "ZELLE TO PARTNER_1", "700.00")],
        ),
        "b.pdf": _x2_statement(
            "222222222222",  # redaction: allow
            credits=[("01/11", "ZELLE FROM ENTITY_A", "700.00")], debits=[],
        ),
    })
    out_leg = _rows("acct-a")["ZELLE TO PARTNER_1"]
    in_leg = _rows("acct-b")["ZELLE FROM ENTITY_A"]
    for leg in (out_leg, in_leg):
        assert TAG in leg.tags
        assert leg.coa_code != "9000"
        assert leg.is_transfer is False
    assert api.confirm_transfer_candidates("ent-a") == 0
    assert api.confirm_transfer_candidates("ent-b") == 0


# ── 3. idempotent re-run ──────────────────────────────────────────────────────

@pytest.mark.parametrize("path_kind", PATHS)
def test_rerun_is_idempotent(cli, path_kind):
    _entity("ent-a")
    cli.run(path_kind, SELF_TRANSFER)
    first = _snapshot()
    assert any(row[1] == "9000" for row in first)  # the pass did promote

    cli.run(path_kind, SELF_TRANSFER)  # already imported → skipped / declined
    assert _snapshot() == first
    assert api.confirm_transfer_candidates("ent-a") == 0
    assert _snapshot() == first


def test_cmd_import_overwrite_rerun_is_idempotent(cli):
    _entity("ent-a")
    cli.run("cmd_import", SELF_TRANSFER)
    first = _snapshot()
    cli.reimport = True  # answer "Re-import and overwrite?" with yes
    cli.run("cmd_import", SELF_TRANSFER)
    assert _snapshot() == first


# ── 4. call order: classify → confirm → reconcile (cmd_import) ────────────────

def test_cmd_import_confirms_before_reconcile(cli, monkeypatch):
    """The post-import reconcile sees the promoted legs and persists them."""
    from ledger_agent.core.intelligence import reconciler

    events: list[str] = []
    seen_by_reconcile: list[dict[str, tuple[str, bool]]] = []
    real_confirm = api.confirm_transfer_candidates
    real_reconcile = reconciler.reconcile

    def _confirm(entity_id):
        events.append("confirm")
        n = real_confirm(entity_id)
        events.append("confirm-done")
        return n

    def _reconcile(txns, *args, **kwargs):
        inside_confirm = events and events[-1] == "confirm"
        if not inside_confirm:
            events.append("reconcile")
            seen_by_reconcile.append(
                {t.description: (t.coa_code, t.is_transfer) for t in txns}
            )
        return real_reconcile(txns, *args, **kwargs)

    monkeypatch.setattr(api, "confirm_transfer_candidates", _confirm)
    monkeypatch.setattr(reconciler, "reconcile", _reconcile)

    _entity("ent-a")
    cli.run("cmd_import", SELF_TRANSFER)

    # one confirm + one reconcile per imported file, confirm first each time
    assert events == ["confirm", "confirm-done", "reconcile"] * 2
    # the second file's reconcile already sees both legs as confirmed transfers
    assert seen_by_reconcile[-1][OUT_DESC] == ("9000", True)
    assert seen_by_reconcile[-1][IN_DESC] == ("9000", True)

    accts = _accounts_by_last4()
    a, b = _rows(accts["1111"]), _rows(accts["2222"])
    assert a[OUT_DESC].is_reconciled is True
    assert b[IN_DESC].is_reconciled is True
    assert a[THIRD_DESC].is_reconciled is False


def test_quick_scan_confirms_once_per_entity_after_all_files(cli, monkeypatch):
    """Both legs sit in different PDFs: the pass runs once, after every insert."""
    calls: list[tuple[str, int]] = []
    real_confirm = api.confirm_transfer_candidates

    def _confirm(entity_id):
        from ledger_agent.core.database import get_conn
        with get_conn() as conn:
            n_rows = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        calls.append((entity_id, n_rows))
        return real_confirm(entity_id)

    monkeypatch.setattr(api, "confirm_transfer_candidates", _confirm)
    _entity("ent-a")
    cli.run("quick_scan", SELF_TRANSFER)
    assert calls == [("ent-a", 3)]  # all three synthetic rows were in already


# ── 5. best-effort: a failing pass never crashes the CLI ──────────────────────

@pytest.mark.parametrize("path_kind", PATHS)
def test_pass_failure_is_logged_not_raised(cli, monkeypatch, path_kind, caplog):
    def _boom(entity_id):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(api, "confirm_transfer_candidates", _boom)
    _entity("ent-a")
    with caplog.at_level("WARNING"):
        cli.run(path_kind, SELF_TRANSFER)  # must not raise
    assert "Transfer-candidate confirmation skipped" in caplog.text

    accts = _accounts_by_last4()
    a, b = _rows(accts["1111"]), _rows(accts["2222"])
    # import itself completed; candidates simply stay unconfirmed
    assert a[OUT_DESC].is_transfer is False and b[IN_DESC].is_transfer is False
    from ledger_agent.core.database import ImportRegistry
    assert ImportRegistry.already_imported(accts["1111"], "2025-01")
    assert ImportRegistry.already_imported(accts["2222"], "2025-01")
