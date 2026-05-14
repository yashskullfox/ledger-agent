"""
ledger_agent/core/logging_setup.py  –  Structured logging for FinancialIntelligence
──────────────────────────────────────────────────────────────────────────────────────
ARCH-20: Canonical location. The legacy path ``core.logging_setup`` is a
backward-compatibility shim that re-exports everything from here.

Supports three output formats:
  • rich   – Coloured human-readable output via Rich (default for dev)
  • json   – JSON lines for log aggregators (production / CI)
  • plain  – Plain-text stdlib format (fallback / minimal envs)

Controlled by environment variables:
  FI_LOG_LEVEL   DEBUG | INFO | WARNING | ERROR   (default: INFO)
  FI_LOG_FORMAT  rich | json | plain              (default: rich)

Usage:
    from core.logging_setup import get_logger
    log = get_logger(__name__)
    log.info("Parsed statement", extra={"period": "2025-01", "txns": 11})
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    _RESERVED = {"msg", "args", "levelname", "name", "pathname",
                 "filename", "module", "exc_info", "exc_text",
                 "stack_info", "lineno", "funcName", "created",
                 "msecs", "relativeCreated", "thread", "threadName",
                 "processName", "process", "message", "taskName"}

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        obj: Dict[str, Any] = {
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
        return json.dumps(obj, default=str)


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

    from config import LOG_LEVEL, LOG_FORMAT
    level = (level or LOG_LEVEL).upper()
    fmt = (fmt or LOG_FORMAT).lower()

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))

    # Remove any handlers already attached (e.g. by imported libs)
    for h in root.handlers[:]:
        root.removeHandler(h)

    if fmt == "json":
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
    elif fmt == "rich":
        try:
            from rich.logging import RichHandler
            handler = RichHandler(
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

    # Install PrivacyFilter on the handler so PII is stripped before emit (R-46)
    try:
        from ledger_agent.core.privacy import PrivacyFilter
        handler.addFilter(PrivacyFilter())
    except Exception:
        pass  # Privacy filter is best-effort; never prevent logging from working

    root.addHandler(handler)

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
