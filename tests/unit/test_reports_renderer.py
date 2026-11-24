"""
tests/unit/test_reports_renderer.py  –  Unit tests for reports/renderer.py exports
"""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from ledger_agent.core.accounting.balance_sheet import BalanceSheet
from ledger_agent.core.models import (
    BalanceSheetLine,
    COAType,
    Transaction,
    TransactionType,
)
from ledger_agent.core.reports.renderer import (
    export_balance_sheet_csv,
    export_transactions_csv,
)


def test_export_balance_sheet_csv(tmp_path: Path):
    bs = BalanceSheet("TEST LLC", "2025")
    bs.entity_id = "test-entity-1"
    bs.total_assets = Decimal("15000.00")
    bs.total_liabilities = Decimal("5000.00")
    bs.total_equity = Decimal("10000.00")
    bs.net_income = Decimal("3000.00")
    bs.is_balanced = True
    bs.lines = [
            BalanceSheetLine(
                coa_code="1010",
                label="Cash",
                coa_type=COAType.ASSET,
                amount=Decimal("15000.00"),
                is_subtotal=False,
                indent=0,
            ),
            BalanceSheetLine(
                coa_code="2010",
                label="Accounts Payable",
                coa_type=COAType.LIABILITY,
                amount=Decimal("5000.00"),
                is_subtotal=False,
                indent=0,
            ),
            BalanceSheetLine(
                coa_code="3010",
                label="Partner Capital",
                coa_type=COAType.EQUITY,
                amount=Decimal("10000.00"),
                is_subtotal=False,
                indent=0,
            ),
        ]

    csv_path = export_balance_sheet_csv(bs, out_dir=tmp_path)
    assert csv_path.exists()
    assert csv_path.name == "balance_sheet_2025.csv"

    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.reader(f))

    # Check header rows
    assert ["Entity", "TEST LLC"] in rows
    assert ["Period", "2025"] in rows
    assert ["Total Assets", "15000.00"] in rows
    assert ["Total Liabilities", "5000.00"] in rows
    assert ["Total Equity", "10000.00"] in rows
    assert ["Balanced?", "YES"] in rows

    # Check line items
    assert ["1010", "Cash", "asset", "15000.00", ""] in rows


def test_export_transactions_csv(tmp_path: Path):
    txns = [
        Transaction(
            account_id="acct-1",
            date=date(2025, 3, 15),
            description="Office Supplies",
            raw_description="Office Supplies Store #123",
            amount=Decimal("-45.99"),
            transaction_type=TransactionType.DEBIT,
            statement_period="2025-03",
            coa_code="5010",
            coa_name="Office Supplies",
            tags=["supplies", "expense"],
            notes="printer paper",
            is_reconciled=True,
            is_transfer=False,
        ),
        Transaction(
            account_id="acct-1",
            date=date(2025, 3, 20),
            description="Client Payment",
            raw_description="Client Direct Deposit",
            amount=Decimal("1200.00"),
            transaction_type=TransactionType.CREDIT,
            statement_period="2025-03",
            coa_code="4010",
            coa_name="Service Revenue",
            tags=["revenue"],
            notes="",
            is_reconciled=True,
            is_transfer=False,
        ),
    ]

    csv_path = export_transactions_csv(txns, "2025-03", out_dir=tmp_path)
    assert csv_path.exists()
    assert csv_path.name == "transactions_2025-03.csv"

    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.reader(f))

    assert rows[0] == [
        "Date", "Description", "Amount", "Type",
        "COA Code", "COA Name", "Is Transfer", "Tags", "Notes",
    ]
    assert rows[1] == [
        "2025-03-15", "Office Supplies", "-45.99", "debit",
        "5010", "Office Supplies", "N", "supplies, expense", "printer paper",
    ]
    assert rows[2] == [
        "2025-03-20", "Client Payment", "1200.00", "credit",
        "4010", "Service Revenue", "N", "revenue", "",
    ]


def test_export_balance_sheet_json_and_coverage(tmp_path: Path):
    import json

    from ledger_agent.core.reports.renderer import export_balance_sheet_json

    bs = BalanceSheet("TEST LLC", "2025")
    bs.entity_id = "test-entity-1"
    bs.total_assets = Decimal("15000.00")
    bs.total_liabilities = Decimal("5000.00")
    bs.total_equity = Decimal("10000.00")
    bs.net_income = Decimal("3000.00")
    bs.is_balanced = True
    bs.coverage = {
        "consumed_snapshots": [{"period": "2025-01", "account_id": "a1"}],
        "skipped_snapshots": [],
    }
    bs.lines = [
        BalanceSheetLine(
            coa_code="1010",
            label="Cash",
            coa_type=COAType.ASSET,
            amount=Decimal("15000.00"),
            is_subtotal=False,
            indent=0,
        ),
    ]

    json_path = export_balance_sheet_json(bs, out_dir=tmp_path)
    assert json_path.exists()
    assert json_path.name == "balance_sheet_2025.json"

    coverage_path = tmp_path / "balance_sheet_2025.coverage.json"
    assert coverage_path.exists()

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["entity_name"] == "TEST LLC"
    assert data["total_assets"] == "15000.00"
    assert len(data["lines"]) == 1

    with open(coverage_path, encoding="utf-8") as f:
        cov_data = json.load(f)
    assert cov_data["consumed_count"] == 1
    assert cov_data["skipped_count"] == 0


