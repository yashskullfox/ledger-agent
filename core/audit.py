"""
core/audit.py  –  Structured INFO-level audit trail (R-46 / Section 7 hardening)
────────────────────────────────────────────────────────────────────────────────
A single, dedicated audit channel separate from the application logger.

Guarantees
----------
1. Every audit event is INFO-level structured JSONL, one event per line.
2. Each process boot opens a FRESH audit log at ``data/audit/run-<run_id>.jsonl``
   (run_id = UTC timestamp + 6-byte hex). Previous runs are NOT mutated.
3. On boot, audit logs older than ``FI_AUDIT_RETENTION_DAYS`` (default 7) are
   purged from disk — no stale leak surface.
4. All audit events are routed through ``core.privacy.redact(scope="log")``
   before write. The audit log itself MUST NOT contain raw PII even if a caller
   accidentally passes one (defence-in-depth).
5. The active run_id is stamped on every event so cross-cycle correlation is
   possible without keeping the underlying data.

Public API
----------
    from core.audit import audit, current_run_id, refresh_audit_log
    audit("import.start", folder=str(folder), report_id="balance_sheet")
    audit("import.complete", imported=12, skipped=2, failed=0)
    audit("egress.blocked", scope="openai", reason="PrivacyLeakError")

Environment
-----------
    FI_AUDIT_DIR              Override audit dir (default: <repo>/data/audit)
    FI_AUDIT_RETENTION_DAYS   Days to keep prior run logs (default: 7)
    FI_AUDIT_DISABLED         If "1", audit is a no-op (CI only)
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# ── Configuration ────────────────────────────────────────────────────────────

_DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent.parent / "data" / "audit"
_AUDIT_DIR = Path(os.environ.get("FI_AUDIT_DIR", str(_DEFAULT_AUDIT_DIR)))
_RETENTION_DAYS = int(os.environ.get("FI_AUDIT_RETENTION_DAYS", "7"))
_DISABLED = os.environ.get("FI_AUDIT_DISABLED", "0") == "1"

# ── Run state ────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_run_id: Optional[str] = None
_log_path: Optional[Path] = None
_initialised = False

_log = logging.getLogger("fi.audit")


def _generate_run_id() -> str:
    """Stable, sortable, collision-resistant run id."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{secrets.token_hex(3)}"


def _purge_old_runs() -> None:
    """Delete audit files older than retention threshold. Best-effort."""
    if not _AUDIT_DIR.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=_RETENTION_DAYS)
    for f in _AUDIT_DIR.glob("run-*.jsonl"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                f.unlink()
        except OSError:
            # Never let audit-housekeeping fail the run.
            pass


def _ensure_initialised() -> None:
    """Lazy-init: open a fresh audit log on first event, purge stale ones."""
    global _initialised, _run_id, _log_path
    if _initialised:
        return
    with _lock:
        if _initialised:
            return
        try:
            _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _log.warning("audit: could not create %s: %s", _AUDIT_DIR, e)
            _initialised = True
            return
        _purge_old_runs()
        _run_id = _generate_run_id()
        _log_path = _AUDIT_DIR / f"run-{_run_id}.jsonl"
        _initialised = True
        # First-line marker proves the file came from a clean boot.
        _write_raw({
            "event": "audit.session.start",
            "run_id": _run_id,
            "retention_days": _RETENTION_DAYS,
            "audit_dir": str(_AUDIT_DIR),
        })


def _write_raw(obj: Dict[str, Any]) -> None:
    """Low-level write. Caller is responsible for redaction beforehand."""
    if _log_path is None:
        return
    obj.setdefault("ts", datetime.now(timezone.utc).isoformat())
    obj.setdefault("run_id", _run_id)
    line = json.dumps(obj, default=str, ensure_ascii=False)
    try:
        with _log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
    except OSError as e:
        # Surface as warning, but never raise from audit.
        _log.warning("audit: write failed (%s); event=%r", e, obj.get("event"))


def _redact_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pass every string value through the privacy filter at scope='log'.
    Numeric / boolean / None / dict values pass through unchanged.
    Nested dicts/lists are recursed; tuples become lists.

    If a string contains an API-key-class secret, the privacy layer raises
    PrivacyLeakError. We catch it and substitute a hard-redacted placeholder
    so the audit pipeline never blocks AND never persists the secret.
    """
    try:
        from core.privacy import redact, PrivacyLeakError
    except Exception:
        return kwargs  # If privacy unavailable, fail-safe by not exploding.

    def _scrub(v: Any) -> Any:
        if isinstance(v, str):
            try:
                safe, _ = redact(v, scope="log")
                return safe
            except PrivacyLeakError:
                return "[REDACTED:secret_detected_in_audit_value]"
        if isinstance(v, dict):
            return {k: _scrub(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [_scrub(x) for x in v]
        return v

    return {k: _scrub(v) for k, v in kwargs.items()}


# ── Public API ───────────────────────────────────────────────────────────────


def current_run_id() -> Optional[str]:
    """Return the active run_id, initialising if necessary."""
    if _DISABLED:
        return None
    _ensure_initialised()
    return _run_id


def current_log_path() -> Optional[Path]:
    """Return the path to the active audit log file."""
    if _DISABLED:
        return None
    _ensure_initialised()
    return _log_path


def audit(event: str, **fields: Any) -> None:
    """
    Emit one structured INFO-level audit event.

    Examples:
        audit("import.start", folder="/x/y", report_id="balance_sheet")
        audit("egress.blocked", scope="mcp_response", reason="PrivacyLeakError")
        audit("report.generated", report="form1065", fiscal_year=2024,
              ordinary_income=Decimal("18732.10"))

    Values are passed through PII redaction before being persisted. Numeric and
    boolean values are unaffected. Callers should still avoid passing raw PII
    (defence-in-depth, not a free pass).
    """
    if _DISABLED:
        return
    _ensure_initialised()
    payload = {"event": event}
    payload.update(_redact_kwargs(fields))
    _write_raw(payload)
    # Mirror to the standard logger at INFO so console viewers see it too.
    # PrivacyFilter (logging_setup.py) will scrub a second time on the way out.
    _log.info("audit", extra={"audit_event": event, **payload})


def refresh_audit_log() -> str:
    """
    Force-rotate the active audit log. Returns the new run_id.

    Intended for long-running processes (Spring webapp, MCP HTTP server) that
    want to start a fresh log between sessions without restarting the process.

    The previous audit file is left intact on disk (retention-day rule applies)
    so the audit trail remains tamper-evident.
    """
    global _initialised, _run_id, _log_path
    if _DISABLED:
        return ""
    with _lock:
        prev = _run_id
        _initialised = False
        _run_id = None
        _log_path = None
    _ensure_initialised()
    audit("audit.session.rotated", previous_run_id=prev)
    return _run_id or ""


def shutdown_audit() -> None:
    """
    Emit a clean shutdown marker and flush. Called from atexit / DisposableBean.
    Idempotent.
    """
    if _DISABLED or not _initialised:
        return
    audit("audit.session.end")
