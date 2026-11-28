"""
tests/unit/test_generic_transfer_paths.py  –  C-13 remaining generic transfer paths.

C-03 made Bank X2 / X4 rows internal only on an own-account cue.  C-13 closes the
remaining "any ZELLE / TRANSFER is internal" paths:

* ``bank_x3_checking``: ZELLE / TRANSFER rows are internal only on the private
  ``intra_xfer_keywords`` or a card-payoff cue; otherwise ``transfer_candidate``.
* ``bank_x_checking``: debit and credit rails (ZELLE / WIRE / TRANSFER) are tagged
  ``transfer_candidate`` unless they carry the private keywords or a card-payoff cue.
* ``local_backend``: the generic ZELLE / WIRE / "TRANSFER TO|FROM" → 9000 rules need
  an own-account marker (card payoff); wording alone no longer makes an untagged
  row internal.

Candidates become 9000 only through the import-time opposite-leg pass.
Synthetic data only (fresh ``db`` fixture, monkeypatched statement text).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ledger_agent.core import api
from ledger_agent.core.intelligence import memory as memory_mod
from ledger_agent.core.intelligence.ai_backend.local_backend import LocalBackend
from ledger_agent.core.intelligence.classifier import (
    TRANSFER_CANDIDATE_TAG,
    classify_transaction,
)
from ledger_agent.core.models import Transaction, TransactionType
from ledger_agent.core.parsers import bank_x3_checking, bank_x_checking
from ledger_agent.core.parsers.bank_x3_checking import BankX3CheckingParser
from ledger_agent.core.parsers.bank_x_checking import BankXCheckingParser

TAG = TRANSFER_CANDIDATE_TAG
NOT_INTERNAL = {"9000", "3010", "1000"}
SYNTH_KW = "SYNTH OWN XFER"


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


# ── synthetic statements ──────────────────────────────────────────────────────

def _bank_x_statement(account_no: str, debits: list[tuple[str, str, str]],
                      credits: list[tuple[str, str, str]]) -> str:
    """Synthetic Bank X statement text (rows: (MM/DD, description, amount))."""
    lines = [
        "SYNTH-BANK: X",
        "ENTITY_A HOLDINGS LLC",
        f"CHECKING {account_no}",
        "For 01/31/2025",
        "Other withdrawals, debits and service charges",
        *[f"{d} {desc} {amt}" for d, desc, amt in debits],
        "Total other withdrawals, debits and service charges",
        "Deposits, credits and interest",
        *[f"{d} {desc} {amt}" for d, desc, amt in credits],
        "Total deposits, credits and interest",
    ]
    return "\n".join(lines) + "\n"


def _bank_x3_statement(account_no: str, credits: list[tuple[str, str, str]],
                       debits: list[tuple[str, str, str]]) -> str:
    """Synthetic Bank X3 statement text (rows: (MM/DD, description, amount))."""
    lines = [
        "SYNTH-BANK: X3",
        "ENTITY_A HOLDINGS LLC",
        f"Account Number: {account_no}",
        "January 01 - January 31, 2025",
        "DEPOSITS AND ADDITIONS",
        "DATE        DESCRIPTION                                 AMOUNT",
        *[f"{d}       {desc:<40}  {amt}" for d, desc, amt in credits],
        "Total Deposits",
        "ELECTRONIC WITHDRAWALS",
        "DATE        DESCRIPTION                                 AMOUNT",
        *[f"{d}       {desc:<40}  {amt}" for d, desc, amt in debits],
        "Total Electronic",
    ]
    return "\n".join(lines) + "\n"


@pytest.fixture
def importer(db, tmp_path, monkeypatch):
    """Run ``api.import_statements`` over synthetic Bank X / X3 statements.

    Text extraction and parser detection are monkeypatched (the ``SYNTH-BANK``
    marker picks the parser); parsing, insert, classification and the C-03 pass
    run for real against the per-test DB.
    """
    from ledger_agent.core.parsers.base import BaseStatementParser
    from ledger_agent.core.parsers.registry import ParserRegistry

    texts: dict[str, str] = {}
    monkeypatch.setattr(
        BaseStatementParser, "extract_text", staticmethod(lambda p: texts[Path(p).name])
    )

    def _detect(cls, text):
        return BankX3CheckingParser if "SYNTH-BANK: X3" in text else BankXCheckingParser

    monkeypatch.setattr(ParserRegistry, "detect", classmethod(_detect))

    def _run(statements: dict[str, str]):
        folder = tmp_path / "stmts"
        folder.mkdir(exist_ok=True)
        for name, text in statements.items():
            texts[name] = text
            (folder / name).write_bytes(b"%PDF-synthetic")
        return api.import_statements(folder, allow_partial=True)

    return _run


def _rows_by_institution() -> dict[str, dict[str, Transaction]]:
    from ledger_agent.core.database import TransactionRepo, get_conn
    out: dict[str, dict[str, Transaction]] = {}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT t.*, a.institution AS inst FROM transactions t"
            " JOIN accounts a ON a.id = t.account_id"
        ).fetchall()
    for r in rows:
        out.setdefault(r["inst"], {})[r["description"]] = TransactionRepo._row_to_model(r)
    return out


def _x3_line(desc: str, is_debit: bool) -> Transaction:
    [t] = BankX3CheckingParser()._parse_section(
        f"HDR\n01/10       {desc:<40}  500.00\nEND\n", "HDR", "END", 2025, is_debit=is_debit
    )
    return t


def _x_debit(desc: str) -> Transaction:
    [t] = BankXCheckingParser()._parse_debits(
        _bank_x_statement("1111111111111", [("01/10", desc, "500.00")], []), 2025
    )
    return t


def _x_credit(desc: str) -> Transaction:
    [t] = BankXCheckingParser()._parse_credits(
        _bank_x_statement("1111111111111", [], [("01/10", desc, "500.00")]), 2025
    )
    return t


# ── 1. Bank X3 parser (C-03 candidate change) ─────────────────────────────────

class TestBankX3Parser:
    @pytest.mark.parametrize("desc,is_debit", [
        ("Zelle To CHARLIE", True),
        ("Zelle payment from BOB", False),
        ("Online Transfer from savings", False),
        ("ONLINE TRANSFER TO SAVINGS", True),
    ])
    def test_generic_rail_is_candidate(self, desc, is_debit):
        t = _x3_line(desc, is_debit)
        assert t.is_transfer is False
        assert t.tags == [TAG]
        assert t.coa_code == ""
        assert t.transaction_type in (TransactionType.TRANSFER_IN, TransactionType.TRANSFER_OUT)

    def test_private_intra_keyword_is_internal(self, monkeypatch):
        monkeypatch.setitem(bank_x3_checking._CFG, "intra_xfer_keywords", [SYNTH_KW])
        t = _x3_line(f"ONLINE TRANSFER {SYNTH_KW}", is_debit=True)
        assert t.is_transfer is True
        assert TAG not in t.tags

    def test_card_payoff_is_internal(self):
        t = _x3_line("ONLINE TRANSFER TO CREDIT CARD", is_debit=True)
        assert t.is_transfer is True
        assert TAG not in t.tags

    def test_non_rail_rows_untagged(self):
        t = _x3_line("RECURRING DEBIT CARD QUICKBOOKS", is_debit=True)
        assert t.is_transfer is False and t.tags == []

    def test_missing_keyword_config_is_safe(self, monkeypatch):
        monkeypatch.setattr(bank_x3_checking, "_CFG", {"detect": []})
        t = _x3_line("Zelle To CHARLIE", is_debit=True)
        assert t.tags == [TAG] and t.is_transfer is False


# ── 2. Bank X parser: debit and credit rails are candidates ───────────────────

class TestBankXParser:
    @pytest.mark.parametrize("desc", [
        "ONLINE TRANSFER TO SAVINGS",
        "TRANSFER TO PARTNER_1",
        "ZELLE TO CHARLIE",
        "WIRE OUT ACME HOLDINGS",
        "TRANSFER * SYNTH SAVINGS",
    ])
    def test_debit_rail_is_candidate(self, desc):
        t = _x_debit(desc)
        assert t.amount < 0
        assert t.transaction_type == TransactionType.TRANSFER_OUT
        assert t.is_transfer is False
        assert t.tags == [TAG]
        assert t.coa_code == ""

    @pytest.mark.parametrize("desc", ["ZELLE FROM ALICE", "WIRE IN SYNTH CO",
                                      "ONLINE TRANSFER FROM SAVINGS"])
    def test_credit_rail_is_candidate(self, desc):
        t = _x_credit(desc)
        assert t.transaction_type == TransactionType.TRANSFER_IN
        assert t.is_transfer is False
        assert t.tags == [TAG]

    def test_intra_keyword_credit_still_internal(self, monkeypatch):
        monkeypatch.setitem(bank_x_checking._CFG, "intra_xfer_keywords", [SYNTH_KW])
        t = _x_credit(f"{SYNTH_KW} FID BKG")
        assert t.transaction_type == TransactionType.TRANSFER_IN
        assert t.is_transfer is True and t.tags == []

    def test_intra_keyword_debit_is_internal(self, monkeypatch):
        monkeypatch.setitem(bank_x_checking._CFG, "intra_xfer_keywords", [SYNTH_KW])
        t = _x_debit(f"{SYNTH_KW} FID BKG")
        assert t.transaction_type == TransactionType.TRANSFER_OUT
        assert t.is_transfer is True and t.tags == []

    def test_card_payoff_is_internal(self):
        t = _x_debit("ONLINE TRANSFER TO CREDIT CARD")
        assert t.is_transfer is True and t.tags == []

    @pytest.mark.parametrize("desc,ttype", [
        ("POS SYNTH WIRELINE PHONE", TransactionType.DEBIT),
        ("WIRED MAGAZINE SUBSCRIPTION", TransactionType.DEBIT),
        ("WIRE TRANSFER FEE", TransactionType.DEBIT),
        ("TRAN FEE", TransactionType.FEE),
        ("IRS USATAXPYMT", TransactionType.TAX),
        ("DEBIT CARD RECURRING PYMT INCFILE", TransactionType.DEBIT),
    ])
    def test_non_rail_debits_unchanged(self, desc, ttype):
        t = _x_debit(desc)
        assert t.transaction_type == ttype
        assert t.is_transfer is False and t.tags == []

    def test_plain_credit_unchanged(self):
        t = _x_credit("CLIENT PAYMENT SYNTH CO")
        assert t.transaction_type == TransactionType.CREDIT
        assert t.is_transfer is False and t.tags == []


# ── 3. local backend: wording alone is no longer internal ────────────────────

class TestLocalRules:
    @pytest.mark.parametrize("desc", [
        "TRANSFER TO SAVINGS",
        "TRANSFER INTO SAVINGS",
        "TRANSFER IN FROM SAVINGS",
        "ZELLE TO RECIPIENT",
        "ZELLE FROM ALICE",
        "WIRE INCOMING",
        "WIRE TRANSFER OUTGOING",
        "WIRE OUT ACME HOLDINGS",
    ])
    @pytest.mark.parametrize("amount", [-300.0, 300.0])
    def test_generic_wording_is_not_internal(self, desc, amount):
        result = LocalBackend().classify_transaction(desc, amount)
        assert result["coa_code"] != "9000"
        assert result["is_transfer"] is False

    @pytest.mark.parametrize("desc", [
        "TRANSFER TO CREDIT CARD",
        "ONLINE TRANSFER FROM CREDIT CARD",
        "PAYMENT TO CREDIT CARD",
        "TRANSFERTO CREDITCARD",
    ])
    def test_card_payoff_still_internal(self, desc):
        result = LocalBackend().classify_transaction(desc, -300.0)
        assert result["coa_code"] == "9000" and result["is_transfer"] is True

    def test_vendor_rule_now_sees_the_payee(self):
        # was 9000 via "ZELLE TO"; the C-01 vendor rule now decides
        assert LocalBackend().classify_transaction("ZELLE TO ACME LAW", -300.0)[
            "coa_code"] == "5071"

    def test_wire_fee_is_a_bank_fee(self):
        assert LocalBackend().classify_transaction("WIRE FEE", -30.0)["coa_code"] == "5020"

    def test_privacy_vocabulary_keeps_rail_words(self):
        # core/privacy.py exempts the local rules' words from name detection
        from ledger_agent.core.privacy import _local_vendor_vocabulary
        vocab = _local_vendor_vocabulary()
        for word in ("zelle", "wire", "transfer", "incoming", "outgoing", "into", "out"):
            assert word in vocab


# ── 4. classifier on untagged / tagged rows ───────────────────────────────────

def _txn(desc: str, amount: str, *, tags=()) -> Transaction:
    from datetime import date
    from decimal import Decimal
    amt = Decimal(amount)
    return Transaction(
        account_id="acct-a", date=date(2025, 1, 10), description=desc,
        raw_description=desc, amount=amt,
        transaction_type=TransactionType.CREDIT if amt > 0 else TransactionType.DEBIT,
        statement_period="2025-01", tags=list(tags),
    )


class TestClassifier:
    @pytest.mark.parametrize("desc,amount", [
        ("ZELLE TO RECIPIENT", "-300"),
        ("TRANSFER TO SAVINGS", "-300"),
        ("WIRE INCOMING", "300"),
    ])
    def test_untagged_generic_rail_not_internal(self, coa, desc, amount):
        t = classify_transaction(_txn(desc, amount), coa)
        assert t.coa_code not in NOT_INTERNAL
        assert t.is_transfer is False

    @pytest.mark.parametrize("tags", [(), (TAG,)])
    @pytest.mark.parametrize("desc", [
        "PAYMENT TO CREDIT CARD",
        "INTERNET PAYMENT ACH TRANSF BROKERAGE",
        "ACH CORP DEBIT TRANSFER SYNTH",
        "BKG SVC LLC Z SWEEP",
    ])
    def test_card_payoff_and_sweep_unchanged(self, coa, desc, tags):
        t = classify_transaction(_txn(desc, "-300", tags=tags), coa)
        assert t.coa_code == "9000" and t.is_transfer is True


# ── 5. import_statements end to end ───────────────────────────────────────────

class TestImport:
    def test_self_transfer_across_bank_x_and_x3(self, importer):
        report = importer({
            "x.pdf": _bank_x_statement(
                "1111111111111",  # redaction: allow
                debits=[
                    ("01/10", "ONLINE TRANSFER TO SAVINGS", "700.00"),
                    ("01/15", "TRANSFER TO PARTNER_1", "450.00"),
                    ("01/16", "ZELLE TO ACME LAW", "120.00"),
                ],
                credits=[("01/20", "ZELLE FROM ALICE", "300.00")],
            ),
            "x3.pdf": _bank_x3_statement(
                "22223333",  # redaction: allow
                credits=[("01/11", "Online Transfer from checking", "700.00")],
                debits=[("01/18", "Zelle To CHARLIE", "80.00")],
            ),
        })
        assert report.imported == 2
        rows = _rows_by_institution()
        x, x3 = rows["Bank X"], rows["Bank X3"]

        # self-transfer between two accounts of the entity → both legs 9000
        for leg in (x["ONLINE TRANSFER TO SAVINGS"], x3["Online Transfer from checking"]):
            assert leg.coa_code == "9000"
            assert leg.is_transfer is True

        # third-party rails → not internal, still tagged as candidates
        for leg in (x["TRANSFER TO PARTNER_1"], x["ZELLE FROM ALICE"], x3["Zelle To CHARLIE"]):
            assert leg.coa_code not in NOT_INTERNAL, leg.description
            assert leg.is_transfer is False, leg.description
            assert TAG in leg.tags

        # the payee's vendor rule decides once the rail words are stripped
        assert x["ZELLE TO ACME LAW"].coa_code == "5071"

    def test_bank_x_transfer_alone_is_not_confirmed(self, importer):
        importer({
            "x.pdf": _bank_x_statement(
                "1111111111111",  # redaction: allow
                debits=[("01/10", "TRANSFER TO SAVINGS", "700.00")], credits=[],
            ),
        })
        [leg] = _rows_by_institution()["Bank X"].values()
        assert leg.coa_code != "9000" and leg.is_transfer is False
        assert TAG in leg.tags

    def test_x3_own_keyword_transfer_is_internal(self, importer, monkeypatch):
        monkeypatch.setitem(bank_x3_checking._CFG, "intra_xfer_keywords", [SYNTH_KW])
        importer({
            "x3.pdf": _bank_x3_statement(
                "22223333",  # redaction: allow
                credits=[], debits=[("01/18", f"ONLINE TRANSFER {SYNTH_KW}", "80.00")],
            ),
        })
        [leg] = _rows_by_institution()["Bank X3"].values()
        assert leg.is_transfer is True
        assert TAG not in leg.tags

    def test_x3_third_party_zelle_is_not_9000(self, importer):
        importer({
            "x3.pdf": _bank_x3_statement(
                "22223333",  # redaction: allow
                credits=[("01/22", "Zelle payment from BOB", "250.00")], debits=[],
            ),
        })
        [leg] = _rows_by_institution()["Bank X3"].values()
        assert leg.coa_code not in NOT_INTERNAL
        assert leg.is_transfer is False
        assert leg.tags == [TAG]
