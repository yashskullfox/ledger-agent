"""
tests/unit/test_customer_deposit_revenue.py  –  C-11 customer deposits are revenue.

* The COA keyword scan never assigns a balance-sheet (1xxx asset, 2xxx liability)
  or equity (3xxx) account: "deposit" / "balance" / "checking" no longer mean Cash.
* Remembered or AI-suggested balance-sheet codes are not applied either.
* Bank X4 "Customer Deposits" rows are pre-coded 4020 Service Revenue, and the
  classifier codes a "Customer Deposit" inflow 4020 too.
* ``generate_form_1065`` counts a parsed customer deposit as income.
* ``scripts/reclassify_cash_coded.py``: dry run writes nothing, ``--apply`` writes
  only the proposed rows (audited, ids / codes only), a second run changes nothing,
  and the output carries counts only.

Synthetic data only (fresh tmp DBs, pseudonymous names, invented amounts).
"""
from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ledger_agent.core.intelligence import memory as memory_mod
from ledger_agent.core.intelligence.classifier import (
    UNCLASSIFIED_CODE,
    _keyword_match,
    classify_batch,
    classify_transaction,
    suggest_classification,
)
from ledger_agent.core.models import Transaction, TransactionType
from ledger_agent.core.parsers.bank_x4_checking import BankX4CheckingParser

ROOT = Path(__file__).resolve().parents[2]
FY = 2026
P_AND_L = ("revenue", "expense")


@pytest.fixture(autouse=True)
def fresh_memory(tmp_path, monkeypatch):
    """Isolated, empty classification memory per test (the session file is shared).

    The seeded default rules are cleared so the tests exercise the rule steps: the
    fuzzy seed "DEPOSIT INTUIT" (4020) would otherwise match any "... DEPOSIT".
    """
    mem = memory_mod.ClassificationMemory(tmp_path / "mem.json")
    mem._rules = []
    monkeypatch.setattr(memory_mod, "_memory", mem)


@pytest.fixture
def coa(db):
    from ledger_agent.core.database import COARepo
    return COARepo.list_all()


def _txn(desc: str, amount: str, **kw) -> Transaction:
    amt = Decimal(amount)
    return Transaction(
        account_id=kw.pop("account_id", "acct-a"), date=kw.pop("d", date(FY, 1, 15)),
        description=desc, raw_description=desc, amount=amt,
        transaction_type=kw.pop(
            "ttype", TransactionType.CREDIT if amt > 0 else TransactionType.DEBIT
        ),
        statement_period=kw.pop("period", f"{FY}-01"), **kw,
    )


def _x4_customer_deposits(*rows: str) -> list[Transaction]:
    lines = ["Customer Deposits", *rows, "Total Customer Deposits"]
    return BankX4CheckingParser()._parse_customer_deposits(lines, FY, f"{FY}-01")


# ── 1. keyword scan: never a balance-sheet or equity account ─────────────────

class TestKeywordScan:
    @pytest.mark.parametrize("desc, amount", [
        ("CUSTOMER DEPOSIT", 600.0),
        ("Customer Deposit", 600.0),
        ("BALANCE TRANSFER", 250.0),
        ("BALANCE TRANSFER", -250.0),
        ("MOBILE CHECKING DEPOSIT", 75.0),
        ("CASH DEPOSIT", 40.0),
        ("TAX REFUND SYNTH", 20.0),
        ("WIRE IN SYNTH CO", 900.0),
        ("OWNER DRAW", -100.0),
    ])
    def test_never_balance_sheet_or_equity(self, coa, desc, amount):
        hit = _keyword_match(desc, coa, amount)
        assert hit is None or hit.coa_type in P_AND_L, (desc, hit and hit.code)

    def test_customer_deposit_is_not_cash(self, coa):
        hit = _keyword_match("CUSTOMER DEPOSIT", coa, 600.0)
        assert hit is None or hit.code not in ("1000", "1010")

    def test_every_non_pl_seed_keyword_is_inert(self, coa):
        """Each keyword of every asset / liability / equity / 9000 row, as a whole
        description and with either sign, never yields a non-P&L code."""
        checked = 0
        for entry in coa:
            if entry.coa_type in P_AND_L:
                continue
            for kw in entry.keywords:
                for amount in (100.0, -100.0, 0.0):
                    hit = _keyword_match(kw, coa, amount)
                    assert hit is None or hit.coa_type in P_AND_L, (entry.code, hit.code)
                    checked += 1
        assert checked > 0

    def test_liability_overlap_now_resolves_to_expense(self, coa):
        # "margin interest" matched 2010 (margin) and 5030 → no single match before.
        hit = _keyword_match("MARGIN INTEREST CHARGED", coa, -12.0)
        assert hit is not None and hit.code == "5030"

    def test_mistyped_custom_row_with_balance_sheet_code_is_skipped(self, coa):
        from ledger_agent.core.models import COAEntry, COAType
        odd = COAEntry("1990", "Custom", COAType.REVENUE, None, "", ["synthwidget"])
        assert _keyword_match("SYNTHWIDGET SALE", [*coa, odd], 10.0) is None

    def test_pl_keywords_still_work(self, coa):
        assert _keyword_match("QUICKBOOKS ONLINE", coa, -30.0).code == "5010"
        assert _keyword_match("SYNTH CLIENT INVOICE", coa, 500.0).code == "4020"


