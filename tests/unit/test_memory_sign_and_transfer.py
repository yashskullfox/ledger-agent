"""
tests/unit/test_memory_sign_and_transfer.py  –  C-14 classification memory.

* The seed "DEPOSIT INTUIT" → 4020 matches only whole words on an inflow; it no
  longer fuzzy-matches unrelated "... DEPOSIT" text (score about 85).
* Memory lookup is sign-aware: a revenue rule (4xxx) applies only to inflows, an
  expense rule (5xxx) only to outflows, whatever direction it was stored with;
  other codes follow the stored direction ("in" / "out" / "any").
* Rule files written before C-14 (no ``direction``) load as "any" and keep working;
  the new fields are additive, so the file stays readable by older code.
* Memory never replays an internal transfer (9000, or ``is_transfer``) on a row
  without an own-account cue; the "EFT FUNDS PAID" → 9000 seed is gone, and the
  classifier's generic transfer terms ("TRANSFER *", "ZELLE BUSINESS PAYMENT") no
  longer make an unflagged row internal.  Card payoff / sweep wording is unchanged.

Synthetic data only; every test uses a temporary memory file.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import date
from decimal import Decimal

import pytest

from ledger_agent.core.intelligence import memory as memory_mod
from ledger_agent.core.intelligence.classifier import (
    TRANSFER_CANDIDATE_TAG,
    classify_transaction,
    suggest_classification,
)
from ledger_agent.core.intelligence.memory import ClassificationMemory
from ledger_agent.core.models import Transaction, TransactionType

TAG = TRANSFER_CANDIDATE_TAG
XFER = ("9000", "Inter-Account Transfer")


# ── fixtures / helpers ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def no_private_config(monkeypatch):
    """Seeds never read the real private config here (empty intra keywords)."""
    _fake_private(monkeypatch, BANK_X={"detect": [], "intra_xfer_keywords": []})


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A fresh, seeded memory installed as the classifier's singleton."""
    mem = ClassificationMemory(tmp_path / "mem.json")
    monkeypatch.setattr(memory_mod, "_memory", mem)
    return mem


@pytest.fixture
def empty(tmp_path, monkeypatch):
    """An empty memory (seeds cleared) installed as the classifier's singleton."""
    mem = ClassificationMemory(tmp_path / "mem.json")
    mem._rules = []
    monkeypatch.setattr(memory_mod, "_memory", mem)
    return mem


@pytest.fixture
def coa(db):
    from ledger_agent.core.database import COARepo
    return COARepo.list_all()


def _fake_private(monkeypatch, **attrs):
    pkg = types.ModuleType("private")
    mod = types.ModuleType("private.institutions")
    for k, v in attrs.items():
        setattr(mod, k, v)
    pkg.institutions = mod
    monkeypatch.setitem(sys.modules, "private", pkg)
    monkeypatch.setitem(sys.modules, "private.institutions", mod)


def _txn(desc: str, amount: str, *, tags=(), is_transfer: bool = False) -> Transaction:
    amt = Decimal(amount)
    return Transaction(
        account_id="acct-a", date=date(2025, 1, 10), description=desc,
        raw_description=desc, amount=amt,
        transaction_type=TransactionType.CREDIT if amt > 0 else TransactionType.DEBIT,
        statement_period="2025-01", tags=list(tags), is_transfer=is_transfer,
    )


def _code(result) -> str | None:
    return result[0] if result else None


def _write_legacy(path, rules: list[dict]) -> None:
    path.write_text(json.dumps({"rules": rules}, indent=2), encoding="utf-8")


def _legacy_rule(pattern: str, code: str, name: str = "Synthetic", *,
                 is_transfer: bool = False, confirmed: int = 1) -> dict:
    return {"pattern": pattern, "coa_code": code, "coa_name": name,
            "is_transfer": is_transfer, "confirmed_count": confirmed}


# ── 1. the "DEPOSIT INTUIT" seed ──────────────────────────────────────────────

