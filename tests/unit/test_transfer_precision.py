"""
tests/unit/test_transfer_precision.py  –  C-03 internal-transfer precision.

A bank row is an internal transfer (9000, ``is_transfer``) only when
  (a) it carries an own-account cue (private ``intra_xfer_keywords`` or a card
      payoff phrase), or
  (b) the import-time pass finds its opposite leg in ANOTHER account of the SAME
      entity (same amount, within ``MATCH_WINDOW_DAYS``).
Generic ZELLE / WIRE / TRANSFER rows without a cue are ``transfer_candidate`` rows
and go to the classifier (revenue / expense / suspense), never 9000 on wording alone.
A bare "DEPOSIT" is no longer capital (3010); an owner / capital cue is required.

Synthetic data only (fresh ``db`` fixture, monkeypatched statement text).
"""
from __future__ import annotations

import ast
import inspect
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ledger_agent.core import api
from ledger_agent.core.intelligence import memory as memory_mod
from ledger_agent.core.intelligence import owner_draws
from ledger_agent.core.intelligence.classifier import (
    TRANSFER_CANDIDATE_TAG,
    classify_transaction,
)
from ledger_agent.core.models import Transaction, TransactionType
from ledger_agent.core.parsers import bank_x2_checking, bank_x4_checking
from ledger_agent.core.parsers.bank_x2_checking import BankX2CheckingParser

TAG = TRANSFER_CANDIDATE_TAG
SUSPENSE = "9999"
NOT_INTERNAL = {"9000", "3010", "1000"}


# ── fixtures / helpers ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_memory(tmp_path, monkeypatch):
    """Isolated classification memory per test (the session file is shared)."""
    monkeypatch.setattr(
        memory_mod, "_memory", memory_mod.ClassificationMemory(tmp_path / "mem.json")
    )


@pytest.fixture
def coa(db):
    from ledger_agent.core.database import COARepo
    return COARepo.list_all()


def _txn(desc: str, amount: str, *, tags=(), ttype=None) -> Transaction:
    amt = Decimal(amount)
    if ttype is None:
        ttype = TransactionType.CREDIT if amt > 0 else TransactionType.DEBIT
    return Transaction(
        account_id="acct-a", date=date(2025, 1, 10), description=desc,
        raw_description=desc, amount=amt, transaction_type=ttype,
        statement_period="2025-01", tags=list(tags),
    )


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


@pytest.fixture
def importer(db, tmp_path, monkeypatch):
    """Run ``api.import_statements`` over synthetic Bank X2 statements.

    ``statements`` maps a file name to its statement text.  Text extraction and
    parser detection are monkeypatched; parsing, insert, classification and the
    C-03 pass run for real against the per-test DB.
    """
    from ledger_agent.core.parsers.base import BaseStatementParser
    from ledger_agent.core.parsers.registry import ParserRegistry

    texts: dict[str, str] = {}
    monkeypatch.setattr(
        BaseStatementParser, "extract_text", staticmethod(lambda p: texts[Path(p).name])
    )
    monkeypatch.setattr(ParserRegistry, "detect", classmethod(lambda cls, t: BankX2CheckingParser))

    def _run(statements: dict[str, str], folder_name: str = "stmts"):
        folder = tmp_path / folder_name
        folder.mkdir(exist_ok=True)
        for name, text in statements.items():
            texts[name] = text
            (folder / name).write_bytes(b"%PDF-synthetic")
        return api.import_statements(folder, allow_partial=True)

    return _run


def _entity(entity_id: str, name: str):
    from ledger_agent.core.database import EntityRepo
    from ledger_agent.core.models import Entity
    ent = Entity(id=entity_id, name=name, entity_type="LLC", state="MO")
    EntityRepo.upsert(ent)
    return ent


def _account(acct_id: str, entity_id: str, last4: str, institution: str = "Bank X2"):
    from ledger_agent.core.database import AccountRepo
    from ledger_agent.core.models import Account, AccountType
    AccountRepo.upsert(Account(
        id=acct_id, entity_id=entity_id, name=f"Acct {acct_id}", institution=institution,
        account_type=AccountType.CHECKING, account_number_masked=last4,
    ))


