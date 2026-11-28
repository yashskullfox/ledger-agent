"""
tests/unit/test_context_export_isolation.py  –  card R-07 (ARCH-30 entity isolation)

``adapters/context_builder.build_context(entity_id, period)`` must carry ONLY the
requested entity's data, even when another entity has transactions, snapshots and
positions in the SAME statement period:

* transactions — only rows of the entity's own accounts (the bug: the export read
  every entity's rows for the period);
* balance-sheet lines / totals, positions, available periods — the entity's own;
* tax estimate — None for an entity that is not the fiscal year's PTE entity (Q-02);
* building one entity's context in a two-entity DB gives the same context as in a DB
  holding only that entity;
* privacy (P-06 placeholders, ``audit_egress``) is unchanged, and a single-entity DB
  exports exactly the rows it exported before the fix.

Synthetic tmp_path databases and pseudonymous names only; no real data.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from ledger_agent.core import config as _cfg
from ledger_agent.core import privacy

FY = 2026
PERIOD = f"{FY}-01"

ENT_A = "ent-r07-a"
ENT_B = "ent-r07-b"

# Per entity: checking + brokerage account, transactions, snapshot and position.
# Descriptions are lower-case so they pass ai_context redaction unchanged and can be
# matched in the export (upper-case words become <COUNTERPARTY_n> tokens).
# A has more transactions, so it is the fiscal year's PTE entity (api._entity_and_periods).
SEED = {
    ENT_A: {
        "name": "ENTITY_A HOLDINGS LLC",
        "state": "MO",
        "chk": "acct-r07-a-chk",
        "brk": "acct-r07-a-brk",
        "institution": "BANK_X",
        "broker": "BROKER_Y",
        "txns": [
            ("alpha consulting invoice", "4020", "5100.00"),
            ("alpha software plan", "5010", "-61.00"),
            ("alpha postage", "5061", "-23.00"),
        ],
        "chk_balance": "2400",
        "brk_balance": "1500",
        "position": ("TICKER_SEC1", "10", "150", "1500"),
    },
    ENT_B: {
        "name": "ENTITY_B VENTURES LLC",
        "state": "CA",
        "chk": "acct-r07-b-chk",
        "brk": "acct-r07-b-brk",
        "institution": "BANK_X2",
        "broker": "BROKER_Z",
        "txns": [
            ("bravo design retainer", "4020", "9300.00"),
            ("bravo monthly hosting", "5010", "-77.00"),
        ],
        "chk_balance": "8800",
        "brk_balance": "2700",
        "position": ("TICKER_SEC2", "30", "90", "2700"),
    },
}


@pytest.fixture(autouse=True)
def _privacy_env(monkeypatch):
    monkeypatch.setattr(_cfg, "AI_EGRESS_MODE", "redact")
    monkeypatch.setattr(_cfg, "PRIVACY_ENTITY_NAME", "")
    monkeypatch.delenv("FI_PRIVACY_ENTITY_NAME", raising=False)
    privacy._reset_session()
    yield
    privacy._reset_session()


def _seed(db_file, entity_ids) -> None:
    from ledger_agent.core.database import (
        AccountRepo,
        EntityRepo,
        PositionRepo,
        SnapshotRepo,
        TransactionRepo,
        init_db,
    )
    from ledger_agent.core.models import (
        Account,
        AccountSnapshot,
        AccountType,
        Entity,
        Position,
        Transaction,
        TransactionType,
    )

    init_db(db_file)
    for eid in entity_ids:
        s = SEED[eid]
        EntityRepo.upsert(
            Entity(id=eid, name=s["name"], entity_type="LLC", state=s["state"]), db_file
        )
        AccountRepo.upsert(Account(
            id=s["chk"], entity_id=eid, name=f"{s['name']} OPERATING",
            institution=s["institution"], account_type=AccountType.CHECKING,
            account_number_masked="0000",
        ), db_file)
        AccountRepo.upsert(Account(
            id=s["brk"], entity_id=eid, name=f"{s['broker']} Margin",
            institution=s["broker"], account_type=AccountType.BROKERAGE,
            account_number_masked="0000",
        ), db_file)
        TransactionRepo.bulk_insert([
            Transaction(
                id=f"txn-{eid}-{i}", account_id=s["chk"], date=date(FY, 1, 5 + i),
                description=desc, raw_description=desc, amount=Decimal(amt),
                transaction_type=TransactionType.CREDIT if Decimal(amt) > 0
                else TransactionType.DEBIT,
                statement_period=PERIOD, coa_code=code, coa_name="synthetic",
            )
            for i, (desc, code, amt) in enumerate(s["txns"])
        ], db_file)
        for acct, bal in ((s["chk"], s["chk_balance"]), (s["brk"], s["brk_balance"])):
            SnapshotRepo.upsert(AccountSnapshot(
                account_id=acct, statement_period=PERIOD, ending_balance=Decimal(bal),
            ), db_file)
        sym, qty, price, mv = s["position"]
        PositionRepo.upsert_period([Position(
            account_id=s["brk"], symbol=sym, name=sym, quantity=Decimal(qty),
            price_per_unit=Decimal(price), market_value=Decimal(mv),
            statement_period=PERIOD,
        )], db_file)


@pytest.fixture
def two_entity_db(tmp_path, monkeypatch):
    db_file = tmp_path / "r07_two.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    _seed(db_file, (ENT_A, ENT_B))
    return db_file


def _single_entity_db(tmp_path, monkeypatch, eid):
    db_file = tmp_path / f"r07_only_{eid}.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    _seed(db_file, (eid,))
    return db_file


def _build(eid):
    from adapters.context_builder import build_context

    return build_context(eid, period=PERIOD)


def _other(eid):
    return ENT_B if eid == ENT_A else ENT_A


def _amounts(seq) -> list[float]:
    return sorted(float(x) for x in seq)


def _comparable(ctx) -> dict:
    out = dict(ctx)
    out.pop("generated_at")
    return out


# ── 1. Each entity's export carries only its own data ─────────────────────────

@pytest.mark.parametrize("eid", [ENT_A, ENT_B])
def test_transactions_are_only_the_entitys_own(two_entity_db, eid):
    ctx = _build(eid)
    own, other = SEED[eid], SEED[_other(eid)]
    assert len(ctx["transactions"]) == len(own["txns"])
    assert _amounts(t["amount"] for t in ctx["transactions"]) == _amounts(
        amt for _, _, amt in own["txns"])
    descs = {t["description"] for t in ctx["transactions"]}
    assert descs == {desc for desc, _, _ in own["txns"]}
    text = json.dumps(ctx, ensure_ascii=False)
    for desc, _, _ in other["txns"]:
        assert desc not in text


def test_seed_really_mixes_entities_in_one_period(two_entity_db):
    """Non-vacuous: the unfiltered period read returns both entities' rows."""
    from ledger_agent.core.database import TransactionRepo

    rows = TransactionRepo.list_for_period(PERIOD)
    assert len(rows) == len(SEED[ENT_A]["txns"]) + len(SEED[ENT_B]["txns"])


