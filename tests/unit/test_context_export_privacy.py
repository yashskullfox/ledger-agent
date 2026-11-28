"""
tests/unit/test_context_export_privacy.py  –  P-06 fail-closed redaction

The AI context export (``adapters/context_builder.py``) is egress: it is pasted into
an LLM.  These tests pin that

* no entity / partner / institution / account name and no (masked) account number
  appears anywhere in the serialized context or in the prompt text;
* a redaction error drops the field (``<REDACTED>``), never falls back to raw text;
* an ``audit_egress`` failure aborts the export and writes nothing;
* figures (balances, transaction amounts, tax numbers) are unchanged by redaction;
* ``ClassificationMemory.remember`` never persists a raw pattern when redaction fails.

Synthetic tmp_path databases and pseudonymous names only; no real data.

P-16: ``generated_at`` is frozen for every test.  With the real clock, its 6 microsecond
digits contain the synthetic last-4 about 3 times in 10,000 builds, which made the needle
scan fail intermittently on a timestamp, not on leaked text.  Needle hits are reported
as category names only, never the scanned text.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from decimal import Decimal

import pytest

from ledger_agent.core import config as _cfg
from ledger_agent.core import privacy

FY = 2026
PERIOD = f"{FY}-01"
ENTITY_ID = "ent-p06"
ENTITY_NAME = "ENTITY_A HOLDINGS LLC"
PARTNER_NAME = "Alex Testpartner"
ACCT_NUMBER = "4400123456789"  # redaction: allow (synthetic, not Luhn-valid)
MASK_LAST4 = "7731"  # redaction: allow (synthetic)

CHK = "acct-p06-chk"
CC = "acct-p06-cc"
BRK = "acct-p06-brk"

# category → needle (lower-cased); none may appear in the export (str.casefold()).
NEEDLES = {
    "entity_name": "entity_a",          # also inside an account name
    "partner_name": "testpartner",      # in a card account name and a description
    "institution_bank": "bank_x3",
    "institution_broker": "broker_z",
    "account_name": "reservefund",
    "masked_last4": MASK_LAST4,         # in an account name and a description
    "account_number": ACCT_NUMBER,      # full number in a description
}

# P-16: fixed export timestamp; its digits hold none of the needles.
FROZEN_NOW = "2026-01-31T00:00:00+00:00"

ACCOUNT_LABEL_RE = re.compile(r"^<INSTITUTION_\d+> – <ACCOUNT_\d+>$")


@pytest.fixture(autouse=True)
def _privacy_env(monkeypatch):
    import adapters.context_builder as context_builder

    monkeypatch.setattr(context_builder, "_now_iso", lambda: FROZEN_NOW)  # P-16
    monkeypatch.setattr(_cfg, "AI_EGRESS_MODE", "redact")
    monkeypatch.setattr(_cfg, "PRIVACY_ENTITY_NAME", "")
    monkeypatch.delenv("FI_PRIVACY_ENTITY_NAME", raising=False)
    monkeypatch.setenv("FI_PARTNER_1_NAME", PARTNER_NAME)
    privacy._reset_session()
    yield
    privacy._reset_session()


@pytest.fixture
def p06_db(tmp_path, monkeypatch):
    """Entity + checking / credit card / brokerage accounts whose names identify."""
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

    db_file = tmp_path / "p06.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    init_db(db_file)
    EntityRepo.upsert(
        Entity(id=ENTITY_ID, name=ENTITY_NAME, entity_type="LLC", state="MO"), db_file
    )
    AccountRepo.upsert(Account(
        id=CHK, entity_id=ENTITY_ID, name=f"ENTITY_A RESERVEFUND x{MASK_LAST4}",
        institution="BANK_X3", account_type=AccountType.CHECKING,
        account_number_masked=f"****{MASK_LAST4}",
    ), db_file)
    AccountRepo.upsert(Account(
        id=CC, entity_id=ENTITY_ID, name=f"Card {PARTNER_NAME}",
        institution="BANK_X3", account_type=AccountType.CREDIT_CARD,
        account_number_masked="0000",
    ), db_file)
    AccountRepo.upsert(Account(
        id=BRK, entity_id=ENTITY_ID, name="BROKER_Z Margin",
        institution="BROKER_Z", account_type=AccountType.BROKERAGE,
        account_number_masked="0000",
    ), db_file)
    txns = [
        ("Capital contribution Entity_A Holdings LLC", "3010", "1000.00"),
        (f"Reimbursement {PARTNER_NAME}", "5010", "-42.00"),
        (f"ACH DEBIT ACCOUNT {ACCT_NUMBER} ONLINE", "5010", "-17.50"),
        (f"CARD AUTOPAY ****{MASK_LAST4} THANK YOU", "5010", "-8.25"),
        ("DEPOSIT CLIENT INVOICE", "4020", "5000.00"),
    ]
    TransactionRepo.bulk_insert([
        Transaction(
            id=f"txn-p06-{i}", account_id=CHK, date=date(FY, 1, 5 + i),
            description=desc, raw_description=desc, amount=Decimal(amt),
            transaction_type=TransactionType.CREDIT if Decimal(amt) > 0
            else TransactionType.DEBIT,
            statement_period=PERIOD, coa_code=code, coa_name="synthetic",
        )
        for i, (desc, code, amt) in enumerate(txns)
    ], db_file)
    for month in range(1, 13):
        for acct, bal in ((CHK, "2500"), (CC, "310"), (BRK, "1500")):
            SnapshotRepo.upsert(AccountSnapshot(
                account_id=acct, statement_period=f"{FY}-{month:02d}",
                ending_balance=Decimal(bal),
            ), db_file)
    PositionRepo.upsert_period([Position(
        account_id=BRK, symbol="TICKER_SEC1", name="TICKER_SEC1",
        quantity=Decimal("10"), price_per_unit=Decimal("150"),
        market_value=Decimal("1500"), statement_period=PERIOD,
    )], db_file)
    return db_file


@pytest.fixture
def export_root(tmp_path, monkeypatch):
    """save_context only writes inside config.DATA_DIR (X-06): point it at tmp_path."""
    from ledger_agent.core import config as core_config

    monkeypatch.setattr(core_config, "DATA_DIR", tmp_path)
    return tmp_path


def _build():
    from adapters.context_builder import build_context

    return build_context(ENTITY_ID, period=PERIOD)


def _egress_text(ctx) -> str:
    from adapters.context_builder import context_to_prompt

    return json.dumps(ctx, ensure_ascii=False, default=str) + "\n" + context_to_prompt(ctx)


def _hits(text: str) -> list[str]:
    """Categories of the needles found in ``text`` (names only, never the values)."""
    folded = text.casefold()
    return [cat for cat, needle in NEEDLES.items() if needle.casefold() in folded]


def _assert_no_identifier(text: str) -> None:
    found = _hits(text)  # a local, so a failure shows categories, not the text
    assert found == [], f"identifier categories in the egress text: {found}"


def _lines_by_code(ctx, code: str) -> list[dict]:
    return [ln for ln in ctx["balance_sheet"]["lines"] if ln["coa_code"] == code]


def _boom(*_a, **_kw):
    raise RuntimeError("redactor unavailable")


# ── 1. Nothing identifying in the context or the prompt ───────────────────────

def test_context_and_prompt_carry_no_identifier(p06_db):
    ctx = _build()
    text = _egress_text(ctx)
    _assert_no_identifier(text)
    assert privacy.contains_account_reference(text) is None
    assert not privacy.contains_protected_name(text)
    privacy.audit_egress(ctx)  # does not raise


def test_generated_at_is_frozen(p06_db):
    """P-16: the export timestamp is fixed, so the needle scan cannot depend on the clock."""
    assert _build()["generated_at"] == FROZEN_NOW
    assert _hits(FROZEN_NOW) == []


def test_clock_digits_can_match_a_needle_without_a_leak(p06_db, monkeypatch):
    """P-16 root cause, pinned: real-clock microseconds can hold the synthetic last-4.

    That is a needle hit on the timestamp, not a leaked identifier: the redactor and
    the audit rightly see no account reference.  Hence the frozen clock above."""
    import adapters.context_builder as context_builder

    stamp = f"2026-11-28T10:20:30.0{MASK_LAST4}0+00:00"
    monkeypatch.setattr(context_builder, "_now_iso", lambda: stamp)
    ctx = _build()  # the real audit passes
    found = _hits(_egress_text(ctx))
    assert found == ["masked_last4"]
    ctx.pop("generated_at")
    _assert_no_identifier(_egress_text(ctx))
    assert privacy.contains_account_reference(stamp) is None


def test_non_vacuous_seed_does_contain_identifiers(p06_db):
    """The synthetic data really holds the needles (so test 1 is not vacuous)."""
    from ledger_agent.core.database import AccountRepo, TransactionRepo

    raw = " ".join(
        [f"{a.institution} {a.name}" for a in AccountRepo.list_for_entity(ENTITY_ID)]
        + [t.description for t in TransactionRepo.list_for_period(PERIOD)]
        + [ENTITY_NAME]
    )
    found = _hits(raw)
    assert found == list(NEEDLES)


def test_labels_use_stable_placeholders(p06_db):
    ctx = _build()
    assert ctx["entity"]["name"] == "<ENTITY_NAME>"
    assert "<ENTITY_NAME>" in ctx["ai_instructions"]
    (cash,) = _lines_by_code(ctx, "1010")
    assert ACCOUNT_LABEL_RE.match(cash["label"])
    (brk,) = _lines_by_code(ctx, "1100_acct")
    assert ACCOUNT_LABEL_RE.match(brk["label"])
    (card,) = _lines_by_code(ctx, "2030")
    assert re.match(r"^Credit Card Payable – <INSTITUTION_\d+> \(<ACCOUNT_\d+>\)$",
                    card["label"])
    # Same institution → same institution token; different accounts → different tokens.
    assert cash["label"].split(" – ")[0] in card["label"]
    assert cash["label"] != brk["label"]
    # Positions: account label tokenised and equal to the balance-sheet line; ticker kept.
    (pos,) = ctx["positions"]
    assert pos["account"] == brk["label"]
    assert pos["symbol"] == "TICKER_SEC1"
    # Transactions: account number / masked digits tokenised.
    descs = " ".join(t["description"] for t in ctx["transactions"])
    assert "<ACCT_" in descs


def test_unknown_account_label_shape_falls_back_to_fixed_token(p06_db, monkeypatch):
    from ledger_agent.core import labels  # P-12: tokeniser moved out of mcp_server

    monkeypatch.setattr(labels, "account_label_map", lambda _eid: {})
    ctx = _build()
    for code in ("1010", "1100_acct", "2030"):
        assert [ln["label"] for ln in _lines_by_code(ctx, code)] == ["<ACCOUNT>"]
    _assert_no_identifier(_egress_text(ctx))


def test_account_tokeniser_failure_fails_closed(p06_db, monkeypatch):
    from ledger_agent.core import labels  # P-12: tokeniser moved out of mcp_server

    monkeypatch.setattr(labels, "account_tokens", _boom)
    ctx = _build()
    assert [p["account"] for p in ctx["positions"]] == ["<REDACTED>"]
    for code in ("1010", "1100_acct", "2030"):
        assert [ln["label"] for ln in _lines_by_code(ctx, code)] == ["<ACCOUNT>"]
    _assert_no_identifier(_egress_text(ctx))


# ── 2. Redaction error → field dropped, never raw ─────────────────────────────

def test_redaction_error_drops_every_free_text_field(p06_db, monkeypatch):
    monkeypatch.setattr(privacy, "redact", _boom)
    ctx = _build()
    assert {t["description"] for t in ctx["transactions"]} == {"<REDACTED>"}
    assert ctx["ai_instructions"] == "<REDACTED>"
    assert ctx["entity"] == {"name": "<ENTITY_NAME>", "type": "<REDACTED>",
                             "state": "<REDACTED>"}
    account_codes = {"1010", "1100_acct", "2030"}
    for ln in ctx["balance_sheet"]["lines"]:
        if ln["coa_code"] in account_codes:
            assert ACCOUNT_LABEL_RE.match(ln["label"]) or ln["label"].startswith(
                "Credit Card Payable – <INSTITUTION_")
        else:
            assert ln["label"] == "<REDACTED>"
    _assert_no_identifier(_egress_text(ctx))


def test_redaction_returning_non_string_is_dropped(p06_db, monkeypatch):
    monkeypatch.setattr(privacy, "redact", lambda *_a, **_kw: (None, {}))
    ctx = _build()
    assert {t["description"] for t in ctx["transactions"]} == {"<REDACTED>"}
    _assert_no_identifier(_egress_text(ctx))


# ── 3. audit_egress failure → export aborted, nothing written ─────────────────

def _leaky_audit(*_a, **_kw):
    raise privacy.PrivacyLeakError("audit says no: SECRET-VALUE-XYZ")


def test_audit_failure_aborts_build_without_echo(p06_db, monkeypatch):
    from adapters.context_builder import ContextExportBlocked

    monkeypatch.setattr(privacy, "audit_egress", _leaky_audit)
    with pytest.raises(ContextExportBlocked) as exc_info:
        _build()
    assert "SECRET-VALUE-XYZ" not in str(exc_info.value)
    assert "nothing was written" in str(exc_info.value)


def test_audit_failure_on_save_writes_nothing(p06_db, tmp_path, export_root, monkeypatch):
    from adapters.context_builder import ContextExportBlocked, save_context

    ctx = _build()
    monkeypatch.setattr(privacy, "audit_egress", _leaky_audit)
    out = tmp_path / "exports" / "ai_context.json"
    with pytest.raises(ContextExportBlocked):
        save_context(ctx, out)
    assert not out.exists()
    assert not out.parent.exists()


def test_audit_failure_on_prompt_raises(p06_db, monkeypatch):
    from adapters.context_builder import ContextExportBlocked, context_to_prompt

    ctx = _build()
    monkeypatch.setattr(privacy, "audit_egress", _leaky_audit)
    with pytest.raises(ContextExportBlocked):
        context_to_prompt(ctx)


def test_real_audit_blocks_a_tampered_context(p06_db, tmp_path, export_root):
    """A context carrying a raw name (e.g. built by hand) is refused by the real audit."""
    from adapters.context_builder import ContextExportBlocked, save_context

    ctx = _build()
    ctx["entity"]["name"] = ENTITY_NAME
    out = tmp_path / "exports" / "tampered.json"
    with pytest.raises(ContextExportBlocked):
        save_context(ctx, out)
    assert not out.exists()


def test_save_context_writes_the_audited_file(p06_db, tmp_path, export_root):
    from adapters.context_builder import save_context

    out = save_context(_build(), tmp_path / "exports" / "ai_context.json")
    text = out.read_text(encoding="utf-8")
    _assert_no_identifier(text)
    assert json.loads(text)["entity"]["name"] == "<ENTITY_NAME>"


# ── 4. Figures are unchanged by redaction ─────────────────────────────────────

def _figures(ctx) -> dict:
    bs = ctx["balance_sheet"]
    return {
        "totals": [bs[k] for k in ("total_assets", "total_liabilities",
                                   "total_equity", "net_income")],
        "lines": [(ln["coa_code"], ln["amount"]) for ln in bs["lines"]],
        "txns": sorted(t["amount"] for t in ctx["transactions"]),
        "positions": [(p["quantity"], p["price"], p["market_value"])
                      for p in ctx["positions"]],
        "tax": ctx["tax_estimate"],
        "summary": ctx["summary"],
    }


def test_figures_match_the_engine(p06_db):
    from ledger_agent.core import api
    from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder

    ctx = _build()
    bs = BalanceSheetBuilder(ENTITY_ID, PERIOD).build()
    fig = _figures(ctx)
    assert fig["totals"] == [float(bs.total_assets), float(bs.total_liabilities),
                             float(bs.total_equity), float(bs.net_income)]
    assert fig["lines"] == [(ln.coa_code, float(ln.amount)) for ln in bs.lines]
    assert fig["txns"] == sorted([1000.0, -42.0, -17.5, -8.25, 5000.0])
    assert fig["positions"] == [(10.0, 150.0, 1500.0)]
    est = api.pte_estimate(FY)
    assert ctx["tax_estimate"] is not None
    assert ctx["tax_estimate"]["total_annual_tax"] == float(est.total_annual_tax)
    assert ctx["tax_estimate"]["annual_entity_taxable_income"] == float(est.net_income)
    assert f"${float(bs.net_income):,.2f}" in ctx["ai_instructions"]
    assert f"${float(bs.total_assets):,.2f}" in ctx["ai_instructions"]


def test_figures_survive_a_redaction_failure(p06_db, monkeypatch):
    baseline = _figures(_build())
    monkeypatch.setattr(privacy, "redact", _boom)
    assert _figures(_build()) == baseline


# ── 5. Memory rules fail closed ───────────────────────────────────────────────

RAW_RULE = f"ACH ACCOUNT {ACCT_NUMBER} {PARTNER_NAME}"


def _memory(tmp_path):
    from ledger_agent.core.intelligence.memory import ClassificationMemory

    return ClassificationMemory(tmp_path / "memory.json")


def test_memory_rule_not_persisted_on_redaction_error(tmp_path, monkeypatch, caplog):
    mem = _memory(tmp_path)
    before_rules = mem.list_rules()
    before_file = (tmp_path / "memory.json").read_text()
    monkeypatch.setattr(privacy, "redact", _boom)
    with caplog.at_level(logging.WARNING):
        mem.remember(RAW_RULE, "5010", "Software & Subscriptions")  # does not raise
    assert mem.list_rules() == before_rules
    assert (tmp_path / "memory.json").read_text() == before_file
    assert ACCT_NUMBER not in (tmp_path / "memory.json").read_text()
    assert any("memory rule not saved" in r.getMessage() for r in caplog.records)
    assert not any(ACCT_NUMBER in r.getMessage() or "Testpartner" in r.getMessage()
                   for r in caplog.records)


def test_memory_existing_rule_untouched_on_redaction_error(tmp_path, monkeypatch):
    mem = _memory(tmp_path)
    mem.remember("MONTHLY SOFTWARE", "5010", "Software & Subscriptions")
    (rule,) = [r for r in mem.list_rules() if r["pattern"] == "MONTHLY SOFTWARE"]
    assert rule["confirmed_count"] == 1
    monkeypatch.setattr(privacy, "redact", _boom)
    mem.remember("MONTHLY SOFTWARE", "5020", "Bank & Transaction Fees")
    assert rule["coa_code"] == "5010" and rule["confirmed_count"] == 1


def test_memory_rule_persisted_redacted_when_redaction_works(tmp_path):
    mem = _memory(tmp_path)
    mem.remember(RAW_RULE, "5010", "Software & Subscriptions")
    text = (tmp_path / "memory.json").read_text()
    assert ACCT_NUMBER not in text
    assert "testpartner" not in text.casefold()
    assert len(mem.list_rules()) >= 1
