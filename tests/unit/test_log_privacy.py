"""
tests/unit/test_log_privacy.py — P-14: no raw PII on stderr, even at DEBUG.

stderr is inherited by the Java webapp from the bridge process.  The logging
redaction filter must redact the fully rendered record (``msg % args``, the
traceback, the stack and string extras) for every handler ``configure_logging``
creates, and the bridge must refuse DEBUG unless ``FI_BRIDGE_DEBUG=1``.
Synthetic data only.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sys

import pytest

from ledger_agent.core import config as _cfg
from ledger_agent.core import logging_setup, privacy
from ledger_agent.core.logging_setup import (
    API_KEY_TEXT,
    REDACTION_FAILED_TEXT,
    LogRedactionFilter,
    configure_logging,
    install_log_redaction,
)

PARTNER = "Zorvath Quillmont"
# Built at runtime so the source holds no literal long digit run.
DIGITS = "".join(["4417", "2039", "5886", "1"])
MASKED_TAIL = "73" + "19"
MASKED = "acct_****" + MASKED_TAIL

LOGGER = "tests.log_privacy"


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    monkeypatch.setattr(_cfg, "AI_EGRESS_MODE", "redact")
    monkeypatch.setattr(_cfg, "PRIVACY_ENTITY_NAME", "")
    monkeypatch.delenv("FI_PRIVACY_ENTITY_NAME", raising=False)
    monkeypatch.delenv("FI_BRIDGE_DEBUG", raising=False)
    for key in list(os.environ):
        if key.startswith("FI_PARTNER_") and key.endswith("_NAME"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FI_PARTNER_1_NAME", PARTNER)
    monkeypatch.setenv("FI_DB_PATH", str(tmp_path / "absent" / "none.db"))
    privacy._reset_session()
    privacy.refresh_protected_names()
    yield
    privacy._reset_session()
    privacy.refresh_protected_names()


@contextlib.contextmanager
def _isolated_logging():
    """Empty root logger + fresh logging_setup state; everything restored after."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_configured = logging_setup._configured
    saved_floor = logging_setup._level_floor
    for h in saved_handlers:
        root.removeHandler(h)
    logging_setup._configured = False
    logging_setup._level_floor = logging.NOTSET
    try:
        yield root
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)
        logging_setup._configured = saved_configured
        logging_setup._level_floor = saved_floor


def _squash(text: str) -> str:
    """Drop all whitespace so a value wrapped across lines is still found."""
    return "".join(text.split())


def _assert_no_pii(err: str) -> None:
    flat = _squash(err).lower()
    for value in (PARTNER, "Zorvath", "Quillmont", DIGITS, MASKED, MASKED_TAIL):
        assert _squash(value).lower() not in flat, "raw value reached stderr"


def _log_failure(log: logging.Logger) -> None:
    try:
        raise ValueError(f"payment to {PARTNER} ref {DIGITS}")
    except ValueError:
        log.debug("lookup failed for %s", MASKED, exc_info=True)


# ── (a) DEBUG traceback, args and exception text are redacted ─────────────────

@pytest.mark.parametrize("fmt", ["plain", "json", "rich"])
def test_debug_traceback_redacted_on_stderr(fmt, capsys):
    with _isolated_logging():
        configure_logging(level="DEBUG", fmt=fmt)
        _log_failure(logging.getLogger(LOGGER))
        err = capsys.readouterr().err

    assert "ValueError" in err
    assert "lookup failed for" in err
    _assert_no_pii(err)
    assert "Logging error" not in err
    assert "RecursionError" not in err


def test_configure_logging_attaches_filter_to_every_handler():
    with _isolated_logging() as root:
        configure_logging(level="DEBUG", fmt="rich")
        assert root.handlers
        for h in root.handlers:
            assert any(isinstance(f, LogRedactionFilter) for f in h.filters)


