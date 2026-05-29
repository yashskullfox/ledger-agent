"""
tests/integration/test_cli_summary.py  –  Integration tests for ``ledger summary`` (W28)
==========================================================================================

Verifies that ``build_customer_summary()`` (the core backing the CLI command)
returns the W27 contract shape and that the CLI layer formats it correctly.

Test isolation: every test uses a throw-away SQLite DB via the ``FI_DB_PATH``
environment variable, following the same pattern as the unit tests.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def iso_db(tmp_path):
    """Isolated SQLite database; sets FI_DB_PATH for the duration of the test."""
    db = tmp_path / "test.db"
    os.environ["FI_DB_PATH"] = str(db)
    yield db
    del os.environ["FI_DB_PATH"]


def _seed_2024(db_path: Path) -> None:
    """Seed a minimal entity + transactions covering fiscal year 2024."""
    from ledger_agent.core.database import init_db

    init_db(db_path)
    con = sqlite3.connect(str(db_path))

    con.execute(
        "INSERT INTO entities(id,name,entity_type,state,created_at) "
        "VALUES('e1','ENTITY_A','LLC','MO','2024-01-01')"
    )
    con.execute(
        "INSERT INTO accounts(id,entity_id,name,institution,account_type,"
        "account_number_masked,created_at) "
        "VALUES('a1','e1','Checking','BANK_X','checking','****1234','2024-01-01')"  # redaction: allow
    )

    rows = [
        ("t1", "a1", "2024-06-01", "Service revenue", "5000.00", "credit", "4020", "2024-06"),
        ("t2", "a1", "2024-07-01", "Service revenue", "3000.00", "credit", "4020", "2024-07"),
        ("t3", "a1", "2024-06-15", "Office supplies", "-400.00", "debit",  "5010", "2024-06"),
        ("t4", "a1", "2024-07-20", "Software sub",   "-200.00", "debit",  "5010", "2024-07"),
    ]
    con.executemany(
        "INSERT INTO transactions(id,account_id,date,description,amount,"
        "transaction_type,coa_code,statement_period,is_transfer,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,0,?)",
        [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[2]) for r in rows],
    )
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Tests: W27 contract shape
# ---------------------------------------------------------------------------


class TestCustomerSummaryContract:
    """Verify that build_customer_summary() returns every required W27 field."""

    @pytest.fixture(autouse=True)
    def setup(self, iso_db):
        _seed_2024(iso_db)

    def test_required_top_level_fields(self):
        import ledger_agent.core.api as api

        s = api.build_customer_summary(2024)
        assert s.fiscal_year == 2024
        assert isinstance(s.period_covered, str) and s.period_covered
        assert isinstance(s.profit_or_loss, dict)
        assert isinstance(s.growth_signal, dict)
        assert isinstance(s.balance_sheet_health, dict)
        assert isinstance(s.pte_due_signal, dict)
        assert isinstance(s.tax_due_signal, dict)
        assert isinstance(s.confidence_flags, list)
        assert isinstance(s.next_actions, list)

    def test_profit_or_loss_fields(self):
        import ledger_agent.core.api as api

        s = api.build_customer_summary(2024)
        pl = s.profit_or_loss
        assert "status" in pl
        assert pl["status"] in ("profit", "loss", "break_even")
        assert "signal" in pl
        assert pl["signal"] in ("positive", "negative", "neutral")
        assert "ordinary_business_income" in pl

    def test_balance_sheet_health_fields(self):
        import ledger_agent.core.api as api

        s = api.build_customer_summary(2024)
        bs = s.balance_sheet_health
        for key in ("is_balanced", "status", "total_assets", "total_liabilities", "total_equity"):
            assert key in bs, f"balance_sheet_health missing key '{key}'"

    def test_pte_due_signal_fields(self):
        import ledger_agent.core.api as api

        s = api.build_customer_summary(2024)
        pte = s.pte_due_signal
        assert "status" in pte
        assert "annual_estimate" in pte
        assert "next_due" in pte

    def test_growth_signal_fields(self):
        import ledger_agent.core.api as api

        s = api.build_customer_summary(2024)
        gs = s.growth_signal
        assert "status" in gs
        assert "note" in gs

    def test_next_actions_is_non_empty_list(self):
        import ledger_agent.core.api as api

        s = api.build_customer_summary(2024)
        assert len(s.next_actions) >= 1

    def test_profit_signal_for_profitable_year(self):
        """With revenue > expenses the status must be 'profit'."""
        import ledger_agent.core.api as api

        s = api.build_customer_summary(2024)
        # Revenue = 8000, expenses = 600 → net profit
        assert s.profit_or_loss["status"] == "profit"
        assert s.profit_or_loss["signal"] == "positive"

    def test_serialisable_as_dict(self):
        """dataclasses.asdict() must produce a JSON-serialisable dict."""
        import ledger_agent.core.api as api

        s = api.build_customer_summary(2024)
        d = dataclasses.asdict(s)
        # Should not raise
        json.dumps(d, default=str)

    def test_missing_year_raises_value_error(self, iso_db):
        import ledger_agent.core.api as api

        with pytest.raises(ValueError, match="No statement data"):
            api.build_customer_summary(1999)


# ---------------------------------------------------------------------------
# Tests: confidence flags
# ---------------------------------------------------------------------------


class TestConfidenceFlags:
    """Confidence flags reflect open correctness gates."""

    @pytest.fixture(autouse=True)
    def setup(self, iso_db):
        _seed_2024(iso_db)

    def test_confidence_flags_are_strings(self):
        import ledger_agent.core.api as api

        s = api.build_customer_summary(2024)
        for flag in s.confidence_flags:
            assert isinstance(flag, str), f"flag must be str, got {type(flag)}"

    def test_close_ready_or_not_flag_present(self):
        """At least one CLOSE_READY or NOT_CLOSE_READY_* flag must be emitted."""
        import ledger_agent.core.api as api

        s = api.build_customer_summary(2024)
        has_readiness = any(
            f.startswith("CLOSE_READY") or f.startswith("NOT_CLOSE_READY")
            for f in s.confidence_flags
        )
        assert has_readiness, (
            f"Expected a readiness flag in {s.confidence_flags}"
        )


# ---------------------------------------------------------------------------
# Tests: CLI layer --no-prompt JSON output
# ---------------------------------------------------------------------------


class TestCliSummaryCommand:
    """Test the CLI cmd_summary() dispatcher produces valid JSON with --no-prompt."""

    @pytest.fixture(autouse=True)
    def setup(self, iso_db):
        _seed_2024(iso_db)

    def test_no_prompt_outputs_valid_json(self, capsys):
        from ledger_agent.cli.main import cmd_summary

        rc = cmd_summary(["2024", "--no-prompt"])
        assert rc == 0
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["fiscal_year"] == 2024
        assert "profit_or_loss" in parsed
        assert "confidence_flags" in parsed
        assert "next_actions" in parsed

    def test_no_prompt_json_matches_contract_shape(self, capsys):
        from ledger_agent.cli.main import cmd_summary

        cmd_summary(["2024", "--no-prompt"])
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        # All W27 top-level keys must be present
        for key in (
            "fiscal_year", "period_covered", "profit_or_loss",
            "growth_signal", "balance_sheet_health", "pte_due_signal",
            "tax_due_signal", "confidence_flags", "next_actions",
        ):
            assert key in parsed, f"Missing W27 key '{key}' in --no-prompt output"

    def test_missing_year_returns_nonzero(self, capsys):
        from ledger_agent.cli.main import cmd_summary

        rc = cmd_summary(["1999"])
        assert rc != 0