# ── 2. classifier: customer deposit → 4020; no balance-sheet replay ──────────

class TestClassifier:
    def test_customer_deposit_inflow_is_service_revenue(self, coa):
        t = classify_transaction(_txn("Customer Deposit", "600"), coa)
        assert (t.coa_code, t.coa_name) == ("4020", "Service Revenue")
        assert t.is_transfer is False

    def test_customer_deposit_outflow_is_not_revenue(self, coa):
        t = classify_transaction(_txn("CUSTOMER DEPOSIT REFUND", "-600"), coa)
        assert t.coa_code != "4020"

    def test_unknown_deposit_goes_to_review_not_cash(self, coa):
        t = classify_transaction(_txn("MOBILE CHECKING DEPOSIT", "75"), coa)
        assert t.coa_code == UNCLASSIFIED_CODE

    def test_remembered_cash_code_is_not_replayed(self, coa):
        mem = memory_mod.get_memory()
        mem.remember("MOBILE CHECKING DEPOSIT", "1000", "Cash & Cash Equivalents")
        t = classify_transaction(_txn("MOBILE CHECKING DEPOSIT", "75"), coa)
        assert t.coa_code != "1000"

    def test_remembered_pl_code_still_applies(self, coa):
        memory_mod.get_memory().remember("SYNTH VENDOR ZQX", "5080", "Other Operating Expenses")
        t = classify_transaction(_txn("SYNTH VENDOR ZQX", "-10"), coa)
        assert t.coa_code == "5080"

    def test_ai_balance_sheet_suggestion_is_ignored(self, coa, monkeypatch):
        from ledger_agent.core.intelligence import ai_backend

        class _Fake:
            backend_name = "fake"

            def classify_transaction(self, description, amount, context=None):
                return {"coa_code": "2020", "coa_name": "Taxes Payable",
                        "is_transfer": False, "confidence": 0.99}

        monkeypatch.setattr(ai_backend, "get_backend", lambda: _Fake())
        t = classify_transaction(_txn("SYNTH LEVY OFFICE", "-40"), coa)
        assert t.coa_code != "2020"
        assert suggest_classification("SYNTH LEVY OFFICE", -40.0)["coa_code"] != "2020"

    def test_explicit_owner_draw_wording_is_3040(self, coa):
        assert classify_transaction(_txn("OWNER DRAW", "-100"), coa).coa_code == "3040"
        assert classify_transaction(
            _txn("MEMBER DISTRIBUTION Q1", "-100"), coa
        ).coa_code == "3040"

    def test_owner_draw_wording_on_inflow_or_bare_distribution_is_not_equity(self, coa):
        assert classify_transaction(_txn("OWNER DRAW", "100"), coa).coa_code != "3040"
        assert classify_transaction(
            _txn("SYNTH FUND DISTRIBUTION", "-100"), coa
        ).coa_code != "3040"


# ── 3. Bank X4 Customer Deposits section → 4020 ──────────────────────────────

