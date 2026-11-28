"""
tests/unit/test_withholding.py  –  Partner withholding reclassification tests (ARCH-29)
"""
from __future__ import annotations

import os
import sqlite3

import pytest


@pytest.fixture
def iso_db(tmp_path):
    prev = os.environ.get("FI_DB_PATH")
    os.environ["FI_DB_PATH"] = str(tmp_path / "test.db")
    yield tmp_path / "test.db"
    if prev is not None:
        os.environ["FI_DB_PATH"] = prev
    else:
        os.environ.pop("FI_DB_PATH", None)


def _seed(db_path):
    from ledger_agent.core.database import init_db
    init_db(db_path)
    con = sqlite3.connect(str(db_path))
    con.execute("INSERT INTO entities(id,name,entity_type,state,created_at) VALUES('e1','Entity','LLC','MO','2024-01-01')")
    con.execute("INSERT INTO accounts(id,entity_id,name,institution,account_type,account_number_masked,created_at) VALUES('a1','e1','Checking','BANK_X','checking','****1234','2024-01-01')")
    # Misclassified: IRS payment coded 5050
    con.execute("INSERT INTO transactions(id,account_id,date,description,amount,transaction_type,coa_code,statement_period,is_transfer,created_at) VALUES('t1','a1','2024-03-01','USATAXPYMT','-238.68','debit','5050','2024-03',0,'2024-03-01')")
    # Correctly classified: software cost coded 5010
    con.execute("INSERT INTO transactions(id,account_id,date,description,amount,transaction_type,coa_code,statement_period,is_transfer,created_at) VALUES('t2','a1','2024-03-02','GitHub subscription','-19.00','debit','5010','2024-03',0,'2024-03-02')")
    con.commit()
    con.close()


class TestFindMisclassified:
    @pytest.fixture(autouse=True)
    def setup(self, iso_db):
        _seed(iso_db)

    def test_finds_irs_payment(self):
        from ledger_agent.core.accounting.withholding import find_misclassified_withholding
        ids = find_misclassified_withholding("e1", 2024)
        assert "t1" in ids

    def test_ignores_normal_expense(self):
        from ledger_agent.core.accounting.withholding import find_misclassified_withholding
        ids = find_misclassified_withholding("e1", 2024)
        assert "t2" not in ids


class TestKeywordWordBoundaries:
    """C-01: withholding keywords match whole words, not substrings."""

    @pytest.mark.parametrize("description", [
        "FIRST NATIONAL BANK",      # irs ⊂ first
        "FOREIGN AFFAIRS COUNCIL",  # irs ⊂ affairs
        "OFFICE CHAIRS",            # irs ⊂ chairs
        "THEIRS BOUTIQUE",          # irs ⊂ theirs
        "REDISTRIBUTION FEE",       # distribution ⊂ redistribution
        "",
        None,
    ])
    def test_substring_does_not_match(self, description):
        from ledger_agent.core.accounting.withholding import matches_withholding_keyword
        assert matches_withholding_keyword(description) is False

    @pytest.mark.parametrize("description", [
        "IRS USATAXPYMT",
        "ACH CORP DEBIT USATAXPYMT",
        "irs treas 310",
        "IRS2024 PAYMENT",          # reference digits glued on
        "Q1 ESTIMATED TAX",
        "ESTIMATED  TAXES",         # extra whitespace, plural
        "OWNER DRAWS",
        "member draw",
        "PARTNER DISTRIBUTIONS",
    ])
    def test_intended_keywords_match(self, description):
        from ledger_agent.core.accounting.withholding import matches_withholding_keyword
        assert matches_withholding_keyword(description) is True

    def test_find_skips_substring_lookalikes(self, iso_db):
        _seed(iso_db)
        con = sqlite3.connect(str(iso_db))
        # Coded as tax expense, but the description only CONTAINS "irs".
        con.execute("INSERT INTO transactions(id,account_id,date,description,amount,transaction_type,coa_code,statement_period,is_transfer,created_at) VALUES('t3','a1','2024-03-03','FIRST STATE SERVICES','-50.00','debit','5055','2024-03',0,'2024-03-03')")
        con.execute("INSERT INTO transactions(id,account_id,date,description,amount,transaction_type,coa_code,statement_period,is_transfer,created_at) VALUES('t4','a1','2024-03-04','Q2 ESTIMATED TAXES','-60.00','debit','5055','2024-03',0,'2024-03-04')")
        con.commit()
        con.close()
        from ledger_agent.core.accounting.withholding import find_misclassified_withholding
        ids = find_misclassified_withholding("e1", 2024)
        assert "t3" not in ids
        assert set(ids) == {"t1", "t4"}


class TestReclassify:
    @pytest.fixture(autouse=True)
    def setup(self, iso_db):
        _seed(iso_db)
        self._db_path = iso_db

    def test_dry_run_returns_count(self):
        from ledger_agent.core.accounting.withholding import reclassify_partner_withholding
        count = reclassify_partner_withholding("e1", 2024, dry_run=True)
        assert count == 1
        # Verify nothing changed
        con = sqlite3.connect(str(self._db_path))
        row = con.execute("SELECT coa_code FROM transactions WHERE id='t1'").fetchone()
        con.close()
        assert row[0] == "5050", "dry_run must not change coa_code"

    def test_reclassifies_to_3040(self):
        from ledger_agent.core.accounting.withholding import reclassify_partner_withholding
        count = reclassify_partner_withholding("e1", 2024)
        assert count == 1
        con = sqlite3.connect(str(self._db_path))
        row = con.execute("SELECT coa_code FROM transactions WHERE id='t1'").fetchone()
        con.close()
        assert row[0] == "3040", "IRS payment must be reclassified to 3040"

    def test_no_double_reclassification(self):
        from ledger_agent.core.accounting.withholding import reclassify_partner_withholding
        reclassify_partner_withholding("e1", 2024)
        count2 = reclassify_partner_withholding("e1", 2024)
        assert count2 == 0, "second run should find nothing to reclassify"
