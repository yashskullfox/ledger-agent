"""
tests/unit/test_context_export_storage.py  –  X-06 hardened AI-context file write

``adapters.context_builder.save_context`` (CodeQL "clear-text storage" alert) must

* create the file owner-only (0600) — also when the file already existed (POSIX);
* refuse a target that resolves outside ``config.DATA_DIR`` (``..`` or a symlink
  included) and create no file or directory;
* rebuild the context from the field allow-list — an unknown field or a wrongly
  typed value blocks the export, and nothing is created;
* write UTF-8 (non-ASCII text round-trips) without changing the exported content;
* keep its callers working: root ``main.py context`` and the quick-scan export.

Synthetic tmp_path databases and pseudonymous names only; no real data.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from datetime import date
from decimal import Decimal

import pytest

from ledger_agent.core import config as _cfg
from ledger_agent.core import privacy

FY = 2026
PERIOD = f"{FY}-01"
ENTITY_ID = "ent-x06"
CHK = "acct-x06-chk"
AI_CONTEXT_CHOICE = "AI Context JSON (for Claude / GPT / Perplexity)"

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")


@pytest.fixture(autouse=True)
def _privacy_env(monkeypatch):
    monkeypatch.setattr(_cfg, "AI_EGRESS_MODE", "redact")
    monkeypatch.setattr(_cfg, "PRIVACY_ENTITY_NAME", "")
    monkeypatch.delenv("FI_PRIVACY_ENTITY_NAME", raising=False)
    privacy._reset_session()
    yield
    privacy._reset_session()


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """``config.DATA_DIR`` (and the exports dir callers use) under tmp_path."""
    import config as legacy_shim_config  # root shim; root main.py reads EXPORTS_DIR from it
    from ledger_agent.core import config as core_config

    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(core_config, "DATA_DIR", root)
    monkeypatch.setattr(core_config, "EXPORTS_DIR", root / "exports")
    monkeypatch.setattr(legacy_shim_config, "EXPORTS_DIR", root / "exports")
    return root


@pytest.fixture
def x06_db(tmp_path, monkeypatch):
    from ledger_agent.core.database import (
        AccountRepo,
        EntityRepo,
        SnapshotRepo,
        TransactionRepo,
        init_db,
    )
    from ledger_agent.core.models import (
        Account,
        AccountSnapshot,
        AccountType,
        Entity,
        Transaction,
        TransactionType,
    )

    db_file = tmp_path / "x06.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    init_db(db_file)
    EntityRepo.upsert(
        Entity(id=ENTITY_ID, name="ENTITY_A STORAGE LLC", entity_type="LLC", state="MO"),
        db_file,
    )
    AccountRepo.upsert(Account(
        id=CHK, entity_id=ENTITY_ID, name="Operating", institution="BANK_X",
        account_type=AccountType.CHECKING, account_number_masked="0000",
    ), db_file)
    TransactionRepo.bulk_insert([
        Transaction(
            id=f"txn-x06-{i}", account_id=CHK, date=date(FY, 1, 5 + i),
            description=desc, raw_description=desc, amount=Decimal(amt),
            transaction_type=TransactionType.CREDIT if Decimal(amt) > 0
            else TransactionType.DEBIT,
            statement_period=PERIOD, coa_code=code, coa_name="synthetic",
        )
        for i, (desc, code, amt) in enumerate((
            ("DEPOSIT CLIENT INVOICE", "4020", "5000.00"),
            ("MONTHLY SERVICE FEE", "5010", "-12.00"),
        ))
    ], db_file)
    SnapshotRepo.upsert(AccountSnapshot(
        account_id=CHK, statement_period=PERIOD, ending_balance=Decimal("4988"),
    ), db_file)
    return db_file


def _ctx():
    from adapters.context_builder import build_context

    return build_context(ENTITY_ID, period=PERIOD)


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _entity():
    from ledger_agent.core.database import EntityRepo

    return next(e for e in EntityRepo.list_all() if e.id == ENTITY_ID)


# ── 1. Owner-only file ────────────────────────────────────────────────────────

@posix_only
def test_written_file_is_owner_only(x06_db, data_root):
    from adapters.context_builder import save_context

    out = save_context(_ctx(), data_root / "exports" / "ai_context.json")
    assert _mode(out) == 0o600


@posix_only
def test_existing_file_is_tightened_to_owner_only(x06_db, data_root):
    from adapters.context_builder import save_context

    out = data_root / "exports" / "ai_context.json"
    out.parent.mkdir(parents=True)
    out.write_text("stale content that is longer than nothing", encoding="utf-8")
    out.chmod(0o644)
    save_context(_ctx(), out)
    assert _mode(out) == 0o600
    assert json.loads(out.read_text(encoding="utf-8"))["period"] == PERIOD


# ── 2. Path guard ─────────────────────────────────────────────────────────────

def test_path_outside_data_dir_is_refused(x06_db, data_root, tmp_path):
    from adapters.context_builder import ContextExportPathError, save_context

    out = tmp_path / "elsewhere" / "ai_context.json"
    with pytest.raises(ContextExportPathError) as exc_info:
        save_context(_ctx(), out)
    assert str(tmp_path) not in str(exc_info.value)
    assert not out.exists()
    assert not out.parent.exists()


def test_dotdot_escape_is_refused(x06_db, data_root, tmp_path):
    from adapters.context_builder import ContextExportPathError, save_context

    out = data_root / "exports" / ".." / ".." / "escaped" / "ai_context.json"
    with pytest.raises(ContextExportPathError):
        save_context(_ctx(), out)
    assert not (tmp_path / "escaped").exists()
    assert not (data_root / "exports").exists()


def test_data_dir_itself_is_refused(x06_db, data_root):
    from adapters.context_builder import ContextExportPathError, save_context

    with pytest.raises(ContextExportPathError):
        save_context(_ctx(), data_root)


@posix_only
def test_symlink_out_of_data_dir_is_refused(x06_db, data_root, tmp_path):
    from adapters.context_builder import ContextExportPathError, save_context

    outside = tmp_path / "outside"
    outside.mkdir()
    (data_root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContextExportPathError):
        save_context(_ctx(), data_root / "link" / "ai_context.json")
    assert list(outside.iterdir()) == []


# ── 3. Allow-list projection ──────────────────────────────────────────────────

def test_unknown_field_blocks_and_writes_nothing(x06_db, data_root):
    from adapters.context_builder import ContextExportBlocked, save_context

    ctx = _ctx()
    ctx["transactions"][0]["raw_description"] = "SECRET-VALUE-XYZ"
    out = data_root / "exports" / "ai_context.json"
    with pytest.raises(ContextExportBlocked) as exc_info:
        save_context(ctx, out)
    assert "SECRET-VALUE-XYZ" not in str(exc_info.value)
    assert not out.parent.exists()


def test_text_in_a_number_field_blocks(x06_db, data_root):
    from adapters.context_builder import ContextExportBlocked, save_context

    ctx = _ctx()
    ctx["balance_sheet"]["total_assets"] = "SECRET-VALUE-XYZ"
    out = data_root / "exports" / "ai_context.json"
    with pytest.raises(ContextExportBlocked) as exc_info:
        save_context(ctx, out)
    assert "SECRET-VALUE-XYZ" not in str(exc_info.value)
    assert not out.parent.exists()


def test_audit_failure_blocks_before_any_directory(x06_db, data_root, monkeypatch):
    from adapters.context_builder import ContextExportBlocked, save_context

    ctx = _ctx()

    def _fail(*_a, **_kw):
        raise privacy.PrivacyLeakError("audit says no")

    monkeypatch.setattr(privacy, "audit_egress", _fail)
    out = data_root / "exports" / "nested" / "ai_context.json"
    with pytest.raises(ContextExportBlocked):
        save_context(ctx, out)
    assert not (data_root / "exports").exists()


def test_written_content_matches_the_context(x06_db, data_root):
    """The projection drops nothing and changes no value (Decimal → float aside)."""
    from adapters.context_builder import save_context

    ctx = _ctx()
    ctx["summary"]["net_income"] = Decimal("12.5")
    out = save_context(ctx, data_root / "exports" / "ai_context.json")
    written = json.loads(out.read_text(encoding="utf-8"))
    expected = json.loads(json.dumps(ctx, default=float))
    assert written == expected
    assert list(written) == list(ctx)  # key order kept


# ── 4. UTF-8 ──────────────────────────────────────────────────────────────────

def test_non_ascii_text_round_trips_as_utf8(x06_db, data_root):
    from adapters.context_builder import save_context

    ctx = _ctx()
    text = "Frais bancaires – café ✓ 家賃"
    ctx["transactions"][0]["description"] = text
    out = save_context(ctx, data_root / "exports" / "ai_context.json")
    raw = out.read_bytes().decode("utf-8")  # strict: must be valid UTF-8
    assert text in raw  # written as characters, not \\u escapes
    assert json.loads(raw)["transactions"][0]["description"] == text


# ── 5. Callers keep working ───────────────────────────────────────────────────

def test_root_main_context_command_writes_owner_only_export(
        x06_db, data_root, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "dotenv", None)
    import main as root_main
    from ledger_agent.cli import commands

    monkeypatch.setattr(commands, "_get_or_setup_entity", _entity)
    root_main._cmd_context(PERIOD)
    out = data_root / "exports" / f"ai_context_{PERIOD}.json"
    assert json.loads(out.read_text(encoding="utf-8"))["entity"]["name"] == "<ENTITY_NAME>"
    if os.name != "nt":
        assert _mode(out) == 0o600
    assert "Financial Report" in capsys.readouterr().out


def test_quick_scan_ai_context_export_writes_under_exports(x06_db, data_root, monkeypatch):
    from ledger_agent.cli import prompts, quick_scan

    monkeypatch.setattr(prompts, "ask_select", lambda *_a, **_kw: AI_CONTEXT_CHOICE)
    quick_scan._offer_exports(_entity(), PERIOD, [PERIOD])
    out = data_root / "exports" / f"ai_context_{PERIOD}.json"
    assert json.loads(out.read_text(encoding="utf-8"))["period"] == PERIOD
    if os.name != "nt":
        assert _mode(out) == 0o600