class TestBankX4CustomerDeposits:
    def test_rows_are_precoded_service_revenue(self):
        rows = _x4_customer_deposits(
            "Jan150000000001600.00",  # redaction: allow (synthetic)
            "Jan200000000002125.50",  # redaction: allow (synthetic)
        )
        assert len(rows) == 2
        for t in rows:
            assert (t.coa_code, t.coa_name) == ("4020", "Service Revenue")
            assert t.amount > 0
            assert t.is_transfer is False
            assert t.transaction_type == TransactionType.CREDIT
        assert rows[0].amount == Decimal("600.00")  # redaction: allow (synthetic)

    def test_classify_batch_keeps_the_precode(self, ledger_db):
        from ledger_agent.core.database import TransactionRepo
        [t] = _x4_customer_deposits("Jan150000000001600.00")  # redaction: allow (synthetic)
        t.account_id = "acct-c11"
        TransactionRepo.bulk_insert([t], ledger_db)
        classify_batch([t])
        assert TransactionRepo.get_by_id(t.id, ledger_db).coa_code == "4020"


# ── 4. Form 1065 counts a customer deposit as income ─────────────────────────

@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    from ledger_agent.core.database import AccountRepo, EntityRepo, SnapshotRepo, init_db
    from ledger_agent.core.models import Account, AccountSnapshot, AccountType, Entity

    db_file = tmp_path / "c11.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    monkeypatch.setenv("FI_WASH_SALE_CSV", str(tmp_path / "absent.csv"))
    import ledger_agent.core.accounting.wash_sale as wash_sale
    monkeypatch.setattr(wash_sale, "_DEFAULT_CSV", tmp_path / "absent-default.csv")
    init_db(db_file)
    EntityRepo.upsert(Entity(id="ent-c11", name="ENTITY_A", entity_type="LLC", state="MO"),
                      db_file)
    AccountRepo.upsert(Account(
        id="acct-c11", entity_id="ent-c11", name="Operating", institution="Bank X4",
        account_type=AccountType.CHECKING, account_number_masked="0000",
    ), db_file)
    SnapshotRepo.upsert(AccountSnapshot(
        account_id="acct-c11", statement_period=f"{FY}-01", ending_balance=Decimal("100"),
    ), db_file)
    return db_file


def test_form_1065_counts_parsed_customer_deposit_as_income(ledger_db):
    import ledger_agent.core.api as api
    from ledger_agent.core.database import TransactionRepo

    [t] = _x4_customer_deposits("Jan150000000001600.00")  # redaction: allow (synthetic)
    t.account_id = "acct-c11"
    TransactionRepo.bulk_insert([t], ledger_db)

    f = api.generate_form_1065(FY)
    assert f.total_income == Decimal("600")
    assert f.ordinary_business_income == Decimal("600")


def test_form_1065_ignores_the_same_row_when_cash_coded(ledger_db):
    """Control: the pre-C-11 coding (1000) never reached income."""
    import ledger_agent.core.api as api
    from ledger_agent.core.database import TransactionRepo

    [t] = _x4_customer_deposits("Jan150000000001600.00")  # redaction: allow (synthetic)
    t.account_id, t.coa_code, t.coa_name = "acct-c11", "1000", "Cash & Cash Equivalents"
    TransactionRepo.bulk_insert([t], ledger_db)
    assert api.generate_form_1065(FY).total_income == Decimal("0")


# ── 5. scripts/reclassify_cash_coded.py ──────────────────────────────────────

def _load_script():
    spec = importlib.util.spec_from_file_location(
        "reclassify_cash_coded", ROOT / "scripts" / "reclassify_cash_coded.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


def _fingerprint(db_file: Path) -> str:
    conn = sqlite3.connect(str(db_file))
    try:
        rows = conn.execute("SELECT * FROM transactions ORDER BY id").fetchall()
    finally:
        conn.close()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def _codes(db_file: Path) -> dict[str, tuple[str, int]]:
    conn = sqlite3.connect(str(db_file))
    try:
        return {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT id, coa_code, is_transfer FROM transactions"
        )}
    finally:
        conn.close()


