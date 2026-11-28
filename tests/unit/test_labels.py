"""
tests/unit/test_labels.py  –  card P-12 (shared account-label tokeniser in core)

``ledger_agent.core.labels`` is the one tokeniser for account / institution labels,
used by both the legacy MCP server (P-02) and the AI context export (P-06).  These
tests pin

* the token formats (``<ACCOUNT_n>``, ``<INSTITUTION_n>``, ``<ACCT_MASKED>``,
  ``<ACCOUNT>``) and their numbering: accounts by account id, institutions by name,
  both from 1, stable across calls and across insertion order;
* the label-map shapes (``inst – name`` and ``Credit Card Payable – inst (name)``)
  and the account-line fallback;
* entity scoping: every entity gets its own numbering and its own map;
* that the legacy MCP server and the context export emit identical tokens for the
  same database, and neither keeps a private copy of the tokeniser.

Synthetic tmp_path databases and pseudonymous names only; no real data.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from ledger_agent.core import config as _cfg
from ledger_agent.core import privacy

FY = 2026
PERIOD = f"{FY}-01"

ENT_A = "ent-p12-a"
ENT_B = "ent-p12-b"

# Account ids deliberately sort differently from the repo's (institution, name) order,
# so the id-based numbering is actually exercised.
A_BRK = "acct-p12-a-1"   # BROKER_Y brokerage
A_CHK = "acct-p12-a-2"   # BANK_X2 checking
A_CC = "acct-p12-a-3"    # BANK_X2 credit card
B_CHK = "acct-p12-b-1"   # BANK_X checking

EXPECTED_A = {
    A_BRK: ("<ACCOUNT_1>", "<INSTITUTION_2>"),
    A_CHK: ("<ACCOUNT_2>", "<INSTITUTION_1>"),
    A_CC: ("<ACCOUNT_3>", "<INSTITUTION_1>"),
}
EXPECTED_B = {B_CHK: ("<ACCOUNT_1>", "<INSTITUTION_1>")}
EXPECTED_MAP_A = {
    "BROKER_Y – Margin": "<INSTITUTION_2> – <ACCOUNT_1>",
    "BANK_X2 – Operating": "<INSTITUTION_1> – <ACCOUNT_2>",
    "BANK_X2 – Card": "<INSTITUTION_1> – <ACCOUNT_3>",
    "Credit Card Payable – BROKER_Y (Margin)":
        "Credit Card Payable – <INSTITUTION_2> (<ACCOUNT_1>)",
    "Credit Card Payable – BANK_X2 (Operating)":
        "Credit Card Payable – <INSTITUTION_1> (<ACCOUNT_2>)",
    "Credit Card Payable – BANK_X2 (Card)":
        "Credit Card Payable – <INSTITUTION_1> (<ACCOUNT_3>)",
}
RAW_NAMES = ("BROKER_Y", "BANK_X2", "BANK_X", "Margin", "Operating", "ENTITY_A", "ENTITY_B")
ACCOUNT_CODES = ("1010", "1100_acct", "2030")
TOKEN_LABEL_RE = re.compile(r"<INSTITUTION_\d+>")


@pytest.fixture(autouse=True)
def _privacy_env(monkeypatch):
    monkeypatch.setattr(_cfg, "AI_EGRESS_MODE", "redact")
    monkeypatch.setattr(_cfg, "PRIVACY_ENTITY_NAME", "")
    monkeypatch.delenv("FI_PRIVACY_ENTITY_NAME", raising=False)
    privacy._reset_session()
    yield
    privacy._reset_session()


def _seed(db_file: Path, *, reverse: bool = False) -> None:
    from ledger_agent.core.database import (
        AccountRepo,
        EntityRepo,
        PositionRepo,
        SnapshotRepo,
        init_db,
    )
    from ledger_agent.core.models import (
        Account,
        AccountSnapshot,
        AccountType,
        Entity,
        Position,
    )

    init_db(db_file)
    EntityRepo.upsert(Entity(id=ENT_A, name="ENTITY_A HOLDINGS LLC", entity_type="LLC",
                             state="MO"), db_file)
    EntityRepo.upsert(Entity(id=ENT_B, name="ENTITY_B VENTURES LLC", entity_type="LLC",
                             state="CA"), db_file)
    accounts = [
        Account(id=A_BRK, entity_id=ENT_A, name="Margin", institution="BROKER_Y",
                account_type=AccountType.BROKERAGE, account_number_masked="0000"),
        Account(id=A_CHK, entity_id=ENT_A, name="Operating", institution="BANK_X2",
                account_type=AccountType.CHECKING, account_number_masked="0000"),
        Account(id=A_CC, entity_id=ENT_A, name="Card", institution="BANK_X2",
                account_type=AccountType.CREDIT_CARD, account_number_masked=""),
        Account(id=B_CHK, entity_id=ENT_B, name="Operating", institution="BANK_X",
                account_type=AccountType.CHECKING, account_number_masked="0000"),
    ]
    for acct in (reversed(accounts) if reverse else accounts):
        AccountRepo.upsert(acct, db_file)
    for acct_id, bal in ((A_BRK, "1500"), (A_CHK, "2400"), (A_CC, "310"), (B_CHK, "800")):
        SnapshotRepo.upsert(AccountSnapshot(
            account_id=acct_id, statement_period=PERIOD, ending_balance=Decimal(bal),
        ), db_file)
    PositionRepo.upsert_period([Position(
        account_id=A_BRK, symbol="TICKER_SEC1", name="TICKER_SEC1",
        quantity=Decimal("10"), price_per_unit=Decimal("150"),
        market_value=Decimal("1500"), statement_period=PERIOD,
    )], db_file)


@pytest.fixture
def labels_db(tmp_path, monkeypatch):
    db_file = tmp_path / "p12.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    _seed(db_file)
    return db_file


def _legacy_call(capsys, tool: str, args: dict) -> dict:
    from mcp_server import server as legacy

    capsys.readouterr()
    legacy._dispatch({"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                      "params": {"name": tool, "arguments": args}})
    (line,) = capsys.readouterr().out.strip().splitlines()
    resp = json.loads(line)
    assert "error" not in resp and not resp["result"].get("isError")
    return json.loads(resp["result"]["content"][0]["text"])


# ── 1. Token formats, numbering and stability ─────────────────────────────────

def test_constants_keep_their_token_formats():
    from ledger_agent.core import labels

    assert labels.ACCOUNT_FALLBACK == "<ACCOUNT>"
    assert labels.MASKED_TOKEN == "<ACCT_MASKED>"
    assert labels.ACCOUNT_LINE_CODES == frozenset(ACCOUNT_CODES)


def test_account_tokens_numbered_by_id_and_institution_name(labels_db):
    from ledger_agent.core.labels import account_tokens

    assert account_tokens(ENT_A) == EXPECTED_A


def test_account_tokens_stable_across_calls_and_insertion_order(labels_db, tmp_path,
                                                                 monkeypatch):
    from ledger_agent.core.labels import account_label_map, account_tokens

    first = (account_tokens(ENT_A), account_label_map(ENT_A))
    assert (account_tokens(ENT_A), account_label_map(ENT_A)) == first

    other = tmp_path / "p12_reversed.db"
    monkeypatch.setenv("FI_DB_PATH", str(other))
    _seed(other, reverse=True)
    assert (account_tokens(ENT_A), account_label_map(ENT_A)) == first


def test_account_label_map_shapes(labels_db):
    from ledger_agent.core.labels import account_label_map

    assert account_label_map(ENT_A) == EXPECTED_MAP_A


def test_account_line_label_token_fallback_and_passthrough(labels_db):
    from ledger_agent.core.labels import ACCOUNT_FALLBACK, account_label_map, account_line_label

    label_map = account_label_map(ENT_A)
    known = SimpleNamespace(label="BANK_X2 – Operating", coa_code="1010")
    assert account_line_label(known, label_map) == "<INSTITUTION_1> – <ACCOUNT_2>"
    for code in ACCOUNT_CODES:
        unknown = SimpleNamespace(label="BANK_X2 unexpected shape", coa_code=code)
        assert account_line_label(unknown, label_map) == ACCOUNT_FALLBACK
    other = SimpleNamespace(label="Total Assets", coa_code="")
    assert account_line_label(other, label_map) is None
    # An empty map (tokeniser unavailable) still never passes an account label through.
    assert account_line_label(known, {}) == ACCOUNT_FALLBACK


# ── 2. Entity scoping ─────────────────────────────────────────────────────────

def test_each_entity_has_independent_numbering_and_map(labels_db):
    from ledger_agent.core.labels import account_label_map, account_tokens

    assert account_tokens(ENT_B) == EXPECTED_B
    assert account_label_map(ENT_B) == {
        "BANK_X – Operating": "<INSTITUTION_1> – <ACCOUNT_1>",
        "Credit Card Payable – BANK_X (Operating)":
            "Credit Card Payable – <INSTITUTION_1> (<ACCOUNT_1>)",
    }
    # Neither map knows the other entity's accounts.
    assert not set(account_tokens(ENT_A)) & set(account_tokens(ENT_B))
    assert "BANK_X – Operating" not in account_label_map(ENT_A)
    assert not any(k.startswith(("BROKER_Y", "BANK_X2")) for k in account_label_map(ENT_B))


def test_unknown_entity_gets_empty_maps(labels_db):
    from ledger_agent.core.labels import account_label_map, account_tokens

    assert account_tokens("ent-p12-missing") == {}
    assert account_label_map("ent-p12-missing") == {}


# ── 3. Both egress paths agree (cross-check) ──────────────────────────────────

def test_context_export_and_legacy_mcp_emit_identical_tokens(labels_db, capsys):
    from adapters.context_builder import build_context
    from ledger_agent.core.labels import account_label_map, account_tokens

    ctx = build_context(ENT_A, period=PERIOD)
    ctx_labels = sorted(
        ln["label"] for ln in ctx["balance_sheet"]["lines"] if ln["coa_code"] in ACCOUNT_CODES
    )
    bs = _legacy_call(capsys, "get_balance_sheet", {"period": PERIOD, "entity_id": ENT_A})
    legacy_labels = sorted(ln["label"] for ln in bs["lines"]
                           if TOKEN_LABEL_RE.search(ln["label"] or ""))

    # Non-vacuous: one line per account, all of them tokenised from the shared map.
    assert len(ctx_labels) == 3
    assert set(ctx_labels) <= set(account_label_map(ENT_A).values())
    assert legacy_labels == ctx_labels

    # Positions in the export use the same (institution, account) tokens.
    acct_tok, inst_tok = account_tokens(ENT_A)[A_BRK]
    assert [p["account"] for p in ctx["positions"]] == [f"{inst_tok} – {acct_tok}"]

    # The legacy entity summary lists the same tokens in account-id order.
    summary = _legacy_call(capsys, "get_entity_summary", {"entity_id": ENT_A})
    assert [(a["name"], a["institution"]) for a in summary["accounts"]] == [
        EXPECTED_A[k] for k in sorted(EXPECTED_A)
    ]
    assert [a["masked"] for a in summary["accounts"]] == ["<ACCT_MASKED>", "<ACCT_MASKED>", ""]

    text = json.dumps([ctx, bs, summary], ensure_ascii=False).casefold()
    assert sum(1 for n in RAW_NAMES if n.casefold() in text) == 0  # count only


def test_patching_core_tokeniser_reaches_both_egress_paths(labels_db, capsys, monkeypatch):
    """Both paths look the tokeniser up in core at call time: one source of truth."""
    from adapters.context_builder import build_context
    from ledger_agent.core import labels

    monkeypatch.setattr(labels, "account_label_map", lambda _eid: {})
    ctx = build_context(ENT_A, period=PERIOD)
    bs = _legacy_call(capsys, "get_balance_sheet", {"period": PERIOD, "entity_id": ENT_A})

    assert [ln["label"] for ln in ctx["balance_sheet"]["lines"]
            if ln["coa_code"] in ACCOUNT_CODES] == ["<ACCOUNT>"] * 3
    assert sum(1 for ln in bs["lines"] if ln["label"] == "<ACCOUNT>") == 3
    assert not any(TOKEN_LABEL_RE.search(ln["label"] or "") for ln in bs["lines"])


def test_no_private_tokeniser_copy_left_in_wrappers():
    from mcp_server import server as legacy

    for name in ("_account_tokens", "_account_label_map", "_ACCOUNT_FALLBACK",
                 "_ACCOUNT_LINE_CODES", "_MASKED_TOKEN"):
        assert not hasattr(legacy, name), name
    root = Path(__file__).resolve().parents[2]
    builder_src = (root / "adapters" / "context_builder.py").read_text(encoding="utf-8")
    assert "mcp_server" not in builder_src