def test_stack_info_and_extra_redacted(capsys):
    with _isolated_logging():
        configure_logging(level="DEBUG", fmt="json")
        logging.getLogger(LOGGER).debug(
            "context", stack_info=True,
            extra={"payee": f"to {PARTNER}", "ref": DIGITS, "count": 3},
        )
        err = capsys.readouterr().err
    obj = json.loads(err.strip().splitlines()[-1])
    assert obj["count"] == 3
    assert "stack" in obj
    _assert_no_pii(err)


def test_filter_redacts_once_and_clears_exc_info():
    record = None
    try:
        raise KeyError(DIGITS)
    except KeyError:
        record = logging.LogRecord(LOGGER, logging.DEBUG, __file__, 1,
                                   "failed for %s", (MASKED,), sys.exc_info())
    flt = LogRedactionFilter()
    out = flt.filter(record)
    if sys.version_info >= (3, 12):
        # A redacted copy; the original stays intact for other handlers.
        assert isinstance(out, logging.LogRecord) and out is not record
        assert record.exc_info is not None
        red = out
    else:
        assert out is True
        red = record
    assert red.exc_info is None
    assert red.args == ()
    assert "KeyError" in red.exc_text
    _assert_no_pii(red.msg + red.exc_text)
    # Filtering an already-redacted record is a no-op.
    again = flt.filter(red)
    assert again is red or again is True
    assert red.msg and red.exc_text


def test_install_log_redaction_covers_foreign_handlers(capsys):
    with _isolated_logging() as root:
        logging.basicConfig(level=logging.DEBUG, format="%(message)s")
        install_log_redaction()
        install_log_redaction()  # idempotent
        for h in root.handlers:
            assert sum(isinstance(f, LogRedactionFilter) for f in h.filters) == 1
        _log_failure(logging.getLogger(LOGGER))
        err = capsys.readouterr().err
    assert "ValueError" in err
    _assert_no_pii(err)


def test_install_log_redaction_skips_non_stderr_handlers():
    import io

    with _isolated_logging() as root:
        mem = logging.StreamHandler(io.StringIO())
        err = logging.StreamHandler(sys.stderr)
        root.addHandler(mem)
        root.addHandler(err)
        install_log_redaction()
        assert not any(isinstance(f, LogRedactionFilter) for f in mem.filters)
        assert any(isinstance(f, LogRedactionFilter) for f in err.filters)


# ── (b) a redaction failure fails closed ──────────────────────────────────────

def test_redaction_error_gives_placeholder(monkeypatch, capsys):
    def _boom(text, scope="openai", entity_name=""):
        raise RuntimeError(f"redactor crashed on {text}")

    monkeypatch.setattr(privacy, "redact", _boom)
    with _isolated_logging():
        configure_logging(level="DEBUG", fmt="plain")
        _log_failure(logging.getLogger(LOGGER))
        err = capsys.readouterr().err

    assert REDACTION_FAILED_TEXT in err
    assert "redactor crashed" not in err
    assert "lookup failed" not in err
    _assert_no_pii(err)


def test_api_key_in_log_gives_fixed_text(monkeypatch, capsys):
    def _leak(text, scope="openai", entity_name=""):
        raise privacy.PrivacyLeakError("API key detected")

    monkeypatch.setattr(privacy, "redact", _leak)
    with _isolated_logging():
        configure_logging(level="INFO", fmt="plain")
        logging.getLogger(LOGGER).warning("key for %s", PARTNER)
        err = capsys.readouterr().err
    assert API_KEY_TEXT in err
    _assert_no_pii(err)


def test_bad_format_args_never_echoed(capsys):
    with _isolated_logging():
        configure_logging(level="INFO", fmt="plain")
        logging.getLogger(LOGGER).warning("two args %s %s", PARTNER)
        err = capsys.readouterr().err
    assert REDACTION_FAILED_TEXT in err
    _assert_no_pii(err)


