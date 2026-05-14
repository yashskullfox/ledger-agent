"""
tests/test_cleanup.py  –  Unit tests for ledger_agent/core/cleanup.py

Covers:
  BUG-C1  — cycle_cleanup emits cleanup.skipped_busy when the write lock
             is held by an active writer rather than racing.
  SMELL-C4 — _purge_process_scratch skips entries not owned by the current
              user and emits cleanup.skipped_foreign_uid.
"""
from __future__ import annotations

import os
import sys
import threading
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import ledger_agent.core.cleanup as cleanup_mod
from ledger_agent.core.cleanup import (
    cycle_cleanup,
    hold_write_lock,
    _write_lock,
    _PROCESS_SCRATCH_PREFIX,
    _purge_process_scratch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_write_lock():
    """Release _write_lock if it was accidentally left held between tests."""
    # RLock tracks owner thread + count; release until it's fully free
    try:
        while True:
            _write_lock.release()
    except RuntimeError:
        pass  # "release unlocked lock"


@pytest.fixture(autouse=True)
def clean_write_lock():
    """Ensure the write lock is fully released before and after each test."""
    _reset_write_lock()
    yield
    _reset_write_lock()


# ---------------------------------------------------------------------------
# BUG-C1: cycle_cleanup skips when write lock is held
# ---------------------------------------------------------------------------

class TestBugC1WriteRace:
    """cycle_cleanup must emit skipped_busy instead of racing an active writer."""

    def test_cleanup_skips_when_lock_held(self):
        """
        Acquire _write_lock from a worker thread and call cycle_cleanup.
        cleanup must return without deleting files and must emit the
        cleanup.skipped_busy audit event.
        """
        audit_events = []

        def fake_audit(event, **kwargs):
            audit_events.append((event, kwargs))

        # Hold the lock from another thread, simulating an active writer
        lock_acquired = threading.Event()
        lock_release = threading.Event()

        def writer_thread():
            with hold_write_lock():
                lock_acquired.set()
                lock_release.wait(timeout=5)

        t = threading.Thread(target=writer_thread, daemon=True)
        t.start()
        lock_acquired.wait(timeout=2)

        try:
            # Now call cycle_cleanup — it should not be able to acquire the lock
            fake_audit_mod = MagicMock()
            fake_audit_mod.audit = fake_audit

            with patch.dict("sys.modules", {"ledger_agent.core.audit": fake_audit_mod}):
                # Reduce timeout so the test doesn't wait 2 full seconds
                original_timeout = cleanup_mod._WRITE_LOCK_TIMEOUT_SECS
                cleanup_mod._WRITE_LOCK_TIMEOUT_SECS = 0.1
                try:
                    cycle_cleanup("test_cycle")
                finally:
                    cleanup_mod._WRITE_LOCK_TIMEOUT_SECS = original_timeout
        finally:
            lock_release.set()
            t.join(timeout=2)

        skipped = [e for e, _ in audit_events if e == "cleanup.skipped_busy"]
        assert skipped, (
            "BUG-C1: cleanup.skipped_busy not emitted when write lock was held. "
            "cycle_cleanup must defer rather than race an active writer."
        )

    def test_cleanup_runs_when_lock_free(self, tmp_path, monkeypatch):
        """When no writer holds the lock, cycle_cleanup must proceed normally."""
        audit_events = []

        def fake_audit(event, **kwargs):
            audit_events.append(event)

        fake_audit_mod = MagicMock()
        fake_audit_mod.audit = fake_audit

        # Point raw_cache at a temp directory so we don't touch real data
        monkeypatch.setattr(cleanup_mod, "_RAW_CACHE_DIR", tmp_path / "raw_cache")
        monkeypatch.setattr(cleanup_mod, "_EXPORT_TMP_DIR", tmp_path / "exports")

        with patch.dict("sys.modules", {"ledger_agent.core.audit": fake_audit_mod}):
            cycle_cleanup("free_cycle")

        # Should have emitted cleanup.cycle, not cleanup.skipped_busy
        assert "cleanup.skipped_busy" not in audit_events
        assert "cleanup.cycle" in audit_events

    def test_hold_write_lock_context_manager(self):
        """hold_write_lock() must acquire then release the lock."""
        assert not _write_lock._is_owned()  # type: ignore[attr-defined]
        with hold_write_lock():
            assert _write_lock._is_owned()  # type: ignore[attr-defined]
        assert not _write_lock._is_owned()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# SMELL-C4: _purge_process_scratch skips foreign-uid entries
# ---------------------------------------------------------------------------

class TestSmellC4UidFilter:
    """_purge_process_scratch must not delete entries owned by other users."""

    @pytest.mark.skipif(not hasattr(os, "getuid"), reason="uid not available on this OS")
    def test_foreign_uid_entry_skipped(self, tmp_path, monkeypatch):
        """
        Create a fake scratch entry with a different uid; assert it is NOT
        removed and that cleanup.skipped_foreign_uid is emitted.
        """
        scratch_name = f"{_PROCESS_SCRATCH_PREFIX}test_foreign"
        foreign_entry = tmp_path / scratch_name
        foreign_entry.write_text("foreign user scratch")

        audit_events = []

        def fake_audit(event, **kwargs):
            audit_events.append((event, kwargs))

        fake_audit_mod = MagicMock()
        fake_audit_mod.audit = fake_audit

        # Patch tempfile.gettempdir to return our tmp_path
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

        # Patch os.stat to return a foreign uid for our test entry
        my_uid = os.getuid()
        foreign_uid = my_uid + 1000  # guaranteed different

        original_stat = os.stat

        def patched_stat(path, **kwargs):
            result = original_stat(path, **kwargs)
            if Path(path).name == scratch_name:
                # Return a stat result with a different st_uid
                import stat as stat_mod

                class FakeStat:
                    def __getattr__(self, name):
                        return getattr(result, name)
                    st_uid = foreign_uid

                return FakeStat()
            return result

        with patch.dict("sys.modules", {"ledger_agent.core.audit": fake_audit_mod}):
            with patch("os.stat", side_effect=patched_stat):
                with patch("ledger_agent.core.cleanup.os.stat", side_effect=patched_stat):
                    _purge_process_scratch()

        # Entry must still exist (was not deleted)
        assert foreign_entry.exists(), (
            "SMELL-C4: foreign-uid scratch entry was deleted — confused-deputy risk"
        )

        # audit event must have been emitted
        foreign_skipped = [
            kwargs for event, kwargs in audit_events
            if event == "cleanup.skipped_foreign_uid"
        ]
        assert foreign_skipped, (
            "SMELL-C4: cleanup.skipped_foreign_uid not emitted for foreign-uid entry"
        )

    @pytest.mark.skipif(not hasattr(os, "getuid"), reason="uid not available on this OS")
    def test_own_uid_entry_deleted(self, tmp_path, monkeypatch):
        """Entries owned by the current user must be deleted normally."""
        scratch_name = f"{_PROCESS_SCRATCH_PREFIX}test_own"
        own_entry = tmp_path / scratch_name
        own_entry.write_text("my scratch")

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

        _purge_process_scratch()

        assert not own_entry.exists(), (
            "SMELL-C4: own-uid scratch entry was NOT deleted — cleanup broken"
        )
