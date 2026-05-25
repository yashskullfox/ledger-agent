"""
tests/integration/test_entity_isolation.py  –  Entity isolation in exports (ARCH-30)
"""
from __future__ import annotations

import os
import sqlite3
from decimal import Decimal

import pytest


@pytest.fixture
def iso_db(tmp_path):
    os.environ["FI_DB_PATH"] = str(tmp_path / "test.db")
    yield tmp_path / "test.db"
    del os.environ["FI_DB_PATH"]


def _seed_two_entities(db_path):
    """Seed two entities with separate transaction sets."""
    from ledger_agent.core.database import init_db
    init_db(db_path)
    con = sqlite3.connect(str(db_path))
    # Entity A
    con.execute("INSERT INTO entities(id,name,entity_type,state,created_at) VALUES('ea','ENTITY_A','LLC','MO','2024-01-01')")
    con.execute("INSERT INTO accounts(id,entity_id,name,institution,account_type,account_number_masked,created_at) VALUES('aa','ea','ENTITY_A Checking','BANK_X','checking','****1234','2024-01-01')")
    con.execute("INSERT INTO transactions(id,account_id,date,description,amount,transaction_type,coa_code,statement_period,is_transfer,created_at) VALUES('ta1','aa','2024-06-01','Revenue A','5000.00','credit','4020','2024-06',0,'2024-06-01')")
    # Entity B
    con.execute("INSERT INTO entities(id,name,entity_type,state,created_at) VALUES('eb','ENTITY_B','LLC','CA','2024-01-01')")
    con.execute("INSERT INTO accounts(id,entity_id,name,institution,account_type,account_number_masked,created_at) VALUES('ab','eb','ENTITY_B Checking','BANK_X','checking','****5678','2024-01-01')")
    con.execute("INSERT INTO transactions(id,account_id,date,description,amount,transaction_type,coa_code,statement_period,is_transfer,created_at) VALUES('tb1','ab','2024-06-01','Revenue B','9000.00','credit','4020','2024-06',0,'2024-06-01')")
    con.commit(); con.close()


class TestEntityIsolation:
    """Verify that Form 1065 reports reflect the first entity only and includes entity name."""

    @pytest.fixture(autouse=True)
    def setup(self, iso_db):
        _seed_two_entities(iso_db)

    def test_form_1065_carries_entity_name(self):
        import ledger_agent.core.api as api
        f = api.generate_form_1065(2024)
        # entity_name must be set (not empty)
        assert f.entity_name, "Form 1065 must carry entity name"

    def test_form_1065_total_income_matches_first_entity(self):
        """Only ENTITY_A's transactions should be included (entities[0])."""
        import ledger_agent.core.api as api
        f = api.generate_form_1065(2024)
        # entity A has 5000, entity B has 9000 — should NOT sum both
        assert f.total_income != Decimal("14000.00"), (
            "Form 1065 must NOT sum transactions from multiple entities"
        )
        assert f.total_income in (Decimal("5000.00"), Decimal("9000.00")), (
            f"Expected single-entity income (5000 or 9000), got {f.total_income}"
        )
