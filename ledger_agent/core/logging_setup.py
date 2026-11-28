"""
core/logging_setup.py  –  Structured logging for FinancialIntelligence
────────────────────────────────────────────────────────────────────────
Supports three output formats:
  • rich   – Coloured human-readable output via Rich (default for dev)
  • json   – JSON lines for log aggregators (production / CI)
  • plain  – Plain-text stdlib format (fallback / minimal envs)

Controlled by environment variables:
  FI_LOG_LEVEL   DEBUG | INFO | WARNING | ERROR   (default: INFO)
  FI_LOG_FORMAT  rich | json | plain              (default: rich)

Every handler writes to stderr, which the Java webapp inherits from the bridge
process, so stderr is treated as egress (P-14): ``LogRedactionFilter`` redacts
the fully rendered record — message with its ``%`` args, traceback, stack and
string ``extra`` fields — before any handler formats it.

Usage:
    from ledger_agent.core.logging_setup import get_logger
    log = get_logger(__name__)
    log.info("Parsed statement", extra={"period": "2025-01", "txns": 11})
"""
from __future__ import annotations

import copy
import json
import logging
import sys
import threading
from datetime import datetime, timezone
from typing import Any

# Standard LogRecord attributes (plus Formatter-set ones).  Anything else on a
# record came from ``extra={}``.
_RECORD_ATTRS = frozenset({
    "msg", "args", "levelname", "levelno", "name", "pathname",
    "filename", "module", "exc_info", "exc_text",
    "stack_info", "lineno", "funcName", "created",
    "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName", "asctime",
})


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    _RESERVED = {"msg", "args", "levelname", "name", "pathname",
                 "filename", "module", "exc_info", "exc_text",
                 "stack_info", "lineno", "funcName", "created",
                 "msecs", "relativeCreated", "thread", "threadName",
                 "processName", "process", "message", "taskName"}

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        obj: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.message,
        }
        # Attach any extra fields passed via extra={}
        for key, val in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                obj[key] = val
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            # LogRedactionFilter replaces exc_info with the redacted rendering.
            obj["exc"] = record.exc_text
        if record.stack_info:
            obj["stack"] = record.stack_info
        return json.dumps(obj, default=str)


# ── Log redaction (P-14) ──────────────────────────────────────────────────────

REDACTION_FAILED_TEXT = "<log redaction failed>"
API_KEY_TEXT = "[REDACTED: API key detected in log message]"

_guard = threading.local()

# Python 3.12+: a filter may return a LogRecord that replaces the record for
# that handler only.
_FILTER_CAN_REPLACE = sys.version_info >= (3, 12)


def _redact_log_text(text: str) -> str:
    """Redact one rendered log string; raises on any redaction failure.

    One pass with the ``log`` scope, which includes the masked-account-reference
    and bare digit-run detectors since P-15 (stderr is inherited by the Java
    process, so it gets the same account firewall as egress).
    """
    from ledger_agent.core.privacy import redact

    safe, _ = redact(text, scope="log")
    return safe


def _safe_redact(text: str) -> str:
    """``_redact_log_text`` that never raises and never returns the raw text."""
    try:
        return _redact_log_text(text)
    except Exception as exc:  # noqa: BLE001 — fail closed, never the raw text
        try:
            from ledger_agent.core.privacy import PrivacyLeakError
            if isinstance(exc, PrivacyLeakError):
                return API_KEY_TEXT
        except Exception:  # noqa: BLE001
            pass
        return REDACTION_FAILED_TEXT