def _accounts_by_last4() -> dict[str, str]:
    from ledger_agent.core.database import get_conn
    with get_conn() as conn:
        return dict(conn.execute("SELECT account_number_masked, id FROM accounts").fetchall())


def _rows(account_id: str) -> dict[str, Transaction]:
    from ledger_agent.core.database import TransactionRepo as Repo
    from ledger_agent.core.database import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE account_id=?", (account_id,)
        ).fetchall()
    return {r["description"]: Repo._row_to_model(r) for r in rows}


def _insert(account_id: str, desc: str, amount: str, day: int, *, code: str = "",
            is_transfer: bool = False, tags=(), ttype=None) -> Transaction:
    from ledger_agent.core.database import TransactionRepo
    amt = Decimal(amount)
    if ttype is None:
        ttype = TransactionType.TRANSFER_OUT if amt < 0 else TransactionType.TRANSFER_IN
    t = Transaction(
        account_id=account_id, date=date(2025, 1, day), description=desc,
        raw_description=desc, amount=amt, transaction_type=ttype,
        statement_period="2025-01", coa_code=code,
        coa_name="Inter-Account Transfer" if code == "9000" else code,
        tags=list(tags), is_transfer=is_transfer,
    )
    TransactionRepo.bulk_insert([t])
    return t


def _get(tx_id: str) -> Transaction:
    from ledger_agent.core.database import TransactionRepo
    got = TransactionRepo.get_by_id(tx_id)
    assert got is not None
    return got


# ── 1. parsers: generic rails are candidates, own-account cues stay internal ──

class TestParserCandidates:
    @pytest.mark.parametrize("desc,is_debit", [
        ("ZELLE FROM ALICE", False),
        ("Zelle Credit from ALICE", False),
        ("WIRE OUT ACME HOLDINGS", True),
        ("TRANSFER TO SAVINGS", True),
        ("Online Banking transfer from savings", False),
    ])
    def test_bank_x2_generic_rail_is_candidate(self, desc, is_debit):
        [t] = BankX2CheckingParser()._parse_lines(
            f"01/10  {desc:<40}  500.00\n", 2025, is_debit=is_debit
        )
        assert t.is_transfer is False
        assert t.tags == [TAG]
        assert t.coa_code == ""
        # the rail stays visible in the type (the reconciler needs it)
        assert t.transaction_type in (TransactionType.TRANSFER_IN, TransactionType.TRANSFER_OUT)

    def test_bank_x2_card_payoff_stays_internal(self):
        [t] = BankX2CheckingParser()._parse_lines(
            "01/10  TRANSFER TO CREDIT CARD                    500.00\n", 2025, is_debit=True
        )
        assert t.is_transfer is True
        assert TAG not in t.tags

    def test_bank_x2_private_intra_keyword_is_internal(self, monkeypatch):
        monkeypatch.setitem(bank_x2_checking._CFG, "intra_xfer_keywords", ["SYNTH OWN XFER"])
        [t] = BankX2CheckingParser()._parse_lines(
            "01/10  ZELLE SYNTH OWN XFER                       500.00\n", 2025, is_debit=True
        )
        assert t.is_transfer is True
        assert TAG not in t.tags

    def test_bank_x2_non_rail_rows_untagged(self):
        [t] = BankX2CheckingParser()._parse_lines(
            "01/10  CLIENT PAYMENT                             500.00\n", 2025, is_debit=False
        )
        assert t.is_transfer is False and t.tags == []

    def _x4(self, line: str, is_debit: bool) -> Transaction:
        header, footer = (
            ("Other Withdrawals", "Total Other Withdrawals") if is_debit
            else ("Other Deposits", "Total Other Deposits")
        )
        [t] = bank_x4_checking.BankX4CheckingParser()._parse_section(
            [header, line, footer], 2026, "2026-01", is_debit=is_debit
        )
        return t

    def test_bank_x4_card_payoff_unchanged(self):
        t = self._x4("Jan16Internet Banking PaymentTo Credit Card$300.00-", is_debit=True)  # redaction: allow (synthetic)
        assert t.transaction_type == TransactionType.TRANSFER_OUT
        assert t.is_transfer is True
        assert TAG not in t.tags

    def test_bank_x4_transfer_to_credit_card_unchanged(self):
        t = self._x4("Jan16Transfer To Credit Card$300.00-", is_debit=True)  # redaction: allow (synthetic)
        assert t.is_transfer is True
        assert TAG not in t.tags

    def test_bank_x4_generic_transfer_is_candidate(self):
        t = self._x4("Jan15Internet Banking Transfer To Savings$500.00-", is_debit=True)  # redaction: allow (synthetic)
        assert t.transaction_type == TransactionType.TRANSFER_OUT
        assert t.is_transfer is False
        assert t.tags == [TAG]

    def test_bank_x4_ext_tfr_deposit_unchanged(self):
        t = self._x4("Jan15Ext Tfr Deposit$500.00", is_debit=False)  # redaction: allow (synthetic)
        assert t.transaction_type == TransactionType.CREDIT
        assert t.is_transfer is False
        assert t.tags == []

    def test_bank_x4_private_intra_keyword_is_internal(self, monkeypatch):
        monkeypatch.setitem(bank_x4_checking._CFG, "intra_xfer_keywords", ["SYNTH OWN XFER"])
        t = self._x4("Jan15Transfer SYNTH OWN XFER$500.00-", is_debit=True)  # redaction: allow (synthetic)
        assert t.is_transfer is True
        assert TAG not in t.tags


