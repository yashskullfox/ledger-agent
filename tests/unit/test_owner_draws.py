"""
tests/unit/test_owner_draws.py  –  C-04 / owner decision D2.

Transfers out of a business account to an owner's OWN account (last-4 listed in the
private config ``OWNER_DRAW_ACCOUNTS``) are owner draws (3040), not internal transfers.

* shared matcher ``intelligence/owner_draws.account_refs``: only marker-preceded
  4-digit groups; never dates, amounts, check numbers or longer digit runs
* parser path (broker_y / broker_z) and classifier path
* entity-to-entity transfers stay 9000; unlisted outside accounts keep their code
  and get the ``owner_draw_candidate`` tag
* missing / malformed private config → no crash, no reclassification
* balance-sheet equity shows 3040 distributions

Synthetic data only; the listed last-4 is monkeypatched to "0000".
"""
from __future__ import annotations

import json
import sqlite3
import sys
import types
from datetime import date
from decimal import Decimal

import pytest

from ledger_agent.core.intelligence import owner_draws
from ledger_agent.core.models import Transaction, TransactionType
from ledger_agent.core.parsers.broker_y_brokerage import BrokerYBrokerageParser
from ledger_agent.core.parsers.broker_z import BrokerZParser

LISTED = "0000"   # owner's own account (synthetic)
ENTITY = "2222"   # another account of the entity (synthetic)
OUTSIDE = "3333"  # unlisted account outside the entity (synthetic)
TAG = owner_draws.OWNER_DRAW_CANDIDATE_TAG


@pytest.fixture
def listed(monkeypatch):
    monkeypatch.setattr(owner_draws, "OWNER_DRAW_LAST4", frozenset({LISTED}))


@pytest.fixture
def entity_db(db):
    """Fresh DB with one entity owning a checking (ENTITY) and a brokerage account."""
    from ledger_agent.core.database import AccountRepo, EntityRepo
    from ledger_agent.core.models import Account, AccountType, Entity

    ent = Entity(id="ent-od", name="ENTITY_A", entity_type="LLC", state="MO")
    EntityRepo.upsert(ent)
    AccountRepo.upsert(Account(
        id="acct-chk", entity_id=ent.id, name="Operating", institution="Bank X",
        account_type=AccountType.CHECKING, account_number_masked=f"****{ENTITY}",
    ))
    AccountRepo.upsert(Account(
        id="acct-brk", entity_id=ent.id, name="Brokerage", institution="Broker Y",
        account_type=AccountType.BROKERAGE, account_number_masked="****9999",  # redaction: allow
    ))
    return db


def _out(desc: str, *, code: str = "9000", is_transfer: bool = True,
         ttype: TransactionType = TransactionType.TRANSFER_OUT,
         amount: str = "-500", account_id: str = "") -> Transaction:
    return Transaction(
        account_id=account_id, date=date(2026, 2, 10), description=desc,
        raw_description=desc, amount=Decimal(amount), transaction_type=ttype,
        statement_period="2026-02", coa_code=code,
        coa_name="Inter-Account Transfer" if code == "9000" else code,
        is_transfer=is_transfer,
    )


# ── matcher ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Transfer to x0000",
    "TRANSFER TO X0000",
    "Transfer to XXXX0000",
    "Transfer to XXXX-0000",
    "Transfer to *0000",
    "Transfer to **0000",
    "Transfer to ******0000",  # redaction: allow
    "Transfer to ...0000",
    "Transfer to acct 0000",
    "Transfer to ACCT# 0000",
    "Transfer to Acct No. 0000",
    "Transfer to account ending 0000",
    "Transfer to account ending in 0000",
    "Transfer to checking ending in 0000",
])
def test_account_refs_matches_marker_preceded_group(text):
    assert owner_draws.account_refs(text) == [LISTED]


@pytest.mark.parametrize("text", [
    "Transfer 0000",                       # bare 4 digits, no marker
    "Wire 01/00 -$10,000.00",              # amount containing the digits  # redaction: allow
    "Wire -10000.00",                      # amount, no separators
    "Check #0000",                         # check number
    "Ref 100000 Transfer",                 # longer digit run
    "Transfer to acct 100000",             # marker + longer run: not a last-4
    "Transfer to x100000",
    "Transfer to ******00001",  # redaction: allow
    "Transfer to acct 0000-1234",          # first group of a hyphenated number
    "Period ending 0000",                  # bare 'ending' (year ending ...) is not a marker
    "TAX0000",                             # x inside a word
    "2000-00-00 Transfer",                 # date
])
def test_account_refs_ignores_dates_amounts_and_long_runs(text):
    assert LISTED not in owner_draws.account_refs(text)