class LogRedactionFilter(logging.Filter):
    """Handler filter that redacts the FULLY RENDERED record before emit (P-14).

    * message: ``record.getMessage()`` (``msg % args``) is redacted, stored in
      ``record.msg``, and ``record.args`` is cleared;
    * ``exc_info``: rendered with ``logging.Formatter().formatException``,
      redacted, stored in ``record.exc_text``; ``exc_info`` is set to ``None``
      so no handler (Rich tracebacks included) renders the live exception;
    * ``stack_info`` and a pre-set ``exc_text``: redacted in place;
    * string ``extra`` fields (emitted by the JSON formatter): redacted; other
      non-scalar extras are replaced by their redacted ``str()``.

    On Python 3.12+ the filter returns a redacted COPY, so only the handler it is
    attached to emits the redacted form and other handlers (e.g. pytest's log
    capture) still see the original record; on 3.10/3.11, where a filter cannot
    replace the record, the record is redacted in place.

    Never raises and never drops a record: on a redaction error the affected part
    becomes ``REDACTION_FAILED_TEXT``.  Records logged by the redactor itself
    while it runs (``fi.privacy`` DEBUG lines) are dropped by a re-entrancy
    guard; they carry no values.
    """

    def filter(self, record: logging.LogRecord) -> bool | logging.LogRecord:
        if getattr(_guard, "active", False):
            return False  # emitted by redact() itself, while redacting
        if getattr(record, "_fi_log_redacted", False):
            return record if _FILTER_CAN_REPLACE else True
        target = copy.copy(record) if _FILTER_CAN_REPLACE else record
        _guard.active = True
        try:
            self._redact_record(target)
        finally:
            _guard.active = False
        return target if _FILTER_CAN_REPLACE else True

    @staticmethod
    def _redact_record(record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — bad format string / failing __str__
            msg = None
        record.msg = _safe_redact(msg) if msg is not None else REDACTION_FAILED_TEXT
        record.args = ()

        exc_text = None
        if record.exc_info:
            try:
                exc_text = logging.Formatter().formatException(record.exc_info)
            except Exception:  # noqa: BLE001
                exc_text = REDACTION_FAILED_TEXT
        elif record.exc_text:
            exc_text = record.exc_text
        record.exc_info = None
        record.exc_text = (
            _safe_redact(exc_text) if exc_text not in (None, REDACTION_FAILED_TEXT)
            else exc_text
        )

        if record.stack_info:
            record.stack_info = _safe_redact(record.stack_info)

        for key, val in list(record.__dict__.items()):
            if key in _RECORD_ATTRS or key.startswith("_"):
                continue
            if val is None or isinstance(val, (bool, int, float)):
                continue
            try:
                text = val if isinstance(val, str) else str(val)
            except Exception:  # noqa: BLE001
                text = None
            setattr(record, key,
                    _safe_redact(text) if text is not None else REDACTION_FAILED_TEXT)

        if "message" in record.__dict__:
            record.message = record.msg  # a formatter may have cached the raw text
        record._fi_log_redacted = True


def _writes_to_stderr(handler: logging.Handler) -> bool:
    """True for a stream handler on stderr or a Rich handler with a stderr console."""
    stream = getattr(handler, "stream", None)
    if stream is not None and any(stream is s for s in (sys.stderr, sys.__stderr__)):
        return True
    console = getattr(handler, "console", None)  # rich.logging.RichHandler
    return bool(console is not None and getattr(console, "stderr", False))


def install_log_redaction(logger: logging.Logger | None = None) -> None:
    """Attach ``LogRedactionFilter`` to every stderr handler of ``logger`` (default: root).

    Idempotent.  Call after any code that adds handlers outside
    ``configure_logging`` (e.g. ``logging.basicConfig``).  Handlers that write
    elsewhere (files, in-memory capture such as pytest's ``caplog``) are left alone.
    """
    target = logger if logger is not None else logging.getLogger()
    for handler in target.handlers:
        if not _writes_to_stderr(handler):
            continue
        if not any(isinstance(f, LogRedactionFilter) for f in handler.filters):
            handler.addFilter(LogRedactionFilter())


# ── Level floor (bridge DEBUG clamp, P-14) ────────────────────────────────────

_level_floor = logging.NOTSET


def set_level_floor(level: int) -> None:
    """Never let the root logger or its handlers go below ``level``.

    Applied now and again by every later ``configure_logging``.  The handler
    levels also stop records from child loggers that set their own lower level.
    ``logging.NOTSET`` clears the floor (it does not lower current levels).
    """
    global _level_floor
    _level_floor = level
    _apply_level_floor()


def get_level_floor() -> int:
    return _level_floor


def _apply_level_floor() -> None:
    if _level_floor <= logging.NOTSET:
        return
    root = logging.getLogger()
    if root.level < _level_floor:
        root.setLevel(_level_floor)
    for handler in root.handlers:
        if handler.level < _level_floor:
            handler.setLevel(_level_floor)


_PLAIN_FMT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
_PLAIN_DATE = "%Y-%m-%dT%H:%M:%S"

_configured = False


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """
    Call once at startup.  Subsequent calls are no-ops.
    level: DEBUG | INFO | WARNING | ERROR
    fmt:   rich | json | plain
    """
    global _configured
    if _configured:
        return
    _configured = True

    from ledger_agent.core.config import LOG_FORMAT, LOG_LEVEL
    level = (level or LOG_LEVEL).upper()
    fmt = (fmt or LOG_FORMAT).lower()

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))

    # Remove any handlers already attached (e.g. by imported libs)
    for h in root.handlers[:]:
        root.removeHandler(h)

    if fmt == "json":
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter())
    elif fmt == "rich":
        try:
            from rich.console import Console
            from rich.logging import RichHandler
            handler = RichHandler(
                console=Console(stderr=True),
                rich_tracebacks=True,
                show_time=True,
                show_path=False,
                markup=True,
            )
        except ImportError:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter(_PLAIN_FMT, datefmt=_PLAIN_DATE))
    else:  # plain
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_PLAIN_FMT, datefmt=_PLAIN_DATE))

    # Redact the rendered record (message, args, traceback, stack, extras) before
    # emit (R-46 / P-14).  The filter is defined here, so attaching it cannot fail.
    handler.addFilter(LogRedactionFilter())

    root.addHandler(handler)
    _apply_level_floor()

    # Silence noisy third-party loggers
    for noisy in ("pdfminer", "pdfplumber", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    Return a logger for the given name.
    Auto-configures logging on first call if not already done.
    """
    configure_logging()
    return logging.getLogger(name)