# ── 2. classifier: candidates never become 9000 on wording alone ─────────────

class TestClassifierCandidates:
    def test_third_party_zelle_in_is_not_internal(self, coa):
        t = classify_transaction(_txn("ZELLE FROM ALICE", "300", tags=[TAG]), coa)
        assert t.coa_code not in NOT_INTERNAL
        assert t.coa_code == SUSPENSE  # no vendor rule: suspense for review
        assert t.is_transfer is False

    def test_third_party_wire_out_is_not_internal(self, coa):
        t = classify_transaction(_txn("WIRE OUT ACME HOLDINGS", "-450", tags=[TAG]), coa)
        assert t.coa_code not in NOT_INTERNAL
        assert t.is_transfer is False

    def test_candidate_vendor_rule_decides(self, coa):
        # rail words are stripped for the backend, so the vendor rule fires
        t = classify_transaction(_txn("WIRE OUT ACME LAW LLP", "-450", tags=[TAG]), coa)
        assert t.coa_code == "5071"
        assert t.is_transfer is False

    def test_zelle_business_payment_candidate_not_internal(self, coa):
        t = classify_transaction(
            _txn("ZELLE BUSINESS PAYMENT TO BOB", "-200", tags=[TAG]), coa
        )
        assert t.coa_code not in NOT_INTERNAL

    def test_candidate_ignores_transfer_memory(self, coa):
        memory_mod.get_memory().remember("ZELLE FROM ALICE", "9000", "Inter-Account Transfer",
                                         True)
        t = classify_transaction(_txn("ZELLE FROM ALICE", "300", tags=[TAG]), coa)
        assert t.coa_code != "9000" and t.is_transfer is False

    def test_candidate_ignores_backend_transfer_verdict(self, coa, monkeypatch):
        from ledger_agent.core.intelligence import ai_backend

        class _Fake:
            backend_name = "fake"

            def classify_transaction(self, description, amount):
                return {"coa_code": "9000", "coa_name": "Inter-Account Transfer",
                        "is_transfer": True, "confidence": 0.99}

        monkeypatch.setattr(ai_backend, "get_backend", lambda: _Fake())
        t = classify_transaction(_txn("ZELLE FROM ALICE", "300", tags=[TAG]), coa)
        assert t.coa_code != "9000" and t.is_transfer is False
        legacy = classify_transaction(_txn("ZELLE FROM ALICE", "300"), coa)
        assert legacy.coa_code == "9000"

    def test_candidate_result_not_remembered(self, coa):
        classify_transaction(_txn("WIRE OUT ACME LAW LLP", "-450", tags=[TAG]), coa)
        assert memory_mod.get_memory().lookup("WIRE OUT ACME LAW LLP") is None

    def test_own_account_term_still_internal_for_candidate(self, coa):
        t = classify_transaction(_txn("PAYMENT TO CREDIT CARD", "-300", tags=[TAG]), coa)
        assert t.coa_code == "9000" and t.is_transfer is True

    def test_untagged_generic_rail_is_not_internal(self, coa):
        # C-13: an untagged row is no longer internal on ZELLE / TRANSFER wording
        # alone (was 9000 through the local backend's generic rail rules)
        t = classify_transaction(_txn("ZELLE TO RECIPIENT", "-300"), coa)
        assert t.coa_code != "9000" and t.is_transfer is False