@pytest.mark.parametrize("eid", [ENT_A, ENT_B])
def test_positions_are_only_the_entitys_own(two_entity_db, eid):
    ctx = _build(eid)
    sym, qty, price, mv = SEED[eid]["position"]
    assert [(p["symbol"], p["quantity"], p["price"], p["market_value"])
            for p in ctx["positions"]] == [(sym, float(qty), float(price), float(mv))]
    other_sym = SEED[_other(eid)]["position"][0]
    assert other_sym not in json.dumps(ctx, ensure_ascii=False)


@pytest.mark.parametrize("eid", [ENT_A, ENT_B])
def test_balance_sheet_and_periods_are_only_the_entitys_own(two_entity_db, eid):
    ctx = _build(eid)
    own = SEED[eid]
    (cash,) = [ln for ln in ctx["balance_sheet"]["lines"] if ln["coa_code"] == "1010"]
    assert cash["amount"] == float(own["chk_balance"])
    expected_assets = float(own["chk_balance"]) + float(own["position"][3])
    assert ctx["balance_sheet"]["total_assets"] == pytest.approx(expected_assets)
    assert ctx["summary"]["total_assets"] == ctx["balance_sheet"]["total_assets"]
    assert ctx["available_periods"] == [PERIOD]


def test_tax_estimate_never_attached_to_the_other_entity(two_entity_db):
    """Q-02: the year's PTE estimate belongs to one entity; the other gets None."""
    from ledger_agent.core import api

    pte_entity, _ = api._entity_and_periods(FY)
    assert pte_entity.id == ENT_A
    assert _build(ENT_B)["tax_estimate"] is None