class TestDepositIntuitSeed:
    def test_seed_is_inflow_only_and_whole_word(self, seeded):
        [rule] = [r for r in seeded.list_rules() if r["pattern"] == "DEPOSIT INTUIT"]
        assert rule["coa_code"] == "4020"
        assert rule["direction"] == "in"
        assert rule["match"] == "word"

    @pytest.mark.parametrize("desc", [
        "SYNTH ACH DEPOSIT",
        "MOBILE CHECKING DEPOSIT",
        "DEPOSIT INTUITION SYNTH",
    ])
    def test_unrelated_deposit_outflow_not_seed_coded(self, seeded, desc):
        assert _code(seeded.lookup(desc, -75.0)) != "4020"

    @pytest.mark.parametrize("desc", ["SYNTH ACH DEPOSIT", "MOBILE CHECKING DEPOSIT"])
    def test_unrelated_deposit_inflow_not_fuzzy_matched(self, seeded, desc):
        assert _code(seeded.lookup(desc, 75.0)) != "4020"

    @pytest.mark.parametrize("desc", [
        "DEPOSIT INTUIT",
        "DEPOSIT INTUIT INVOICE",
        "DEPOSIT INTUIT 123456789",
        "SYNTH ACH DEPOSIT INTUIT 01-08",
    ])
    def test_intuit_deposit_inflow_still_4020(self, seeded, desc):
        result = seeded.lookup(desc, 500.0)
        assert _code(result) == "4020" and result[3] == 100

    def test_intuit_deposit_outflow_is_not_revenue(self, seeded):
        assert _code(seeded.lookup("DEPOSIT INTUIT REVERSAL", -500.0)) != "4020"

    def test_classifier_deposit_outflow_not_4020(self, seeded, coa):
        t = classify_transaction(_txn("SYNTH ACH DEPOSIT", "-75"), coa)
        assert t.coa_code != "4020"

    def test_classifier_intuit_deposit_inflow_is_4020(self, seeded, coa):
        t = classify_transaction(_txn("DEPOSIT INTUIT INVOICE", "500"), coa)
        assert t.coa_code == "4020" and t.is_transfer is False


# ── 2. sign-aware lookup ──────────────────────────────────────────────────────

class TestSignAwareLookup:
    def test_expense_rule_not_applied_to_inflow(self, empty):
        empty.remember("SYNTH VENDOR ZQX", "5080", "Other Operating Expenses", amount=-10)
        assert empty.lookup("SYNTH VENDOR ZQX", 10.0) is None
        assert _code(empty.lookup("SYNTH VENDOR ZQX", -10.0)) == "5080"

    def test_revenue_rule_not_applied_to_outflow(self, empty):
        empty.remember("SYNTH CLIENT QQX", "4020", "Service Revenue", amount=500)
        assert empty.lookup("SYNTH CLIENT QQX", -500.0) is None
        assert _code(empty.lookup("SYNTH CLIENT QQX", 500.0)) == "4020"

    def test_family_wins_over_stored_direction(self, empty):
        # an expense learned from a refund inflow still applies only to outflows
        empty.remember("SYNTH VENDOR RFD", "5061", "Office Supplies", amount=25)
        assert empty.list_rules()[0]["direction"] == "in"
        assert empty.lookup("SYNTH VENDOR RFD", 25.0) is None
        assert _code(empty.lookup("SYNTH VENDOR RFD", -25.0)) == "5061"

    def test_equity_rule_follows_stored_direction(self, empty):
        empty.remember("SYNTH LEVY QQX", "3040", "Members Distributions", amount=-40)
        assert empty.lookup("SYNTH LEVY QQX", 40.0) is None
        assert _code(empty.lookup("SYNTH LEVY QQX", -40.0)) == "3040"

    def test_rule_without_amount_is_any(self, empty):
        empty.remember("SYNTH CAPITAL QQX", "3010", "Members Capital Contributions")
        assert empty.list_rules()[0]["direction"] == "any"
        assert _code(empty.lookup("SYNTH CAPITAL QQX", 40.0)) == "3010"
        assert _code(empty.lookup("SYNTH CAPITAL QQX", -40.0)) == "3010"

    def test_unknown_amount_applies_no_direction_filter(self, empty):
        empty.remember("SYNTH VENDOR ZQX", "5080", "Other Operating Expenses", amount=-10)
        assert _code(empty.lookup("SYNTH VENDOR ZQX")) == "5080"
        assert _code(empty.lookup("SYNTH VENDOR ZQX", 0)) == "5080"

    def test_reconfirm_other_direction_widens_to_any(self, empty):
        empty.remember("SYNTH CAPITAL QQX", "3010", "Members Capital Contributions", amount=40)
        empty.remember("SYNTH CAPITAL QQX", "3010", "Members Capital Contributions", amount=-40)
        [rule] = empty.list_rules()
        assert rule["direction"] == "any" and rule["confirmed_count"] == 2

    def test_new_code_takes_new_direction(self, empty):
        empty.remember("SYNTH CAPITAL QQX", "3010", "Members Capital Contributions", amount=40)
        empty.remember("SYNTH CAPITAL QQX", "3040", "Members Distributions", amount=-40)
        [rule] = empty.list_rules()
        assert (rule["coa_code"], rule["direction"]) == ("3040", "out")

    def test_classifier_does_not_replay_expense_on_inflow(self, empty, coa):
        empty.remember("SYNTH VENDOR ZQX", "5080", "Other Operating Expenses", amount=-10)
        assert classify_transaction(_txn("SYNTH VENDOR ZQX", "10"), coa).coa_code != "5080"
        assert classify_transaction(_txn("SYNTH VENDOR ZQX", "-10"), coa).coa_code == "5080"

    def test_classifier_does_not_replay_revenue_on_outflow(self, empty, coa):
        empty.remember("SYNTH CLIENT QQX", "4020", "Service Revenue", amount=500)
        assert classify_transaction(_txn("SYNTH CLIENT QQX", "-500"), coa).coa_code != "4020"
        assert classify_transaction(_txn("SYNTH CLIENT QQX", "500"), coa).coa_code == "4020"

    def test_classifier_stores_direction(self, empty, coa):
        classify_transaction(_txn("SYNTH PAYROLL RUN", "-900"), coa)
        [rule] = empty.list_rules()
        assert (rule["coa_code"], rule["direction"]) == ("5021", "out")

    def test_suggest_is_sign_aware(self, empty, db):
        empty.remember("SYNTH VENDOR ZQX", "5080", "Other Operating Expenses", amount=-10)
        assert suggest_classification("SYNTH VENDOR ZQX", -10.0)["source"] == "memory"
        assert suggest_classification("SYNTH VENDOR ZQX", 10.0)["source"] != "memory"


