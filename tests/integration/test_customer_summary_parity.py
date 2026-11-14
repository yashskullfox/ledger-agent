"""
tests/integration/test_customer_summary_parity.py
=================================================
Four-way synthetic parity test across all four forms:
- Form A: Python library API (api.build_customer_summary)
- Form B: CLI command (cmd_summary --no-prompt)
- Form C: MCP tool (call_tool("customer_summary", ...))
- Form D: Bridge JSON-RPC stdio dispatch (_dispatch)

Verifies:
1. Divergence < 1 dollar across ordinary business income, assets, liabilities,
   equity, and PTE estimate per R-51 / ARCH-32.
2. Identical confidence flags across all 4 forms.
3. Bridge server_info exposes "customer_summary" in its method manifest.
4. "Not tax advice" disclaimer is present across all 4 forms.
5. Error handling for missing parameters in Bridge.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import config
import ledger_agent.core.api as api
import ledger_agent.core.config as core_config
from ledger_agent.bridge.jsonrpc_stdio import _dispatch
from ledger_agent.cli.main import cmd_summary
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
from ledger_agent.mcp.tools import call_tool

NOT_TAX_ADVICE_DISCLAIMER = core_config.NOT_TAX_ADVICE_DISCLAIMER


@pytest.fixture
def synthetic_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create an isolated temporary SQLite database populated with clean synthetic records."""
    db_file = tmp_path / "synthetic_financials.db"

    # Set FI_DB_PATH in env and on config modules
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(core_config, "DB_PATH", db_file)

    init_db(db_file)

    # 1 Entity
    entity = Entity(
        id="ent-synth-001",
        name="Synthetic LLC",
        entity_type="LLC",
        state="DE",
    )
    EntityRepo.upsert(entity, db_file)

    # 1 Checking Account
    account = Account(
        id="acct-synth-001",
        entity_id=entity.id,
        name="Operating Checking",
        institution="BANK_X",
        account_type=AccountType.CHECKING,
        account_number_masked="1234",
    )
    AccountRepo.upsert(account, db_file)

    # 2 Transactions (1 revenue COA 4020, 1 expense COA 5010)
    txns = [
        Transaction(
            id="txn-synth-001",
            account_id=account.id,
            date=date(2026, 1, 15),
            description="Client Consulting Retainer",
            amount=Decimal("10000.00"),
            transaction_type=TransactionType.CREDIT,
            statement_period="2026-01",
            coa_code="4020",
            coa_name="Service Revenue",
        ),
        Transaction(
            id="txn-synth-002",
            account_id=account.id,
            date=date(2026, 1, 20),
            description="Cloud Hosting",
            amount=Decimal("-1000.00"),
            transaction_type=TransactionType.DEBIT,
            statement_period="2026-01",
            coa_code="5010",
            coa_name="Software & Subscriptions",
        ),
    ]
    TransactionRepo.bulk_insert(txns, db_file)

    # 1 AccountSnapshot ($9,000 ending balance)
    snapshot = AccountSnapshot(
        id="snap-synth-001",
        account_id=account.id,
        statement_period="2026-01",
        ending_balance=Decimal("9000.00"),
        gross_asset_value=Decimal("9000.00"),
        margin_balance=Decimal("0.00"),
    )
    SnapshotRepo.upsert(snapshot, db_file)

    return db_file