@pytest.fixture
def reclass_db(ledger_db):
    from ledger_agent.core.database import TransactionRepo

    def row(tx_id, desc, amount, code, **kw):
        return _txn(desc, amount, id=tx_id, account_id="acct-c11",
                    coa_code=code, coa_name=code, **kw)

    TransactionRepo.bulk_insert([
        row("t-dep", "Customer Deposit", "600", "1000"),           # → 4020
        row("t-unk", "SYNTH ZQX ITEM", "75", "1000"),              # → 9999 (review)
        row("t-sw", "QUICKBOOKS ONLINE", "-30", "1000"),           # → 5010
        row("t-tax", "SYNTH TAX OFFICE", "-40", "2020"),           # → 9999 (review)
        # C-14: own-account wording (card payoff); generic "TRANSFER *" wording
        # alone is no longer internal.
        row("t-xfr", "PAYMENT TO CREDIT CARD SYNTH", "-50", "1010"),  # → 9000: skipped
        row("t-ok", "SYNTH CLIENT INVOICE", "500", "4020"),        # not selected
        row("t-int", "Customer Deposit", "300", "1000", is_transfer=True,
            ttype=TransactionType.TRANSFER_IN),                     # transfer: not selected
        row("t-sell", "SOLD SYNTH TICKER_SEC1", "200", "1110",
            ttype=TransactionType.SELL),                            # trade row: not selected
    ], ledger_db)
    return ledger_db


class TestReclassifyScript:
    def test_dry_run_writes_nothing_and_proposes_pairs(self, reclass_db):
        mod = _load_script()
        before = _fingerprint(reclass_db)
        calls = []
        plan = mod.run(reclass_db, apply=False, audit_fn=lambda *a, **k: calls.append(k))
        assert _fingerprint(reclass_db) == before
        assert calls == []
        assert plan.candidates == 5
        assert dict(plan.pair_counts()) == {
            ("1000", "4020"): 1, ("1000", "9999"): 1, ("1000", "5010"): 1,
            ("2020", "9999"): 1,
        }
        assert plan.skipped["would_be_transfer"] == 1

    def test_apply_writes_only_proposed_rows_and_audits_ids_codes(self, reclass_db):
        mod = _load_script()
        before = _codes(reclass_db)
        calls = []
        plan = mod.run(reclass_db, apply=True,
                       audit_fn=lambda event, **k: calls.append((event, k)))
        after = _codes(reclass_db)
        changed = {i for i in before if before[i] != after[i]}
        assert changed == {"t-dep", "t-unk", "t-sw", "t-tax"}
        assert after["t-dep"] == ("4020", 0)
        assert after["t-sw"][0] == "5010"
        assert after["t-xfr"] == before["t-xfr"]
        assert len(calls) == len(plan.changes) == 4
        for event, fields in calls:
            assert event == "classifier.reassigned"
            assert set(fields) == {"transaction_id", "prior_coa_code", "coa_code",
                                   "classifier_version", "source"}

    def test_apply_is_idempotent(self, reclass_db):
        mod = _load_script()
        mod.run(reclass_db, apply=True, audit_fn=lambda *a, **k: None)
        snap = _fingerprint(reclass_db)
        calls = []
        plan = mod.run(reclass_db, apply=True, audit_fn=lambda *a, **k: calls.append(k))
        assert plan.changes == [] and calls == []
        assert _fingerprint(reclass_db) == snap

    def test_skip_suspense_leaves_review_rows_alone(self, reclass_db):
        mod = _load_script()
        plan = mod.run(reclass_db, apply=True, skip_suspense=True,
                       audit_fn=lambda *a, **k: None)
        after = _codes(reclass_db)
        assert after["t-unk"][0] == "1000" and after["t-tax"][0] == "2020"
        assert plan.skipped["suspense_skipped"] == 2

    def test_apply_moves_the_deposit_into_income(self, reclass_db):
        import ledger_agent.core.api as api
        mod = _load_script()
        assert api.generate_form_1065(FY).total_income == Decimal("500")
        mod.run(reclass_db, apply=True, audit_fn=lambda *a, **k: None)
        assert api.generate_form_1065(FY).total_income == Decimal("1100")

    def test_main_prints_counts_only(self, reclass_db, capsys):
        mod = _load_script()
        before = _fingerprint(reclass_db)
        assert mod.main(["--db", str(reclass_db)]) == 0
        out = capsys.readouterr().out
        assert _fingerprint(reclass_db) == before
        assert "DRY RUN" in out and "1000 -> 4020: 1" in out
        for leak in ("Customer Deposit", "QUICKBOOKS", "ZQX", "600", "acct-c11",
                     "t-dep", "ENTITY_A", str(reclass_db)):
            assert leak not in out

    def test_requires_explicit_db(self, tmp_path, capsys):
        mod = _load_script()
        with pytest.raises(SystemExit) as exc:
            mod.main([])
        assert exc.value.code == 2
        missing = tmp_path / "nope.db"
        assert mod.main(["--db", str(missing), "--apply"]) == 2
        assert not missing.exists()
