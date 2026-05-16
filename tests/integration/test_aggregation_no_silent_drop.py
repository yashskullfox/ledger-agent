"""
tests/integration/test_aggregation_no_silent_drop.py  —  ARCH-26
=================================================================

Verifies R-63 / R-64: the balance-sheet aggregator never silently drops
an account snapshot.  Any snapshot present in ``account_snapshots`` for
the requested period MUST appear in the rendered balance sheet.  Any
account with NO snapshot MUST be recorded in the coverage manifest's
``skipped_snapshots`` list — never silently omitted.

Test strategy
-------------
All tests use an isolated in-memory (or temp-file) SQLite database so
they do not touch the production DB.  The ``db`` pytest fixture creates
a fresh database, seeds the minimum required entity/account rows, and
yields a ``db_path`` for injection via ``FI_DB_PATH``.

Acceptance
----------
    pytest tests/integration/test_aggregation_no_silent_drop.py -q
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    """Isolated SQLite DB for each test; injected via FI_DB_PATH."""
    db_file = tmp_path / "test_agg.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    # init schema
    from ledger_agent.core.database import init_db
    init_db(db_file)
    return db_file


def _make_entity(db_path):
    from ledger_agent.core.database import EntityRepo
    from ledger_agent.core.models import Entity
    e = Entity(name="ENTITY_A", entity_type="LLC", state="FL",
               id=str(uuid.uuid4()))
    EntityRepo.upsert(e, db_path)
    return e


def _make_account(db_path, entity_id, institution, acct_type_str, suffix="0001"):
    from ledger_agent.core.database import AccountRepo
    from ledger_agent.core.models import Account, AccountType
    a = Account(
        entity_id=entity_id,
        name=f"{institution} Account",
        institution=institution,
        account_type=AccountType(acct_type_str),
        account_number_masked=suffix,
        id=str(uuid.uuid4()),
    )
    AccountRepo.upsert(a, db_path)
    return a


def _make_snapshot(db_path, account_id, period, ending_balance,
                   gross=None, margin=None):
    from ledger_agent.core.database import SnapshotRepo
    from ledger_agent.core.models import AccountSnapshot
    s = AccountSnapshot(
        account_id=account_id,
        statement_period=period,
        ending_balance=Decimal(ending_balance),
        gross_asset_value=Decimal(gross) if gross else None,
        margin_balance=Decimal(margin) if margin else None,
        id=str(uuid.uuid4()),
    )
    SnapshotRepo.upsert(s, db_path)
    return s


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSnapshotConsumed:
    """Snapshot present → account appears in balance sheet and coverage."""

    def test_checking_account_appears_in_total_assets(self, db):
        entity = _make_entity(db)
        acct = _make_account(db, entity.id, "Bank X", "checking")
        _make_snapshot(db, acct.id, "2025-01", "4031.20")

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()

        assert bs.total_assets == Decimal("4031.20"), (
            f"Expected total_assets=4031.20, got {bs.total_assets}"
        )

    def test_brokerage_account_gross_in_total_assets(self, db):
        entity = _make_entity(db)
        acct = _make_account(db, entity.id, "Broker Y", "brokerage")
        _make_snapshot(db, acct.id, "2025-01", "35438.80",
                       gross="59500.00", margin="-24061.20")

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()

        assert bs.total_assets == Decimal("59500.00"), (
            f"Expected gross 59500.00 in total_assets, got {bs.total_assets}"
        )

    def test_consumed_snapshot_in_coverage_manifest(self, db):
        entity = _make_entity(db)
        acct = _make_account(db, entity.id, "Bank X", "checking")
        _make_snapshot(db, acct.id, "2025-01", "4031.20")

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()

        consumed_ids = {e["account_id"] for e in bs.coverage["consumed_snapshots"]}
        assert acct.id in consumed_ids, (
            "Consumed account must appear in coverage['consumed_snapshots']"
        )

    def test_two_accounts_both_in_assets(self, db):
        entity = _make_entity(db)
        bank_x = _make_account(db, entity.id, "Bank X", "checking", "0001")
        broker_y = _make_account(db, entity.id, "Broker Y", "brokerage", "0002")
        _make_snapshot(db, bank_x.id, "2025-01", "4031.20")
        _make_snapshot(db, broker_y.id, "2025-01", "35438.80",
                       gross="59500.00", margin="-24061.20")

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()

        # total = cash 4031.20 + gross securities 59500.00
        assert bs.total_assets == Decimal("63531.20"), (
            f"Expected 63531.20, got {bs.total_assets}"
        )
        assert len(bs.coverage["consumed_snapshots"]) == 2


class TestSnapshotGap:
    """Account exists with no snapshot → AggregationGap, never silent drop."""

    def test_missing_snapshot_not_in_assets(self, db):
        entity = _make_entity(db)
        _make_account(db, entity.id, "Bank X", "checking")
        # No snapshot inserted for this account/period

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()

        assert bs.total_assets == Decimal("0"), (
            f"Account with no snapshot must contribute 0 to total_assets, "
            f"got {bs.total_assets}"
        )

    def test_missing_snapshot_recorded_in_skipped(self, db):
        entity = _make_entity(db)
        acct = _make_account(db, entity.id, "Bank X", "checking")

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()

        skipped_ids = {e["account_id"] for e in bs.coverage["skipped_snapshots"]}
        assert acct.id in skipped_ids, (
            "Account with no snapshot must appear in coverage['skipped_snapshots']"
        )

    def test_skipped_entry_has_reason(self, db):
        entity = _make_entity(db)
        _make_account(db, entity.id, "Bank X", "checking")

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()

        for entry in bs.coverage["skipped_snapshots"]:
            assert "reason" in entry and entry["reason"], (
                "Every skipped snapshot entry must carry a non-empty 'reason'"
            )

    def test_missing_brokerage_recorded_in_skipped(self, db):
        """V1 regression: Broker Y missing from 2024-12 must be skipped, not silently absent."""
        entity = _make_entity(db)
        broker_y = _make_account(db, entity.id, "Broker Y Investments", "brokerage")
        # Deliberately omit snapshot for 2024-12

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2024-12").build()

        skipped_ids = {e["account_id"] for e in bs.coverage["skipped_snapshots"]}
        assert broker_y.id in skipped_ids, (
            "Broker Y with missing 2024-12 snapshot must be in skipped_snapshots (V1 regression)"
        )


class TestCoverageManifest:
    """Coverage manifest shape and sibling-file export."""

    def test_coverage_keys_present(self, db):
        entity = _make_entity(db)
        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        bs = BalanceSheetBuilder(entity.id, "2025-01").build()

        assert "consumed_snapshots" in bs.coverage
        assert "skipped_snapshots" in bs.coverage

    def test_coverage_json_written_alongside_export(self, db, tmp_path):
        entity = _make_entity(db)
        acct = _make_account(db, entity.id, "Bank X", "checking")
        _make_snapshot(db, acct.id, "2025-01", "4031.20")

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        from ledger_agent.core.reports.renderer import export_balance_sheet_json

        bs = BalanceSheetBuilder(entity.id, "2025-01").build()
        export_balance_sheet_json(bs, tmp_path)

        coverage_file = tmp_path / "balance_sheet_2025-01.coverage.json"
        assert coverage_file.exists(), (
            f"Coverage manifest must be written at {coverage_file}"
        )

    def test_coverage_json_is_valid_and_complete(self, db, tmp_path):
        entity = _make_entity(db)
        acct = _make_account(db, entity.id, "Bank X", "checking")
        _make_snapshot(db, acct.id, "2025-01", "4031.20")

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        from ledger_agent.core.reports.renderer import export_balance_sheet_json

        bs = BalanceSheetBuilder(entity.id, "2025-01").build()
        export_balance_sheet_json(bs, tmp_path)

        data = json.loads(
            (tmp_path / "balance_sheet_2025-01.coverage.json").read_text()
        )
        assert "consumed_snapshots" in data
        assert "skipped_snapshots" in data
        assert "consumed_count" in data
        assert "skipped_count" in data
        assert data["period"] == "2025-01"
        assert data["consumed_count"] == len(data["consumed_snapshots"])
        assert data["skipped_count"] == len(data["skipped_snapshots"])

    def test_coverage_entries_have_account_id_and_institution(self, db, tmp_path):
        entity = _make_entity(db)
        acct = _make_account(db, entity.id, "Bank X", "checking")
        _make_snapshot(db, acct.id, "2025-01", "4031.20")

        from ledger_agent.core.accounting.balance_sheet import BalanceSheetBuilder
        from ledger_agent.core.reports.renderer import export_balance_sheet_json

        bs = BalanceSheetBuilder(entity.id, "2025-01").build()
        export_balance_sheet_json(bs, tmp_path)

        data = json.loads(
            (tmp_path / "balance_sheet_2025-01.coverage.json").read_text()
        )
        for entry in data["consumed_snapshots"] + data["skipped_snapshots"]:
            assert "account_id" in entry
            assert "institution" in entry
            assert "period" in entry
