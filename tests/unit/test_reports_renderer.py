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