# ── (c) bridge: DEBUG needs FI_BRIDGE_DEBUG=1 ────────────────────────────────

def _warning_lines(err: str) -> list[str]:
    return [ln for ln in err.splitlines() if ln.startswith("WARNING")]


def test_bridge_refuses_debug_without_flag(monkeypatch, capsys):
    from ledger_agent.bridge import jsonrpc_stdio

    monkeypatch.setenv("FI_LOG_LEVEL", "DEBUG")
    with _isolated_logging() as root:
        jsonrpc_stdio._configure_bridge_logging()
        bridge_log = logging.getLogger(jsonrpc_stdio.__name__)
        assert not bridge_log.isEnabledFor(logging.DEBUG)
        bridge_log.debug("debug line must not appear")

        # A later configure_logging (first get_logger in a core module) and a
        # child logger with its own DEBUG level still cannot reach stderr.
        configure_logging(level="DEBUG", fmt="plain")
        assert not bridge_log.isEnabledFor(logging.DEBUG)
        assert root.level >= logging.INFO
        child = logging.getLogger(LOGGER + ".child")
        child.setLevel(logging.DEBUG)
        try:
            child.debug("child debug must not appear")
        finally:
            child.setLevel(logging.NOTSET)
        err = capsys.readouterr().err

    assert "must not appear" not in err
    warnings = _warning_lines(err)
    assert len(warnings) == 1
    assert "FI_BRIDGE_DEBUG" in warnings[0]


def test_bridge_allows_debug_with_flag_still_redacted(monkeypatch, capsys):
    from ledger_agent.bridge import jsonrpc_stdio

    monkeypatch.setenv("FI_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("FI_BRIDGE_DEBUG", "1")
    with _isolated_logging():
        jsonrpc_stdio._configure_bridge_logging()
        bridge_log = logging.getLogger(jsonrpc_stdio.__name__)
        assert bridge_log.isEnabledFor(logging.DEBUG)
        _log_failure(bridge_log)
        err = capsys.readouterr().err

    assert "lookup failed for" in err
    assert "ValueError" in err
    assert len(_warning_lines(err)) == 1
    _assert_no_pii(err)


def test_bridge_internal_error_debug_traceback_redacted(monkeypatch, capsys):
    from ledger_agent.bridge import jsonrpc_stdio

    monkeypatch.setenv("FI_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("FI_BRIDGE_DEBUG", "1")
    with _isolated_logging():
        jsonrpc_stdio._configure_bridge_logging()
        try:
            raise ValueError(f"{PARTNER} {DIGITS} {MASKED}")
        except ValueError as exc:
            jsonrpc_stdio._internal_error(7, exc, where="test")
        captured = capsys.readouterr()

    assert "Traceback" in captured.err
    assert "ValueError" in captured.err
    _assert_no_pii(captured.err)
    _assert_no_pii(captured.out)


def test_bridge_info_level_unchanged(monkeypatch, capsys):
    from ledger_agent.bridge import jsonrpc_stdio

    monkeypatch.setenv("FI_LOG_LEVEL", "INFO")
    with _isolated_logging() as root:
        jsonrpc_stdio._configure_bridge_logging()
        assert root.level == logging.WARNING
        assert logging_setup.get_level_floor() == logging.NOTSET
        err = capsys.readouterr().err
    assert _warning_lines(err) == []


# ── (d) plain INFO text is not over-redacted ─────────────────────────────────

@pytest.mark.parametrize("fmt", ["plain", "json"])
def test_info_logs_unchanged(fmt, capsys):
    with _isolated_logging():
        configure_logging(level="INFO", fmt=fmt)
        log = logging.getLogger(LOGGER)
        log.info("Loaded 12 statements")
        log.info("Imported %d transactions for %s", 11, "2025-01")
        err = capsys.readouterr().err
    assert "Loaded 12 statements" in err
    assert "Imported 11 transactions for 2025-01" in err