# ── 3. DEPOSIT → 3010 needs an owner / capital cue ───────────────────────────

class TestDepositRule:
    def test_bare_deposit_is_not_capital(self, coa):
        t = classify_transaction(_txn("DEPOSIT", "1000"), coa)
        assert t.coa_code != "3010"
        assert t.coa_code.startswith("4") or t.coa_code == SUSPENSE

    @pytest.mark.parametrize("desc", [
        "OWNER CAPITAL CONTRIBUTION",
        "CAPITAL CONTRIBUTION",
        "MEMBER CONTRIBUTION",
        "PARTNER CAPITAL DEPOSIT",
        "OWNER DEPOSIT",
    ])
    def test_owner_capital_cue_is_capital(self, coa, desc):
        t = classify_transaction(_txn(desc, "1000"), coa)
        assert t.coa_code == "3010"

    def test_capital_cue_on_outflow_is_not_contribution(self, coa):
        t = classify_transaction(_txn("OWNER CAPITAL CONTRIBUTION", "-1000"), coa)
        assert t.coa_code != "3010"

    def test_cue_needs_word_boundary(self, coa):
        t = classify_transaction(_txn("COWNER DEPOSITORY SERVICES", "1000"), coa)
        assert t.coa_code != "3010"


# ── 4. import-time opposite-leg pass ─────────────────────────────────────────