# ── 3. legacy rule files (no direction) ───────────────────────────────────────

class TestLegacyFile:
    def test_legacy_rules_load_as_any_and_work(self, tmp_path):
        path = tmp_path / "legacy.json"
        _write_legacy(path, [
            _legacy_rule("SYNTH SOFTWARE QQX", "5010", "Software & Subscriptions"),
            _legacy_rule("SYNTH CAPITAL QQX", "3010", "Members Capital Contributions"),
        ])
        mem = ClassificationMemory(path)
        assert _code(mem.lookup("SYNTH SOFTWARE QQX", -30.0)) == "5010"
        assert mem.lookup("SYNTH SOFTWARE QQX", 30.0) is None  # expense: outflows only
        assert _code(mem.lookup("SYNTH CAPITAL QQX", 30.0)) == "3010"
        assert _code(mem.lookup("SYNTH CAPITAL QQX", -30.0)) == "3010"
        assert _code(mem.lookup("SYNTH SOFTWARE QQX")) == "5010"  # old call shape

    def test_loading_does_not_rewrite_the_file(self, tmp_path):
        path = tmp_path / "legacy.json"
        _write_legacy(path, [
            _legacy_rule("DEPOSIT INTUIT", "4020", "Service Revenue", confirmed=0),
            _legacy_rule("EFT FUNDS PAID", "9000", "Inter-Account Transfer (Clearing)",
                         is_transfer=True, confirmed=0),
        ])
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        mem = ClassificationMemory(path)
        mem.lookup("SYNTH ACH DEPOSIT", -75.0)
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before

    def test_saved_file_keeps_old_fields(self, tmp_path):
        path = tmp_path / "legacy.json"
        _write_legacy(path, [_legacy_rule("SYNTH SOFTWARE QQX", "5010")])
        mem = ClassificationMemory(path)
        mem.remember("SYNTH VENDOR ZQX", "5080", "Other Operating Expenses", amount=-10)
        data = json.loads(path.read_text(encoding="utf-8"))
        for rule in data["rules"]:  # what pre-C-14 code reads
            assert {"pattern", "coa_code", "coa_name", "is_transfer",
                    "confirmed_count"} <= set(rule)
        assert "direction" not in data["rules"][0]  # untouched legacy rule
        assert data["rules"][1]["direction"] == "out"

    def test_legacy_seeds_are_tightened_or_dropped(self, tmp_path):
        path = tmp_path / "legacy.json"
        _write_legacy(path, [
            _legacy_rule("DEPOSIT INTUIT", "4020", "Service Revenue", confirmed=0),
            _legacy_rule("EFT FUNDS PAID", "9000", "Inter-Account Transfer (Clearing)",
                         is_transfer=True, confirmed=0),
        ])
        mem = ClassificationMemory(path)
        assert [r["pattern"] for r in mem.list_rules()] == ["DEPOSIT INTUIT"]
        assert _code(mem.lookup("MOBILE CHECKING DEPOSIT", 75.0)) is None
        assert _code(mem.lookup("SYNTH ACH DEPOSIT", -75.0)) is None
        assert _code(mem.lookup("DEPOSIT INTUIT INVOICE", 500.0)) == "4020"

    def test_user_confirmed_seed_pattern_is_kept(self, tmp_path):
        path = tmp_path / "legacy.json"
        _write_legacy(path, [
            _legacy_rule("EFT FUNDS PAID", "9000", "Inter-Account Transfer (Clearing)",
                         is_transfer=True, confirmed=2),
        ])
        mem = ClassificationMemory(path)
        assert len(mem.list_rules()) == 1
        assert mem.lookup("EFT FUNDS PAID", -100.0) is None  # gated: no cue
        assert _code(mem.lookup("EFT FUNDS PAID", -100.0, own_account_cue=True)) == "9000"

    def test_corrupt_direction_value_is_any(self, tmp_path):
        path = tmp_path / "legacy.json"
        rule = _legacy_rule("SYNTH CAPITAL QQX", "3010")
        rule["direction"] = "sideways"
        _write_legacy(path, [rule])
        mem = ClassificationMemory(path)
        assert _code(mem.lookup("SYNTH CAPITAL QQX", -5.0)) == "3010"