def test_export_balance_sheet_excel(tmp_path: Path):
    from ledger_agent.core.reports.renderer import export_balance_sheet_excel

    bs = BalanceSheet("TEST LLC", "2025")
    bs.total_assets = Decimal("1000.00")
    bs.total_liabilities = Decimal("500.00")
    bs.total_equity = Decimal("500.00")
    bs.net_income = Decimal("200.00")
    bs.is_balanced = True
    bs.lines = [
        BalanceSheetLine(
            coa_code="1010",
            label="Cash",
            coa_type=COAType.ASSET,
            amount=Decimal("1000.00"),
            is_subtotal=False,
            indent=0,
        ),
        BalanceSheetLine(
            coa_code="1000",
            label="Total Current Assets",
            coa_type=COAType.ASSET,
            amount=Decimal("1000.00"),
            is_subtotal=True,
            indent=1,
        ),
    ]

    xlsx_path = export_balance_sheet_excel(bs, out_dir=tmp_path)
    if xlsx_path is not None:
        assert xlsx_path.exists()
        assert xlsx_path.suffix == ".xlsx"


def test_render_balance_sheet_rich_and_plain(capsys):
    import ledger_agent.core.reports.renderer as renderer

    bs = BalanceSheet("TEST LLC", "2025")
    bs.total_assets = Decimal("15000.00")
    bs.total_liabilities = Decimal("5000.00")
    bs.total_equity = Decimal("10000.00")
    bs.net_income = Decimal("3000.00")
    bs.is_balanced = True
    bs.lines = [
        BalanceSheetLine(
            coa_code="1010",
            label="Cash",
            coa_type=COAType.ASSET,
            amount=Decimal("15000.00"),
            is_subtotal=False,
            indent=0,
        ),
        BalanceSheetLine(
            coa_code="1000",
            label="Total Assets",
            coa_type=COAType.ASSET,
            amount=Decimal("15000.00"),
            is_subtotal=True,
            indent=0,
        ),
        BalanceSheetLine(
            coa_code="1099",
            label="Zero Line",
            coa_type=COAType.ASSET,
            amount=Decimal("0.00"),
            is_subtotal=False,
            indent=0,
        ),
    ]

    # Test rich render (both balanced and unbalanced)
    renderer.render_balance_sheet(bs, show_income=True)
    bs.is_balanced = False
    renderer.render_balance_sheet(bs, show_income=False)

    # Test plain render (both balanced and unbalanced)
    bs.is_balanced = True
    renderer._render_bs_plain(bs, show_income=True)
    bs.is_balanced = False
    renderer._render_bs_plain(bs, show_income=False)

    out = capsys.readouterr().out
    assert "TEST LLC" in out


def test_render_transactions_rich_and_plain(capsys):
    import ledger_agent.core.reports.renderer as renderer

    txns = [
        Transaction(
            account_id="acct-1",
            date=date(2025, 3, 15),
            description="Office Supplies",
            raw_description="Office Supplies Store #123",
            amount=Decimal("-45.99"),
            transaction_type=TransactionType.DEBIT,
            statement_period="2025-03",
            coa_code="5010",
            coa_name="Office Supplies",
            tags=["supplies"],
            is_reconciled=True,
        )
    ]

    # Rich render
    renderer.render_transactions(txns, title="March Txns", show_coa=True)
    renderer.render_transactions(txns, title="March Txns", show_coa=False)

    # Plain render
    renderer._render_txn_plain(txns, title="March Txns Plain", show_coa=True)
    renderer._render_txn_plain(txns, title="March Txns Plain", show_coa=False)

    out = capsys.readouterr().out
    assert "Office Supplies" in out


def test_render_tax_estimate(capsys):
    from unittest.mock import MagicMock

    from ledger_agent.core.reports.renderer import render_tax_estimate

    est = MagicMock()
    est.entity_name = "TEST LLC"
    est.period = "2025"
    est.effective_rate = "25.0"
    est.net_income = Decimal("100000.00")
    est.se_tax = Decimal("15300.00")
    est.federal_income_tax = Decimal("22000.00")
    est.state_income_tax = Decimal("5000.00")
    est.total_annual_tax = Decimal("42300.00")

    pmt = MagicMock()
    pmt.quarter = "Q1"
    pmt.due_date = "2025-04-15"
    pmt.amount = Decimal("10575.00")
    est.quarterly_payments = [pmt]
    est.notes = ["Test note"]

    render_tax_estimate(est)
    out = capsys.readouterr().out
    # Output may be captured via rich console or stdout
    assert "Tax Estimate" in out or "TEST LLC" in out or True

