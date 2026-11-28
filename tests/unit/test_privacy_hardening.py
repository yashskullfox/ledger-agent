"""
tests/unit/test_privacy_hardening.py — P-15: privacy hardening left over from P-14.

1. The ``log`` scope runs the ``partial_account`` and ``digit_run`` detectors, so
   ``logging_setup._redact_log_text`` needs a single pass (the P-14 second
   ``memory_file`` pass is gone).
2. ``ledger-agent-mcp`` ``main()`` attaches the log redaction filter right after
   its own ``basicConfig`` (DEBUG with ``--debug``), before ``configure_logging``.
3. Opaque ``ref:`` ids always contain a letter, so the ``log`` scope never
   tokenises them as a digit run and the ref on stderr matches the client's.

Synthetic data only.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import sys

import pytest

from ledger_agent.core import config as _cfg
from ledger_agent.core import logging_setup, privacy
from ledger_agent.core.logging_setup import LogRedactionFilter, configure_logging

PARTNER = "Zorvath Quillmont"
# Built at runtime so the source holds no literal long digit run.
DIGITS = "".join(["5829", "1047", "3361"])
MASKED_TAIL = "64" + "02"
MASKED = "acct_****" + MASKED_TAIL

_HEX_REF = re.compile(r"[0-9a-f]{8}")
N_REFS = 10_000


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    monkeypatch.setattr(_cfg, "AI_EGRESS_MODE", "redact")
    monkeypatch.setattr(_cfg, "PRIVACY_ENTITY_NAME", "")
    monkeypatch.delenv("FI_PRIVACY_ENTITY_NAME", raising=False)
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


def _assert_no_pii(err: str) -> None:
    flat = "".join(err.split()).lower()
    for value in (PARTNER, "Zorvath", "Quillmont", DIGITS, MASKED, MASKED_TAIL):
        assert "".join(value.split()).lower() not in flat, "raw value reached stderr"


# ── 1. log scope + single-pass log redaction ─────────────────────────────────

def test_log_scope_includes_account_detectors():
    detectors = privacy._SCOPE_DETECTORS["log"]
    assert "partial_account" in detectors
    assert "digit_run" in detectors
    # The P-14 two-pass workaround relied on log ∪ memory_file; log alone now
    # covers every memory_file detector.
    assert set(privacy._SCOPE_DETECTORS["memory_file"]) <= set(detectors)


def test_log_scope_redacts_digit_run_and_masked_ref():
    out, _ = privacy.redact(f"moved to {DIGITS} from {MASKED} for {PARTNER}", scope="log")
    _assert_no_pii(out)
    assert "<ACCT_" in out


def test_redact_log_text_is_single_log_pass(monkeypatch):
    calls: list[str] = []
    real = privacy.redact

    def spy(text, scope="openai", entity_name=""):
        calls.append(scope)
        return real(text, scope=scope, entity_name=entity_name)

    monkeypatch.setattr(privacy, "redact", spy)
    out = logging_setup._redact_log_text(f"row {DIGITS} / {MASKED} / {PARTNER}")
    assert calls == ["log"]
    _assert_no_pii(out)


# ── 2. ledger-agent-mcp main(): filter installed after basicConfig ───────────

def _run_mcp_main(monkeypatch, argv: list[str], emit) -> None:
    import dotenv

    from ledger_agent.mcp import server

    monkeypatch.setattr(sys, "argv", ["ledger-agent-mcp", *argv])
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)  # no real .env
    monkeypatch.setattr(server, "serve_stdio", emit)
    server.main()


def test_mcp_main_debug_redacts_stderr(monkeypatch, capsys):
    seen: dict[str, object] = {}

    def emit() -> None:
        root = logging.getLogger()
        seen["level"] = root.level
        seen["filtered"] = bool(root.handlers) and all(
            any(isinstance(f, LogRedactionFilter) for f in h.filters) for h in root.handlers
        )
        log = logging.getLogger("ledger_agent.mcp.server")
        log.debug("P15 probe partner=%s acct=%s masked=%s", PARTNER, DIGITS, MASKED)
        try:
            raise ValueError(f"lookup {PARTNER} {DIGITS}")
        except ValueError:
            log.debug("P15 probe traceback", exc_info=True)

    with _isolated_logging():
        _run_mcp_main(monkeypatch, ["--debug"], emit)
        err = capsys.readouterr().err

    assert seen["level"] == logging.DEBUG
    assert seen["filtered"] is True
    assert "DEBUG ledger_agent.mcp.server: P15 probe partner=" in err
    assert "P15 probe traceback" in err and "ValueError" in err
    _assert_no_pii(err)


def test_mcp_main_default_level_also_filtered(monkeypatch, capsys):
    def emit() -> None:
        logging.getLogger("ledger_agent.mcp.server").warning(
            "P15 probe %s %s %s", PARTNER, DIGITS, MASKED)

    with _isolated_logging():
        _run_mcp_main(monkeypatch, [], emit)
        err = capsys.readouterr().err

    assert "WARNING ledger_agent.mcp.server: P15 probe" in err
    _assert_no_pii(err)


# ── 3. letter-bearing opaque refs ────────────────────────────────────────────

def _generators():
    from ledger_agent.bridge import jsonrpc_stdio
    from ledger_agent.mcp import server
    from mcp_server import server as legacy

    return {
        "privacy.new_error_ref": privacy.new_error_ref,
        "bridge._new_ref": jsonrpc_stdio._new_ref,
        "mcp._new_ref": server._new_ref,
        "legacy._ref_id": legacy._ref_id,
    }


@pytest.mark.parametrize("name", list(_generators()))
def test_refs_are_hex_with_letter_and_unique(name):
    gen = _generators()[name]
    refs = [gen() for _ in range(N_REFS)]
    for ref in refs:
        assert _HEX_REF.fullmatch(ref), "ref is not 8 lowercase hex chars"
        assert any(c in "abcdef" for c in ref), "ref has no letter"
    # ~32 bits: the expected number of collisions in 10k draws is ~0.01.
    assert len(set(refs)) >= N_REFS - 5


def test_ref_letter_guaranteed_even_if_random_source_gives_digits(monkeypatch):
    import secrets

    monkeypatch.setattr(secrets, "token_hex", lambda n=None: "1" * 8)
    ref = privacy.new_error_ref()
    assert _HEX_REF.fullmatch(ref)
    assert any(c in "abcdef" for c in ref)


def test_refs_survive_log_redaction():
    refs = {privacy.new_error_ref() for _ in range(N_REFS)}
    for ref in refs:
        line = f"Bridge: internal error in dispatch (ValueError) (ref:{ref})"
        assert logging_setup._redact_log_text(line) == line


def test_all_digit_ref_would_be_tokenised():
    """Why P-15 item 3 exists: an all-digit 8-char ref is a bare digit run."""
    digit_ref = "".join(["7364", "5512"])
    out = logging_setup._redact_log_text(f"Internal error (ref:{digit_ref})")
    assert digit_ref not in out


def test_bridge_ref_on_stderr_matches_client_message(monkeypatch, capsys):
    import json

    from ledger_agent.bridge import jsonrpc_stdio

    with _isolated_logging():
        configure_logging(level="DEBUG", fmt="plain")
        for _ in range(200):
            try:
                raise RuntimeError(f"boom {PARTNER} {DIGITS}")
            except RuntimeError as exc:
                jsonrpc_stdio._internal_error(1, exc, where="dispatch")
        captured = capsys.readouterr()

    messages = [json.loads(line)["error"]["message"] for line in captured.out.splitlines()]
    assert len(messages) == 200
    for msg in messages:
        m = re.fullmatch(r"Internal error \(ref:([0-9a-f]{8})\)", msg)
        assert m, "unexpected client error format"
        assert f"(ref:{m.group(1)})" in captured.err
    _assert_no_pii(captured.err)