def test_customer_summary_four_way_parity(
    synthetic_db: Path, capsys: pytest.CaptureFixture
) -> None:
    """Assert < 1 dollar divergence and identical signals across all 4 delivery forms."""
    year = 2026

    # Form A: Python Library API
    form_a_summary = api.build_customer_summary(year)
    a_obi = float(form_a_summary.profit_or_loss["ordinary_business_income"])
    a_assets = float(form_a_summary.balance_sheet_health["total_assets"])
    a_liab = float(form_a_summary.balance_sheet_health["total_liabilities"])
    a_equity = float(form_a_summary.balance_sheet_health["total_equity"])
    a_pte = float(form_a_summary.pte_due_signal["annual_estimate"])
    a_flags = list(form_a_summary.confidence_flags)
    a_disclaimer = form_a_summary.disclaimer

    # Form B: CLI command with --no-prompt
    capsys.readouterr()  # flush previous buffers
    rc = cmd_summary([str(year), "--no-prompt"])
    assert rc == 0
    captured_b = capsys.readouterr().out
    form_b = json.loads(captured_b)

    b_obi = float(form_b["profit_or_loss"]["ordinary_business_income"])
    b_assets = float(form_b["balance_sheet_health"]["total_assets"])
    b_liab = float(form_b["balance_sheet_health"]["total_liabilities"])
    b_equity = float(form_b["balance_sheet_health"]["total_equity"])
    b_pte = float(form_b["pte_due_signal"]["annual_estimate"])
    b_flags = list(form_b["confidence_flags"])
    b_disclaimer = form_b.get("disclaimer", "")

    # Form C: MCP tool call
    raw_c = call_tool("customer_summary", {"fiscal_year": year}, allow_pii=True)
    form_c = json.loads(raw_c)

    c_obi = float(form_c["profit_or_loss"]["ordinary_business_income"])
    c_assets = float(form_c["balance_sheet_health"]["total_assets"])
    c_liab = float(form_c["balance_sheet_health"]["total_liabilities"])
    c_equity = float(form_c["balance_sheet_health"]["total_equity"])
    c_pte = float(form_c["pte_due_signal"]["annual_estimate"])
    c_flags = list(form_c["confidence_flags"])
    c_disclaimer = form_c.get("disclaimer", "")

    # Form D: Bridge JSON-RPC stdio dispatch
    capsys.readouterr()  # flush
    req_d = {
        "jsonrpc": "2.0",
        "id": "test-req-1",
        "method": "customer_summary",
        "params": {"fiscal_year": year, "_meta": {"allow_pii": True}},
    }
    _dispatch(req_d)
    captured_d = capsys.readouterr().out.strip()
    resp_d = json.loads(captured_d)
    assert "result" in resp_d, f"Bridge returned error: {resp_d.get('error')}"
    form_d = resp_d["result"]

    d_obi = float(form_d["profit_or_loss"]["ordinary_business_income"])
    d_assets = float(form_d["balance_sheet_health"]["total_assets"])
    d_liab = float(form_d["balance_sheet_health"]["total_liabilities"])
    d_equity = float(form_d["balance_sheet_health"]["total_equity"])
    d_pte = float(form_d["pte_due_signal"]["annual_estimate"])
    d_flags = list(form_d["confidence_flags"])
    d_disclaimer = form_d.get("disclaimer", "")

    # 1. Parity Assertions (Divergence < 1 dollar per R-51 / ARCH-32)
    for name, a_val, b_val, c_val, d_val in [
        ("ordinary_business_income", a_obi, b_obi, c_obi, d_obi),
        ("total_assets", a_assets, b_assets, c_assets, d_assets),
        ("total_liabilities", a_liab, b_liab, c_liab, d_liab),
        ("total_equity", a_equity, b_equity, c_equity, d_equity),
        ("pte_annual_estimate", a_pte, b_pte, c_pte, d_pte),
    ]:
        assert abs(a_val - b_val) < 1.00, f"{name}: Form A vs Form B diverged: {a_val} vs {b_val}"
        assert abs(a_val - c_val) < 1.00, f"{name}: Form A vs Form C diverged: {a_val} vs {c_val}"
        assert abs(a_val - d_val) < 1.00, f"{name}: Form A vs Form D diverged: {a_val} vs {d_val}"

    # 2. Confidence flags match identically
    assert a_flags == b_flags == c_flags == d_flags
    assert "CLOSE_READY" in a_flags

    # 3. Disclaimer matches canonical disclaimer across all 4 forms
    assert a_disclaimer == NOT_TAX_ADVICE_DISCLAIMER
    assert b_disclaimer == NOT_TAX_ADVICE_DISCLAIMER
    assert c_disclaimer == NOT_TAX_ADVICE_DISCLAIMER
    assert d_disclaimer == NOT_TAX_ADVICE_DISCLAIMER


def test_bridge_server_info_includes_customer_summary(
    capsys: pytest.CaptureFixture,
) -> None:
    """Verify that Bridge server_info exposes 'customer_summary' in methods list."""
    capsys.readouterr()
    _dispatch({"jsonrpc": "2.0", "id": 100, "method": "server_info"})
    out = capsys.readouterr().out.strip()
    resp = json.loads(out)
    assert resp["id"] == 100
    methods = resp["result"]["methods"]
    assert "customer_summary" in methods
    assert "generate_balance_sheet" in methods
    assert "ping" in methods
    assert "server_info" in methods


def test_bridge_customer_summary_error_handling(
    synthetic_db: Path, capsys: pytest.CaptureFixture
) -> None:
    """Verify bridge dispatch reports error code -32603 when arguments are invalid or missing."""
    capsys.readouterr()
    # Missing required argument 'fiscal_year'
    _dispatch({"jsonrpc": "2.0", "id": 200, "method": "customer_summary", "params": {}})
    out = capsys.readouterr().out.strip()
    resp = json.loads(out)
    assert resp["id"] == 200
    assert "error" in resp
    assert resp["error"]["code"] == -32603


def test_export_renderers_include_disclaimer(
    synthetic_db: Path, tmp_path: Path
) -> None:
    """Verify JSON and CSV exports from renderer.py include the disclaimer."""
    from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
    from ledger_agent.core.reports.renderer import (
        export_balance_sheet_csv,
        export_balance_sheet_json,
    )

    bs = BalanceSheetBuilder("ent-synth-001", "2026-01").build()
    export_dir = tmp_path / "exports"

    json_path = export_balance_sheet_json(bs, out_dir=export_dir)
    assert json_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload.get("disclaimer") == NOT_TAX_ADVICE_DISCLAIMER

    cov_path = export_dir / f"balance_sheet_{bs.period}.coverage.json"
    assert cov_path.exists()
    cov_payload = json.loads(cov_path.read_text(encoding="utf-8"))
    assert cov_payload.get("disclaimer") == NOT_TAX_ADVICE_DISCLAIMER

    csv_path = export_balance_sheet_csv(bs, out_dir=export_dir)
    assert csv_path.exists()
    csv_text = csv_path.read_text(encoding="utf-8")
    assert NOT_TAX_ADVICE_DISCLAIMER in csv_text


def test_financial_dataclasses_have_default_disclaimer() -> None:
    """Verify that all core financial dataclasses carry the default disclaimer."""
    f1065 = api.Form1065()
    assert f1065.disclaimer == NOT_TAX_ADVICE_DISCLAIMER

    k1 = api.ScheduleK1()
    assert k1.disclaimer == NOT_TAX_ADVICE_DISCLAIMER

    pte = api.PTEEstimate()
    assert pte.disclaimer == NOT_TAX_ADVICE_DISCLAIMER

    reconcile = api.ReconcileReport()
    assert reconcile.disclaimer == NOT_TAX_ADVICE_DISCLAIMER