@pytest.mark.parametrize("eid", [ENT_A, ENT_B])
def test_mixed_db_context_equals_single_entity_db_context(tmp_path, monkeypatch, eid):
    """Isolation end-to-end: another entity's presence changes nothing in the export."""
    _single_entity_db(tmp_path, monkeypatch, eid)
    alone = _comparable(_build(eid))
    if eid == ENT_B:
        # Alone, B is the year's PTE entity; in the mixed DB it is not (Q-02 → None).
        alone["tax_estimate"] = None
    mixed_db = tmp_path / "r07_mixed.db"
    monkeypatch.setenv("FI_DB_PATH", str(mixed_db))
    _seed(mixed_db, (ENT_A, ENT_B))
    assert _comparable(_build(eid)) == alone


def test_prompt_lists_only_the_entitys_transactions(two_entity_db):
    from adapters.context_builder import context_to_prompt

    prompt = context_to_prompt(_build(ENT_A))
    assert f"## Transactions ({len(SEED[ENT_A]['txns'])} total)" in prompt
    for desc, _, _ in SEED[ENT_B]["txns"]:
        assert desc not in prompt


# ── 2. Privacy behaviour unchanged ────────────────────────────────────────────

@pytest.mark.parametrize("eid", [ENT_A, ENT_B])
def test_privacy_placeholders_and_audit_unchanged(two_entity_db, eid):
    from adapters.context_builder import context_to_prompt

    ctx = _build(eid)
    assert ctx["entity"]["name"] == "<ENTITY_NAME>"
    text = (json.dumps(ctx, ensure_ascii=False) + "\n" + context_to_prompt(ctx)).casefold()
    for e in (ENT_A, ENT_B):
        assert SEED[e]["name"].casefold() not in text
        assert SEED[e]["institution"].casefold() not in text
    assert {p["account"].split(" – ")[0] for p in ctx["positions"]} <= {
        f"<INSTITUTION_{i}>" for i in (1, 2)}
    privacy.audit_egress(ctx)  # does not raise


def test_audit_failure_still_blocks(two_entity_db, monkeypatch):
    from adapters.context_builder import ContextExportBlocked

    def _fail(*_a, **_kw):
        raise privacy.PrivacyLeakError("blocked")

    monkeypatch.setattr(privacy, "audit_egress", _fail)
    with pytest.raises(ContextExportBlocked):
        _build(ENT_A)


# ── 3. Single-entity regression: same rows as the pre-fix unfiltered read ─────

def test_single_entity_db_exports_the_same_rows_as_before(tmp_path, monkeypatch):
    from ledger_agent.core.database import TransactionRepo

    _single_entity_db(tmp_path, monkeypatch, ENT_A)
    ctx = _build(ENT_A)
    before = TransactionRepo.list_for_period(PERIOD)  # the pre-R-07 query
    assert [(t["date"], t["amount"], t["coa_code"]) for t in ctx["transactions"]] == [
        (t.date.isoformat(), float(t.amount), t.coa_code) for t in before]
    assert ctx["tax_estimate"] is None or ctx["tax_estimate"]["fiscal_year"] == FY