# ── rule (direct) ────────────────────────────────────────────────────────────

def test_rule_listed_draw_recoded_to_3040(listed):
    txn = _out("Transfer to acct x0000")
    assert owner_draws.apply_owner_draw_rule(txn, own_last4=set()) == "draw"
    assert txn.coa_code == "3040"
    assert txn.is_transfer is False
    assert txn.transaction_type == TransactionType.DEBIT
    assert TAG not in txn.tags


def test_rule_inflow_from_listed_account_untouched(listed):
    txn = _out("Transfer from acct x0000", amount="500",
               ttype=TransactionType.TRANSFER_IN)
    assert owner_draws.apply_owner_draw_rule(txn, own_last4=set()) is None
    assert txn.coa_code == "9000"
    assert txn.is_transfer is True


def test_rule_entity_to_entity_stays_9000(listed):
    txn = _out(f"Transfer to acct x{ENTITY}")
    assert owner_draws.apply_owner_draw_rule(txn, own_last4={ENTITY}) is None
    assert txn.coa_code == "9000"
    assert txn.is_transfer is True
    assert txn.tags == []


def test_rule_unlisted_outside_unchanged_and_tagged(listed):
    txn = _out(f"Transfer to acct x{OUTSIDE}")
    assert owner_draws.apply_owner_draw_rule(txn, own_last4={ENTITY}) == "candidate"
    assert txn.coa_code == "9000"
    assert txn.is_transfer is True
    assert txn.tags == [TAG]
    owner_draws.apply_owner_draw_rule(txn, own_last4={ENTITY})
    assert txn.tags == [TAG]  # idempotent


def test_rule_non_transfer_with_unlisted_ref_not_tagged(listed):
    txn = _out(f"Card fee acct x{OUTSIDE}", code="5080", is_transfer=False,
               ttype=TransactionType.DEBIT)
    assert owner_draws.apply_owner_draw_rule(txn, own_last4=set()) is None
    assert txn.tags == []


def test_rule_amount_with_listed_digits_not_matched(listed):
    txn = _out("Wire Transfer Out 01/00 -10000.00")
    assert owner_draws.apply_owner_draw_rule(txn, own_last4=set()) is None
    assert txn.coa_code == "9000"


# ── parser path ──────────────────────────────────────────────────────────────

def test_broker_y_withdrawal_to_listed_account_is_draw(listed):
    text = (
        "Withdrawals\n"
        "Date  Description                   Amount\n"
        "01/20 Wire Transfer to acct x0000   -$2,000.00\n"  # redaction: allow
        "01/21 Wire Transfer Out             -$700.00\n"  # redaction: allow
        "01/25 Annual Fee                    -$150.00\n"  # redaction: allow
        "Total Withdrawals                   -$2,850.00\n"  # redaction: allow
    )
    txns = BrokerYBrokerageParser()._parse_withdrawals(text, "2026-01", 2026)
    draw = next(t for t in txns if "x0000" in t.description)
    assert draw.coa_code == "3040"
    assert draw.is_transfer is False
    assert draw.transaction_type == TransactionType.DEBIT
    plain = next(t for t in txns if t.description == "Wire Transfer Out")
    assert plain.coa_code == "9000" and plain.is_transfer is True
    fee = next(t for t in txns if "Annual Fee" in t.description)
    assert fee.coa_code == "5080"


def test_broker_y_destination_on_wrapped_line_is_draw(listed):
    text = (
        "Withdrawals\n"
        "Date Reference Description Amount\n"
        "01/09 Money Line Paid EFT FUNDS PAID ED12345678 /WEB -$2,500.00\n"  # redaction: allow
        "SYNTHETIC BANK ******0000\n"  # redaction: allow
        "Total Withdrawals -$2,500.00\n"  # redaction: allow
    )
    txns = BrokerYBrokerageParser()._parse_withdrawals(text, "2026-01", 2026)
    assert len(txns) == 1
    assert txns[0].coa_code == "3040"
    assert txns[0].is_transfer is False


