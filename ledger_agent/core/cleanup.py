"""
core/cleanup.py  –  Raw-data cleanup cycle (R-46 / Section 7 hardening)
────────────────────────────────────────────────────────────────────────────────
Closes the leak window between "parse a PDF / load a redaction map" and "next
process boot". Every intermediate that could contain raw PII is purged at the
end of each run cycle. Re-running ``boot_cleanup()`` on startup also removes
anything an earlier crashed run might have left behind.

What gets cleaned
-----------------
1. **In-memory caches**
   - ``core.privacy._session_map`` (raw → token mapping)
   - ``core.privacy._session_counters``
   - Any callable registered via ``register_cleanup_hook(fn)``.
2. **On-disk transient files**
   - ``data/raw_cache/**``  (intermediate PDF text / unredacted JSON)
   - ``data/exports/_tmp/**`` (half-written exports)
   - ``$TMPDIR/ledger-agent-raw-*`` (process-local scratch)
3. **Audit handoff**
   - Emits ``cleanup.start`` / ``cleanup.complete`` audit events so each cycle
     is verifiable from the audit log alone.

Public API
----------
    from ledger_agent.core.cleanup import run_cycle, register_cleanup_hook, boot_cleanup

    # One-shot wrap around a job:
    with run_cycle("import_statements"):
        import_statements(folder)

    # Or manual:
    boot_cleanup()    # call once at process boot
    try:
        ...
    finally:
        cycle_cleanup("manual")
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, List, Optional

# ── Configuration ────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RAW_CACHE_DIR = Path(os.environ.get(
    "FI_RAW_CACHE_DIR", str(_REPO_ROOT / "data" / "raw_cache")
))
_EXPORT_TMP_DIR = Path(os.environ.get(
    "FI_EXPORT_TMP_DIR", str(_REPO_ROOT / "data" / "exports" / "_tmp")
))
_PROCESS_SCRATCH_PREFIX = "ledger-agent-raw-"

_log = logging.getLogger("fi.cleanup")
_lock = threading.Lock()
# BUG-C1 fix: write lock shared between raw-data writers and cycle_cleanup.
# Writers acquire via hold_write_lock(); cleanup acquires with a short timeout
# and emits cleanup.skipped_busy if it cannot (rather than racing with writers).
_write_lock = threading.Lock()
_hooks: List[Callable[[], None]] = []


# ── Write-lock API (BUG-C1) ───────────────────────────────────────────────────


@contextmanager
def hold_write_lock() -> Iterator[None]:
    """
    Acquire the per-cycle write lock before writing any raw artefacts.

    Prevents cycle_cleanup from purging files that are still being written.
    The context manager is re-entrant-safe for the *same* thread only if the
    caller is careful: it uses a plain ``threading.Lock``, so recursive
    acquisition from the same thread will deadlock just as any Lock would.

    Usage::

        with hold_write_lock():
            write_pdf_to_raw_cache(data)
    """
    _write_lock.acquire()
    try:
        yield
    finally:
        _write_lock.release()


# ── Hook registry ────────────────────────────────────────────────────────────


def register_cleanup_hook(fn: Callable[[], None]) -> None:
    """
    Register a callback invoked at the end of every cycle and on boot cleanup.
    Hooks must be idempotent. Exceptions are swallowed (logged at WARNING).
    """
    with _lock:
        if fn not in _hooks:
            _hooks.append(fn)


def _run_hooks() -> int:
    """Run every registered hook. Returns count of successful invocations."""
    ok = 0
    for hook in list(_hooks):
        try:
            hook()
            ok += 1
        except Exception as e:
            _log.warning("cleanup hook failed: %s: %s", hook, e)
            # SMELL-C2 fix: surface hook failures to the audit trail so
            # operators can identify which hook broke without losing
            # the "best-effort" semantics.
            try:
                from ledger_agent.core.audit import audit
                audit("cleanup.hook_failed",
                      hook=getattr(hook, "__qualname__", str(hook)),
                      error_type=type(e).__name__,
                      error_message=str(e)[:200])
            except Exception:
                pass  # never let audit failure block the cleanup loop
    return ok


# ── Filesystem purge ─────────────────────────────────────────────────────────


def _purge_directory(d: Path, *, keep_dir: bool = True) -> int:
    """Delete everything under ``d``. Returns count of entries removed."""
    if not d.exists():
        return 0
    removed = 0
    for entry in d.iterdir():
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
        except OSError as e:
            _log.warning("cleanup: cannot remove %s: %s", entry, e)
    if not keep_dir:
        try:
            d.rmdir()
        except OSError:
            pass
    return removed


def _purge_process_scratch() -> int:
    """Sweep ``$TMPDIR/ledger-agent-raw-*`` left by this or prior runs.

    SMELL-C4 fix: only removes entries owned by the current effective uid.
    On a shared host another user can pre-create ``ledger-agent-raw-<x>``
    directories; a uid check prevents confused-deputy deletion of those.
    """
    tmp = Path(tempfile.gettempdir())
    my_uid = os.geteuid()
    removed = 0
    for entry in tmp.glob(f"{_PROCESS_SCRATCH_PREFIX}*"):
        try:
            # Skip entries not owned by us — avoids confused-deputy errors.
            st = entry.stat()
            if st.st_uid != my_uid:
                # SMELL-C4 fix: emit audit event so operators can detect
                # pre-created trap directories on shared hosts.
                try:
                    from ledger_agent.core.audit import audit
                    audit("cleanup.skipped_foreign_uid",
                          path=str(entry), uid=st.st_uid)
                except Exception:
                    pass
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
    return removed


def _purge_privacy_session() -> None:
    """Wipe the in-memory pseudonym registry."""
    try:
        from ledger_agent.core.privacy import _reset_session
        _reset_session()
    except Exception:
        pass


# ── Shared cleanup core ───────────────────────────────────────────────────────


def _do_cleanup(event_name: str, *, run_hooks: bool, label: Optional[str] = None) -> None:
    """
    SMELL-C3 fix: single implementation shared by boot_cleanup and
    cycle_cleanup. Previously those two functions were near-duplicates
    (only a handful of lines differed), which allowed them to drift silently.

    :param event_name:  Audit event key (``"cleanup.boot"`` or ``"cleanup.cycle"``).
    :param run_hooks:   Whether to invoke registered cleanup hooks (boot does not).
    :param label:       Arbitrary label forwarded to the audit event (cycle only).
    """
    try:
        from ledger_agent.core.audit import audit
    except Exception:
        audit = None  # type: ignore

    # BUG-C1 fix: acquire write lock before purging so we don't race with
    # workers that are still writing raw artefacts under hold_write_lock().
    # A 2-second timeout is generous for any in-flight write; if it expires
    # we skip this cycle's purge rather than block indefinitely.
    acquired = _write_lock.acquire(blocking=True, timeout=2.0)
    if not acquired:
        if audit:
            kwargs: dict = {"event": event_name}
            if label is not None:
                kwargs["label"] = label
            audit("cleanup.skipped_busy", **kwargs)
        return

    try:
        raw = _purge_directory(_RAW_CACHE_DIR)
        exp = _purge_directory(_EXPORT_TMP_DIR)
        scratch = _purge_process_scratch()
        hooks_ran = _run_hooks() if run_hooks else 0
        _purge_privacy_session()
    finally:
        _write_lock.release()

    if audit:
        payload = dict(
            raw_cache_removed=raw,
            export_tmp_removed=exp,
            process_scratch_removed=scratch,
        )
        if run_hooks:
            payload["hooks_ran"] = hooks_ran
        if label is not None:
            payload["label"] = label
        audit(event_name, **payload)


# ── Public API ───────────────────────────────────────────────────────────────


def boot_cleanup() -> None:
    """
    Call once at process boot. Purges anything a crashed prior run left behind
    so the new run starts with zero residue.
    """
    _do_cleanup("cleanup.boot", run_hooks=False)


def cycle_cleanup(label: str = "cycle") -> None:
    """
    Called at the end of every job cycle. Wipes all transients and the
    in-memory redaction map so no raw PII outlives the cycle.
    """
    _do_cleanup("cleanup.cycle", run_hooks=True, label=label)


@contextmanager
def run_cycle(label: str) -> Iterator[None]:
    """
    Context manager that bookends a job with audit start/end and forced cleanup.

    Usage::

        with run_cycle("balance_sheet"):
            generate_balance_sheet(2024)

    Cleanup runs whether the body succeeds or raises. The exception (if any)
    is re-raised after cleanup completes.
    """
    try:
        from ledger_agent.core.audit import audit
    except Exception:
        audit = None  # type: ignore

    if audit:
        audit("cycle.start", label=label)
    try:
        yield
    except Exception as e:
        if audit:
            audit("cycle.error", label=label, error_type=type(e).__name__)
        raise
    finally:
        cycle_cleanup(label)
        if audit:
            audit("cycle.end", label=label)


def raw_cache_dir() -> Path:
    """
    Return a directory safe for short-lived raw artefacts.
    Always returned inside ``data/raw_cache`` so the cycle cleanup can wipe it.
    """
    _RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _RAW_CACHE_DIR


def export_tmp_dir() -> Path:
    """Return the staging directory for half-written exports."""
    _EXPORT_TMP_DIR.mkdir(parents=True, exist_ok=True)
    return _EXPORT_TMP_DIR
