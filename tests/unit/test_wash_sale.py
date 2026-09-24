"""
tests/unit/test_wash_sale.py  –  Wash-sale disallowance unit tests (W16)
"""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ledger_agent.core.accounting.wash_sale import load_adjustments, total_disallowed


@pytest.fixture
def sample_csv(tmp_path: Path) -> Path:
    """Write a temporary wash-sale CSV with synthetic amounts."""
    p = tmp_path / "ws.csv"
    with p.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "disallowed_loss"])
        writer.writerow(["TICKER_SEC1", "1200.00"])
        writer.writerow(["TICKER_SEC2", "500.50"])
    return p


class TestLoadAdjustments:
    def test_loads_two_tickers(self, sample_csv):
        adj = load_adjustments(sample_csv)
        assert set(adj.keys()) == {"TICKER_SEC1", "TICKER_SEC2"}

    def test_decimal_precision(self, sample_csv):
        adj = load_adjustments(sample_csv)
        assert adj["TICKER_SEC1"] == Decimal("1200.00")
        assert adj["TICKER_SEC2"] == Decimal("500.50")

    def test_missing_file_returns_empty(self, tmp_path):
        adj = load_adjustments(tmp_path / "nonexistent.csv")
        assert adj == {}

    def test_malformed_row_skipped(self, tmp_path):
        p = tmp_path / "bad.csv"
        p.write_text("ticker,disallowed_loss\nTICKER_SEC1,bad_value\nTICKER_SEC2,100.00\n")
        adj = load_adjustments(p)
        assert "TICKER_SEC1" not in adj
        assert adj.get("TICKER_SEC2") == Decimal("100.00")


class TestTotalDisallowed:
    def test_sums_all_tickers(self, sample_csv):
        total = total_disallowed(sample_csv)
        assert total == Decimal("1700.50")

    def test_no_file_returns_zero(self, tmp_path):
        total = total_disallowed(tmp_path / "missing.csv")
        assert total == Decimal("0")


class TestReconcileComputedVsCsv:
    def test_reconciliation_deltas(self, monkeypatch, sample_csv):
        from ledger_agent.core.accounting.wash_sale import (
            WashSaleFinding,
            WashSaleReport,
            reconcile_computed_vs_csv,
        )

        mock_report = WashSaleReport(
            entity_id="e1",
            fiscal_year=2026,
            loss_sales_considered=1,
            findings=[
                WashSaleFinding(
                    symbol="TICKER_SEC1",
                    trade_date=date(2026, 1, 15),
                    loss_amount=Decimal("500.00"),
                    disallowed_amount=Decimal("500.00"),
                    evidence="Test finding",
                )
            ],
            total_disallowed=Decimal("500.00"),
        )

        import ledger_agent.core.accounting.wash_sale as ws_mod
        monkeypatch.setattr(ws_mod, "compute_from_ledger", lambda eid, yr: mock_report)

        res = reconcile_computed_vs_csv("e1", 2026, sample_csv)
        assert res["csv_present"] is True
        assert res["fiscal_year"] == 2026
        assert res["entity_id"] == "e1"
        assert res["computed_total"] == "500.00"
        assert res["csv_total"] == "1700.50"

        ticker1 = res["per_ticker"]["TICKER_SEC1"]
        assert ticker1["computed"] == "500.00"
        assert ticker1["authoritative_csv"] == "1200.00"
        assert ticker1["delta"] == "-700.00"

        ticker2 = res["per_ticker"]["TICKER_SEC2"]
        assert ticker2["computed"] == "0"
        assert ticker2["authoritative_csv"] == "500.50"
        assert ticker2["delta"] == "-500.50"

    def test_reconciliation_without_csv(self, monkeypatch, tmp_path):
        from ledger_agent.core.accounting.wash_sale import (
            WashSaleReport,
            reconcile_computed_vs_csv,
        )

        mock_report = WashSaleReport(
            entity_id="e1",
            fiscal_year=2026,
            loss_sales_considered=0,
            findings=[],
            total_disallowed=Decimal("0"),
        )

        import ledger_agent.core.accounting.wash_sale as ws_mod
        monkeypatch.setattr(ws_mod, "compute_from_ledger", lambda eid, yr: mock_report)

        res = reconcile_computed_vs_csv("e1", 2026, tmp_path / "missing.csv")
        assert res["csv_present"] is False
        assert res["csv_total"] == "0"
        assert res["computed_total"] == "0"


class TestComputeFromLedger:
    def test_no_loss_sales_returns_empty_findings(self, db):
        from ledger_agent.core.accounting.wash_sale import compute_from_ledger
        from ledger_agent.core.database import EntityRepo, init_db
        from ledger_agent.core.models import Entity
        init_db()
        entity = Entity(name="WS TEST LLC", entity_type="LLC", state="MO")
        EntityRepo.upsert(entity)

        report = compute_from_ledger(entity.id, 2026)
        assert report.loss_sales_considered == 0
        assert report.total_disallowed == Decimal("0")
        assert len(report.findings) == 0

    def test_wash_sale_detected_on_rebuy(self, db):
        from ledger_agent.core.accounting.wash_sale import compute_from_ledger
        from ledger_agent.core.database import (
            AccountRepo,
            EntityRepo,
            PositionRepo,
            TransactionRepo,
            init_db,
        )
        from ledger_agent.core.models import (
            Account,
            AccountType,
            Entity,
            Position,
            Transaction,
            TransactionType,
        )
        init_db()
        entity = Entity(name="WS REBUY LLC", entity_type="LLC", state="MO")
        EntityRepo.upsert(entity)
        acct = Account(
            entity_id=entity.id, name="Brokerage", institution="Broker Z",
            account_type=AccountType.BROKERAGE, account_number_masked="****3333",  # redaction: allow
        )
        AccountRepo.upsert(acct)

        # Loss sale in 2026-03 of SEC1
        t = Transaction(
            account_id=acct.id,
            date=date(2026, 3, 10),
            description="SELL SEC1",
            raw_description="SELL SEC1",
            amount=Decimal("-400.00"),
            transaction_type=TransactionType.SELL,
            statement_period="2026-03",
            tags=["SEC1"],
        )
        TransactionRepo.bulk_insert([t])

        # Position at end of 2026-03 was 10 shares
        p1 = Position(
            account_id=acct.id,
            symbol="SEC1",
            name="Ticker Sec1 Corp",
            quantity=Decimal("10"),
            price_per_unit=Decimal("50.00"),
            market_value=Decimal("500.00"),
            statement_period="2026-03",
        )
        # Position at end of 2026-04 increased to 25 shares (rebuy within window)
        p2 = Position(
            account_id=acct.id,
            symbol="SEC1",
            name="Ticker Sec1 Corp",
            quantity=Decimal("25"),
            price_per_unit=Decimal("50.00"),
            market_value=Decimal("1250.00"),
            statement_period="2026-04",
        )
        PositionRepo.upsert_period([p1])
        PositionRepo.upsert_period([p2])

        report = compute_from_ledger(entity.id, 2026)
        assert report.loss_sales_considered == 1
        assert report.total_disallowed == Decimal("400.00")
        assert len(report.findings) == 1
        finding = report.findings[0]
        assert finding.symbol == "SEC1"
        assert finding.disallowed_amount == Decimal("400.00")
        assert "increased" in finding.evidence