def _broker_z_text(desc: str) -> str:
    return (
        "Deposits & Withdrawals\n"
        "Date Description Amount\n"
        "USD\n"
        f"2026-01-14 {desc} -3,000.00\n"
        "2026-03-20 Electronic Fund Transfer 1,500.00\n"
        "Total -1,500.00\n"
        "Financial Instrument Information\n"
    )


def test_broker_z_disbursement_to_listed_account_is_draw(listed):
    txns = BrokerZParser()._parse_cash_transactions(
        _broker_z_text("Disbursement to account ending 0000"), "2026-01", 2026
    )
    out = next(t for t in txns if t.amount < 0)
    assert out.coa_code == "3040"
    assert out.is_transfer is False
    inflow = next(t for t in txns if t.amount > 0)
    assert inflow.coa_code == "9000" and inflow.is_transfer is True


def test_broker_z_entity_to_entity_stays_9000(listed, entity_db):
    txns = BrokerZParser()._parse_cash_transactions(
        _broker_z_text(f"Disbursement to account ending {ENTITY}"), "2026-01", 2026
    )
    out = next(t for t in txns if t.amount < 0)
    assert out.coa_code == "9000"
    assert out.is_transfer is True
    assert TAG not in out.tags


def test_broker_z_unlisted_outside_unchanged_and_tagged(listed, entity_db):
    txns = BrokerZParser()._parse_cash_transactions(
        _broker_z_text(f"Disbursement to account ending {OUTSIDE}"), "2026-01", 2026
    )
    out = next(t for t in txns if t.amount < 0)
    assert out.coa_code == "9000"
    assert out.is_transfer is True
    assert out.transaction_type == TransactionType.TRANSFER_OUT
    assert out.tags == [TAG]


# ── classifier path ──────────────────────────────────────────────────────────

def _insert(tx_id: str, desc: str, *, is_transfer: bool = True,
            ttype: TransactionType = TransactionType.TRANSFER_OUT) -> Transaction:
    from ledger_agent.core.database import TransactionRepo

    txn = _out(desc, code="", is_transfer=is_transfer, ttype=ttype, account_id="acct-chk")
    txn.id = tx_id
    TransactionRepo.bulk_insert([txn])
    return txn


def _row(db_file, tx_id: str) -> sqlite3.Row:
    con = sqlite3.connect(str(db_file))
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT coa_code, is_transfer, transaction_type, tags FROM transactions WHERE id=?",
            (tx_id,),
        ).fetchone()
    finally:
        con.close()


def test_classifier_listed_draw_is_3040_and_not_transfer(listed, entity_db):
    from ledger_agent.core.intelligence.classifier import classify_batch

    txn = _insert("od-1", "ONLINE TRANSFER TO CHK x0000")
    classify_batch([txn])

    assert txn.coa_code == "3040" and txn.is_transfer is False
    row = _row(entity_db, "od-1")
    assert row["coa_code"] == "3040"
    assert row["is_transfer"] == 0          # parser flag cleared: a draw leaves the entity
    assert row["transaction_type"] == TransactionType.DEBIT.value


def test_classifier_entity_to_entity_stays_9000(listed, entity_db):
    from ledger_agent.core.intelligence.classifier import classify_batch

    txn = _insert("od-2", f"TRANSFER * TO ACCT {ENTITY}")
    classify_batch([txn])

    row = _row(entity_db, "od-2")
    assert row["coa_code"] == "9000"
    assert row["is_transfer"] == 1
    assert TAG not in json.loads(row["tags"])


def test_classifier_unlisted_outside_tagged_and_persisted(listed, entity_db):
    from ledger_agent.core.intelligence.classifier import classify_batch

    txn = _insert("od-3", f"TRANSFER * TO ACCT {OUTSIDE}")
    classify_batch([txn])

    row = _row(entity_db, "od-3")
    assert row["coa_code"] == "9000"
    assert row["is_transfer"] == 1
    assert json.loads(row["tags"]) == [TAG]


def test_classifier_does_not_teach_memory_the_draw(listed, entity_db):
    """A listed draw must not be remembered: fuzzy memory would spread 3040 to
    transfers to other accounts."""
    from ledger_agent.core.intelligence.classifier import classify_batch

    classify_batch([_insert("od-4", "ONLINE TRANSFER TO CHK x0000")])
    txn = _insert("od-5", f"ONLINE TRANSFER TO CHK x{OUTSIDE}")
    classify_batch([txn])
    assert txn.coa_code != "3040"


# ── private config ───────────────────────────────────────────────────────────