class TestImportPass:
    def test_self_transfer_and_third_party(self, importer):
        report = importer({
            "a.pdf": _x2_statement(
                "111111111111",  # redaction: allow
                credits=[("01/20", "ZELLE FROM ALICE", "300.00")],
                debits=[
                    ("01/10", "ZELLE TRANSFER TO ENTITY SAVINGS", "700.00"),
                    ("01/15", "WIRE OUT ACME HOLDINGS", "450.00"),
                    ("01/18", "PAYMENT TO CREDIT CARD", "90.00"),
                ],
            ),
            "b.pdf": _x2_statement(
                "222222222222",  # redaction: allow
                credits=[("01/12", "ZELLE TRANSFER FROM ENTITY CHECKING", "700.00")],
                debits=[],
            ),
        })
        assert report.imported == 2
        accts = _accounts_by_last4()
        a, b = _rows(accts["1111"]), _rows(accts["2222"])

        # self-transfer between two accounts of the entity → both legs internal
        out_leg = a["ZELLE TRANSFER TO ENTITY SAVINGS"]
        in_leg = b["ZELLE TRANSFER FROM ENTITY CHECKING"]
        for leg in (out_leg, in_leg):
            assert leg.coa_code == "9000"
            assert leg.is_transfer is True

        # third-party Zelle in / wire out → not internal
        for desc in ("ZELLE FROM ALICE", "WIRE OUT ACME HOLDINGS"):
            assert a[desc].coa_code not in NOT_INTERNAL, desc
            assert a[desc].is_transfer is False, desc
            assert TAG in a[desc].tags

        # card payoff → still internal on its own cue
        assert a["PAYMENT TO CREDIT CARD"].coa_code == "9000"
        assert a["PAYMENT TO CREDIT CARD"].is_transfer is True

    def test_cross_entity_look_alike_is_not_internal(self, importer):
        _entity("ent-a", "ENTITY_A")
        _entity("ent-b", "ENTITY_B")
        _account("acct-a", "ent-a", "1111")
        _account("acct-b", "ent-b", "2222")
        importer({
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
            assert leg.coa_code != "9000"
            assert leg.is_transfer is False

    def test_outside_window_or_amount_is_not_matched(self, importer):
        importer({
            "a.pdf": _x2_statement(
                "111111111111",  # redaction: allow
                credits=[], debits=[
                    ("01/01", "ZELLE TO SAVINGS", "700.00"),
                    ("01/05", "WIRE OUT SAVINGS", "650.00"),
                ],
            ),
            "b.pdf": _x2_statement(
                "222222222222",  # redaction: allow
                credits=[
                    ("01/20", "ZELLE FROM CHECKING", "700.00"),   # 19 days later
                    ("01/06", "WIRE IN CHECKING", "600.00"),      # different amount
                ],
                debits=[],
            ),
        })
        from ledger_agent.core.database import get_conn
        with get_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE coa_code='9000' OR is_transfer=1"
            ).fetchone()[0]
        assert n == 0

    def test_reimport_is_idempotent(self, importer):
        stmts = {
            "a.pdf": _x2_statement(
                "111111111111",  # redaction: allow
                credits=[("01/20", "ZELLE FROM ALICE", "300.00")],
                debits=[("01/10", "ZELLE TO SAVINGS", "700.00")],
            ),
            "b.pdf": _x2_statement(
                "222222222222",  # redaction: allow
                credits=[("01/11", "ZELLE FROM CHECKING", "700.00")], debits=[],
            ),
        }
        importer(stmts)
        from ledger_agent.core.database import get_conn

        def snapshot():
            with get_conn() as conn:
                return conn.execute(
                    "SELECT id, coa_code, is_transfer, tags FROM transactions ORDER BY id"
                ).fetchall()

        first = [tuple(r) for r in snapshot()]
        second_report = importer(stmts)
        assert second_report.skipped == 2
        assert [tuple(r) for r in snapshot()] == first

        from ledger_agent.core.database import EntityRepo
        [ent] = EntityRepo.list_all()
        assert api._confirm_transfer_candidates(ent.id) == 0
        assert [tuple(r) for r in snapshot()] == first

    def test_owner_draw_listed_destination_still_3040(self, importer, monkeypatch):
        monkeypatch.setattr(owner_draws, "OWNER_DRAW_LAST4", frozenset({"0000"}))
        importer({
            "a.pdf": _x2_statement(
                "111111111111",  # redaction: allow
                credits=[], debits=[("01/10", "ZELLE TO ACCT x0000", "700.00")],
            ),
            "b.pdf": _x2_statement(
                "222222222222",  # redaction: allow
                credits=[("01/11", "ZELLE FROM CHECKING", "700.00")], debits=[],
            ),
        })
        accts = _accounts_by_last4()
        draw = _rows(accts["1111"])["ZELLE TO ACCT x0000"]
        assert draw.coa_code == "3040"
        assert draw.is_transfer is False


class TestConfirmPassUnit:
    @pytest.fixture
    def three_accounts(self, db):
        _entity("ent-a", "ENTITY_A")
        for acct_id, last4 in (("chk", "1111"), ("card", "2222"), ("sav", "3333")):
            _account(acct_id, "ent-a", last4)

    def test_existing_pair_is_not_stolen(self, three_accounts):
        # card payoff + card-side payment already pair with each other
        _insert("chk", "PAYMENT TO CREDIT CARD", "-300", 10, code="9000", is_transfer=True)
        _insert("card", "PAYMENT THANK YOU", "300", 10, code="9000", is_transfer=True)
        # a third-party Zelle in of the same amount in a third account
        cand = _insert("sav", "ZELLE FROM ALICE", "300", 10, code=SUSPENSE, tags=[TAG])
        assert api._confirm_transfer_candidates("ent-a") == 0
        assert _get(cand.id).coa_code == SUSPENSE

    def test_candidate_pairs_with_unpaired_existing_transfer(self, three_accounts):
        _insert("chk", "SYNTH OWN XFER OUT", "-400", 10, code="9000", is_transfer=True,
                ttype=TransactionType.DEBIT)  # some stored 9000 rows are typed debit
        cand = _insert("sav", "ZELLE FROM CHECKING", "400", 12, code=SUSPENSE, tags=[TAG])
        assert api._confirm_transfer_candidates("ent-a") == 1
        got = _get(cand.id)
        assert got.coa_code == "9000" and got.is_transfer is True
        assert TAG in got.tags  # provenance kept

    def test_promotion_clears_owner_draw_candidate_tag(self, three_accounts):
        od_tag = owner_draws.OWNER_DRAW_CANDIDATE_TAG
        out = _insert("chk", "ZELLE TO SAVINGS", "-250", 10, code="5999", tags=[TAG, od_tag])
        inc = _insert("sav", "ZELLE FROM CHECKING", "250", 11, code=SUSPENSE, tags=[TAG])
        assert api._confirm_transfer_candidates("ent-a") == 2
        assert od_tag not in _get(out.id).tags
        assert _get(out.id).tags == [TAG]
        assert _get(inc.id).is_transfer is True

    def test_same_account_legs_are_not_internal(self, three_accounts):
        _insert("chk", "ZELLE TO BOB", "-250", 10, code="5999", tags=[TAG])
        _insert("chk", "ZELLE FROM BOB", "250", 11, code=SUSPENSE, tags=[TAG])
        assert api._confirm_transfer_candidates("ent-a") == 0

    def test_owner_draw_never_promoted(self, three_accounts):
        _insert("chk", "ZELLE TO ACCT x0000", "-250", 10, code="3040", tags=[TAG])
        _insert("sav", "ZELLE FROM CHECKING", "250", 11, code=SUSPENSE, tags=[TAG])
        assert api._confirm_transfer_candidates("ent-a") == 0

    def test_other_entity_rows_not_read(self, three_accounts):
        _entity("ent-b", "ENTITY_B")
        _account("other", "ent-b", "4444")
        cand = _insert("chk", "ZELLE TO PARTNER_1", "-250", 10, code="5999", tags=[TAG])
        _insert("other", "ZELLE FROM ENTITY_A", "250", 10, code=SUSPENSE, tags=[TAG])
        assert api._confirm_transfer_candidates("ent-a") == 0
        assert api._confirm_transfer_candidates("ent-b") == 0
        assert _get(cand.id).is_transfer is False


_PASS_NAMES = {"_confirm_transfer_candidates", "confirm_transfer_candidates"}

# (file relative to the repo root, enclosing function) of every allowed call site.
_ALLOWED_PASS_CALLERS = {
    ("ledger_agent/core/api.py", "import_statements"),
    ("ledger_agent/core/api.py", "confirm_transfer_candidates"),  # C-12 public wrapper
    ("ledger_agent/cli/commands.py", "cmd_import"),               # C-12
    ("ledger_agent/cli/quick_scan.py", "cmd_quick_scan"),         # C-12
}


def _pass_call_sites(path: Path, rel: str) -> set[tuple[str, str]]:
    """(rel, enclosing function) for every call of the pass found in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sites: set[tuple[str, str]] = set()

    def _visit(node: ast.AST, func: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        if isinstance(node, ast.Call):
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else getattr(
                callee, "id", None
            )
            if name in _PASS_NAMES:
                sites.add((rel, func))
        for child in ast.iter_child_nodes(node):
            _visit(child, func)

    _visit(tree, "<module>")
    return sites


def test_pass_runs_only_from_import_paths():
    """The writer runs only from import paths (C-03 api import, C-12 CLI import /
    quick scan), never from read / report paths: every call site in the product
    code must be in the allow-list, and every allowed site must really call it."""
    root = Path(api.__file__).resolve().parents[2]
    files = [
        *sorted((root / "ledger_agent").rglob("*.py")),
        *sorted((root / "mcp_server").rglob("*.py")),
        *sorted((root / "adapters").rglob("*.py")),
        root / "main.py",
    ]
    sites: set[tuple[str, str]] = set()
    for path in files:
        if path.is_file():
            sites |= _pass_call_sites(path, path.relative_to(root).as_posix())
    assert sites == _ALLOWED_PASS_CALLERS
    # The CLI calls the public wrapper, never the private implementation.
    for mod in ("commands.py", "quick_scan.py"):
        src = (root / "ledger_agent" / "cli" / mod).read_text(encoding="utf-8")
        assert "_confirm_transfer_candidates(" not in src
    # The public wrapper delegates to the one implementation.
    assert "_confirm_transfer_candidates(" in inspect.getsource(api.confirm_transfer_candidates)


def test_call_site_scan_detects_a_report_path_caller(tmp_path):
    """Guard for the guard: a report function calling the pass is reported."""
    bad = tmp_path / "report.py"
    bad.write_text(
        "from ledger_agent.core import api\n"
        "def generate_report(eid):\n"
        "    api.confirm_transfer_candidates(eid)\n",
        encoding="utf-8",
    )
    assert _pass_call_sites(bad, "x/report.py") == {("x/report.py", "generate_report")}
