"""
tests/unit/test_form_1065_cogs.py  –  Form 1065 COGS separation unit tests (W15)
"""
from __future__ import annotations

import os
import pytest
from decimal import Decimal


@pytest.fixture
def temp_db(tmp_path):
    """Isolated SQLite database with a minimal 2024 transaction set."""
    os.environ["FI_DB_PATH"] = str(tmp_path / "test.db")
    yield tmp_path / "test.db"
    del os.environ["FI_DB_PATH"]


def _seed_minimal(db_path):
    """Seed a minimal COA + transactions for structural tests."""
    import sqlite3
    from ledger_agent.core.database import init_db
    init_db(db_path)

    con = sqlite3.connect(str(db_path))
    # Insert entity
    con.execute("INSERT INTO entities(id,name,entity_type,state,created_at) VALUES(?,?,?,?,?)",
                ("e1", "Entity", "LLC", "MO", "2024-01-01"))
    # Insert account
    con.execute("INSERT INTO accounts(id,entity_id,name,institution,account_type,account_number_masked,created_at) VALUES(?,?,?,?,?,?,?)",
                ("a1", "e1", "BANK_X Checking", "BANK_X", "checking", "****1234", "2024-01-01"))  # redaction: allow
    # Insert transactions for 2024
    rows = [
        # 4020 revenue
        ("t1", "a1", "2024-06-01", "Service revenue", "1000.00", "credit", "4020", "2024-06", "2024-06-01"),
        # 5061 COGS
        ("t2", "a1", "2024-06-02", "Office supplies", "-300.00", "debit", "5061", "2024-06", "2024-06-02"),
        # 5030 margin interest → Schedule K
        ("t3", "a1", "2024-06-03", "Margin interest", "-100.00", "debit", "5030", "2024-06", "2024-06-03"),
        # 5050 federal tax → equity draw, excluded
        ("t4", "a1", "2024-06-04", "IRS payment", "-50.00", "debit", "5050", "2024-06", "2024-06-04"),
        # 5010 normal deduction
        ("t5", "a1", "2024-06-05", "Software sub", "-200.00", "debit", "5010", "2024-06", "2024-06-05"),
    ]
    con.executemany(
        "INSERT INTO transactions(id,account_id,date,description,amount,transaction_type,coa_code,statement_period,is_transfer,created_at) VALUES(?,?,?,?,?,?,?,?,0,?)",
        rows,
    )
    con.commit()
    con.close()


class TestForm1065CogsStructure:
    """Verify COGS, Schedule K interest, and equity-draw separation (W15)."""

    @pytest.fixture(autouse=True)
    def setup(self, temp_db):
        _seed_minimal(temp_db)

    def test_cogs_separated_from_deductions(self):
        """5061 must appear in cost_of_goods_sold, NOT in total_deductions."""
        import ledger_agent.core.api as api
        f = api.generate_form_1065(2024)
        assert f.cost_of_goods_sold == Decimal("300.00"), (
            f"COGS should be 300.00, got {f.cost_of_goods_sold}"
        )
        # 5061 must NOT be in total_deductions
        assert f.total_deductions == Decimal("200.00"), (
            f"total_deductions should exclude COGS — expected 200.00, got {f.total_deductions}"
        )

    def test_schedule_k_interest_excluded(self):
        """5030 margin interest must appear in investment_interest_expense, NOT deductions."""
        import ledger_agent.core.api as api
        f = api.generate_form_1065(2024)
        assert f.investment_interest_expense == Decimal("100.00"), (
            f"investment_interest_expense should be 100.00, got {f.investment_interest_expense}"
        )
        assert f.total_deductions == Decimal("200.00"), (
            f"5030 must not inflate total_deductions — expected 200.00, got {f.total_deductions}"
        )

    def test_equity_draw_excluded(self):
        """5050 IRS/tax payments must be excluded from deductions (treated as equity draw)."""
        import ledger_agent.core.api as api
        f = api.generate_form_1065(2024)
        # Neither deductions nor cogs should contain 5050
        assert f.total_deductions == Decimal("200.00"), (
            f"5050 must not inflate deductions — expected 200.00, got {f.total_deductions}"
        )

    def test_gross_profit_computed(self):
        """gross_profit = total_income - cost_of_goods_sold."""
        import ledger_agent.core.api as api
        f = api.generate_form_1065(2024)
        assert f.gross_profit == f.total_income - f.cost_of_goods_sold, (
            f"gross_profit should equal income - COGS"
        )

    def test_ordinary_business_income_uses_gross_profit(self):
        """ordinary_business_income = gross_profit - total_deductions."""
        import ledger_agent.core.api as api
        f = api.generate_form_1065(2024)
        # income=1000, cogs=300, gross_profit=700, deductions=200, obi=500
        assert f.ordinary_business_income == Decimal("500.00"), (
            f"OBI should be 500.00, got {f.ordinary_business_income}"
        )