def _fake_private(monkeypatch, **attrs):
    pkg = types.ModuleType("private")
    mod = types.ModuleType("private.institutions")
    for k, v in attrs.items():
        setattr(mod, k, v)
    pkg.institutions = mod
    monkeypatch.setitem(sys.modules, "private", pkg)
    monkeypatch.setitem(sys.modules, "private.institutions", mod)


def test_missing_key_in_private_config_loads_empty(monkeypatch):
    _fake_private(monkeypatch, BROKER_Y={"detect": []})  # no OWNER_DRAW_ACCOUNTS
    assert owner_draws._load_listed_last4() == frozenset()


def test_missing_private_module_loads_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "private.institutions", None)
    assert owner_draws._load_listed_last4() == frozenset()


def test_malformed_private_values_are_filtered(monkeypatch):
    _fake_private(monkeypatch, OWNER_DRAW_ACCOUNTS={"last4": ["0000", 4444, "12345", "ab12"]})
    assert owner_draws._load_listed_last4() == frozenset({"0000", "4444"})
    _fake_private(monkeypatch, OWNER_DRAW_ACCOUNTS=["0000"])  # wrong shape
    assert owner_draws._load_listed_last4() == frozenset()


def test_empty_list_no_reclassification(monkeypatch):
    monkeypatch.setattr(owner_draws, "OWNER_DRAW_LAST4", frozenset())
    txns = BrokerZParser()._parse_cash_transactions(
        _broker_z_text("Disbursement to account ending 0000"), "2026-01", 2026
    )
    out = next(t for t in txns if t.amount < 0)
    assert out.coa_code == "9000"
    assert out.is_transfer is True


# ── balance sheet equity ─────────────────────────────────────────────────────

def test_balance_sheet_equity_shows_distributions(entity_db):
    from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
    from ledger_agent.core.database import SnapshotRepo, TransactionRepo
    from ledger_agent.core.models import AccountSnapshot

    SnapshotRepo.upsert(AccountSnapshot(
        account_id="acct-chk", statement_period="2025-12", ending_balance=Decimal("1000"),
    ))
    SnapshotRepo.upsert(AccountSnapshot(
        account_id="acct-brk", statement_period="2025-12", ending_balance=Decimal("0"),
    ))

    def _t(period: str, amount: str, code: str) -> Transaction:
        y, m = int(period[:4]), int(period[5:])
        return Transaction(
            account_id="acct-chk", date=date(y, m, 5), description=f"SYNTHETIC {code}",
            raw_description="", amount=Decimal(amount),
            transaction_type=TransactionType.DEBIT if amount.startswith("-")
            else TransactionType.CREDIT,
            statement_period=period, coa_code=code, coa_name=code,
        )

    TransactionRepo.bulk_insert([
        _t("2025-01", "5000", "3010"),
        _t("2025-06", "-300", "3040"),
        _t("2025-09", "-200", "3040"),
        _t("2026-01", "-100", "3040"),  # after the balance-sheet period: excluded
    ])

    bs = BalanceSheetBuilder("ent-od", "2025-12").build()
    lines = {ln.coa_code: ln.amount for ln in bs.lines}

    assert lines["3040"] == Decimal("-500")
    assert lines["3010"] == Decimal("5000")
    # The equity components add up to total equity, and the sheet still balances.
    assert lines["3010"] + lines["3040"] + lines["3020"] + bs.net_income == bs.total_equity
    assert bs.total_equity == bs.total_assets - bs.total_liabilities
    assert bs.is_balanced


# ── self-audit ───────────────────────────────────────────────────────────────

def test_self_audit_lists_owner_draw_candidates(entity_db):
    from ledger_agent.core.accounting.self_audit import check_owner_draw_candidates

    outside = _out(f"Transfer to acct x{OUTSIDE}", account_id="acct-chk")
    outside.tags = [TAG]
    # Tagged at parse time, but the destination has since been imported as an
    # entity account → internal, not a candidate.
    now_internal = _out(f"Transfer to acct x{ENTITY}", account_id="acct-chk")
    now_internal.tags = [TAG]
    untagged = _out("Wire Transfer Out", account_id="acct-chk")

    res = check_owner_draw_candidates([outside, now_internal, untagged])
    assert res.name == "owner_draw_candidates"
    assert res.status == "warn"
    assert len(res.details) == 1
    assert res.details[0]["account_id"] == "acct-chk"
    assert check_owner_draw_candidates([untagged]).status == "pass"