# ── 4. no wording-only internal transfers from memory ─────────────────────────

class TestTransferReplay:
    def test_learned_9000_needs_own_account_cue(self, empty):
        empty.remember("SYNTH ONLINE XFER QQX", *XFER, True, amount=-700)
        assert empty.lookup("SYNTH ONLINE XFER QQX", -700.0) is None
        result = empty.lookup("SYNTH ONLINE XFER QQX", -700.0, own_account_cue=True)
        assert _code(result) == "9000" and result[2] is True

    def test_transfer_flag_rule_needs_cue_too(self, empty):
        empty.remember("SYNTH OWNER MOVE QQX", "3010", "Members Capital Contributions",
                       True, amount=700)
        assert empty.lookup("SYNTH OWNER MOVE QQX", 700.0) is None
        assert _code(
            empty.lookup("SYNTH OWNER MOVE QQX", 700.0, own_account_cue=True)
        ) == "3010"

    @pytest.mark.parametrize("desc", ["SYNTH ONLINE XFER QQX", "SYNTH ONLINE XFER QQX 77"])
    def test_gated_best_match_not_replaced_by_runner_up(self, empty, desc):
        # the fuzzy runner-up of a gated rule is usually unrelated: no result
        empty.remember("SYNTH ONLINE XFER QQX", *XFER, True, amount=-700)
        empty.remember("SYNTH ONLINE XFER QQX FEE", "5020", "Bank & Transaction Fees",
                       amount=-5)
        assert empty.lookup(desc, -700.0) is None
        assert _code(empty.lookup(desc, -700.0, own_account_cue=True)) == "9000"

    def test_sign_gated_best_match_not_replaced_by_runner_up(self, empty):
        empty.remember("SYNTH CLIENT QQX", "4020", "Service Revenue", amount=500)
        empty.remember("SYNTH CLIENT QQX SUPPLIES", "5061", "Office Supplies", amount=-20)
        assert empty.lookup("SYNTH CLIENT QQX", -500.0) is None

    def test_classifier_no_replay_on_unflagged_row(self, empty, coa):
        empty.remember("SYNTH ONLINE XFER QQX", *XFER, True, amount=-700)
        t = classify_transaction(_txn("SYNTH ONLINE XFER QQX", "-700"), coa)
        assert t.coa_code != "9000" and t.is_transfer is False

    def test_classifier_no_replay_on_candidate(self, empty, coa):
        empty.remember("SYNTH ONLINE XFER QQX", *XFER, True, amount=-700)
        t = classify_transaction(_txn("SYNTH ONLINE XFER QQX", "-700", tags=[TAG]), coa)
        assert t.coa_code != "9000" and t.is_transfer is False

    def test_classifier_replays_on_parser_cue(self, empty, coa):
        # the parser flagged the row internal (private keyword / card payoff)
        empty.remember("SYNTH ONLINE XFER QQX", *XFER, True, amount=-700)
        t = classify_transaction(_txn("SYNTH ONLINE XFER QQX", "-700", is_transfer=True), coa)
        assert t.coa_code == "9000" and t.is_transfer is True

    def test_suggest_needs_own_account_wording(self, empty, db):
        empty.remember("SYNTH ONLINE XFER QQX", *XFER, True, amount=-700)
        assert suggest_classification("SYNTH ONLINE XFER QQX", -700.0)["coa_code"] != "9000"
        empty.remember("PAYMENT TO CREDIT CARD QQX", *XFER, True, amount=-300)
        assert suggest_classification("PAYMENT TO CREDIT CARD QQX", -300.0)[
            "coa_code"] == "9000"

    def test_eft_funds_paid_seed_removed(self, seeded):
        assert not [r for r in seeded.list_rules() if r["pattern"] == "EFT FUNDS PAID"]
        assert not [r for r in seeded.list_rules() if r["coa_code"] == "9000"]

    def test_eft_funds_paid_not_internal_by_wording(self, seeded, coa):
        t = classify_transaction(_txn("MONEY LINE PAID EFT FUNDS PAID SYNTH", "-2500"), coa)
        assert t.coa_code != "9000" and t.is_transfer is False

    def test_private_intra_keyword_seed_needs_cue(self, tmp_path, monkeypatch):
        _fake_private(monkeypatch, BANK_X={"detect": [], "intra_xfer_keywords": ["SYNTH KW"]})
        mem = ClassificationMemory(tmp_path / "mem.json")
        [rule] = [r for r in mem.list_rules() if r["pattern"] == "SYNTH KW"]
        assert (rule["coa_code"], rule["direction"]) == ("9000", "any")
        assert mem.lookup("SYNTH KW", -50.0) is None
        assert _code(mem.lookup("SYNTH KW", -50.0, own_account_cue=True)) == "9000"


