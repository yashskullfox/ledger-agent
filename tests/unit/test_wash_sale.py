"""
tests/unit/test_wash_sale.py  –  Wash-sale disallowance unit tests (W16)
"""
from __future__ import annotations

import csv
import pytest
from decimal import Decimal
from pathlib import Path

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