# ── 5. classifier generic transfer terms ──────────────────────────────────────

class TestGenericTransferTerms:
    @pytest.mark.parametrize("desc", [
        "TRANSFER * SYNTH SAVINGS",
        "ZELLE BUSINESS PAYMENT TO BOB",
        "ZELLE BUSINESS PAYMENT FROM ALICE",
    ])
    @pytest.mark.parametrize("amount", ["-50", "50"])
    def test_untagged_generic_term_not_internal(self, empty, coa, desc, amount):
        t = classify_transaction(_txn(desc, amount), coa)
        assert t.coa_code != "9000" and t.is_transfer is False

    def test_generic_term_not_remembered_as_transfer(self, empty, coa):
        classify_transaction(_txn("TRANSFER * SYNTH SAVINGS", "-50"), coa)
        assert not [r for r in empty.list_rules() if r["coa_code"] == "9000"]

    @pytest.mark.parametrize("desc", ["TRANSFER * SYNTH SAVINGS", "ZELLE BUSINESS PAYMENT"])
    def test_generic_term_with_parser_cue_is_internal(self, empty, coa, desc):
        t = classify_transaction(_txn(desc, "-50", is_transfer=True), coa)
        assert t.coa_code == "9000" and t.is_transfer is True

    def test_generic_term_candidate_not_internal(self, empty, coa):
        t = classify_transaction(_txn("TRANSFER * SYNTH SAVINGS", "-50", tags=[TAG]), coa)
        assert t.coa_code != "9000" and t.is_transfer is False

    @pytest.mark.parametrize("tags", [(), (TAG,)])
    @pytest.mark.parametrize("desc", [
        "PAYMENT TO CREDIT CARD",
        "PAYMENTTO CREDIT CARD SYNTH",
        "INTERNET PAYMENT ACH TRANSF BROKERAGE",
        "ACH CORP DEBIT TRANSFER SYNTH",
        "BKG SVC LLC Z SWEEP",
    ])
    def test_card_payoff_and_sweep_unchanged(self, empty, coa, desc, tags):
        t = classify_transaction(_txn(desc, "-300", tags=tags), coa)
        assert t.coa_code == "9000" and t.is_transfer is True
