"""
tests/unit/test_bridge_errors.py  –  P-13: bridge errors fail closed.

``bridge/jsonrpc_stdio.py`` used to send ``traceback.format_exc()`` to the Java
client on two paths: the generic ``except`` in ``_dispatch`` and the main-loop
``except`` in ``serve``.  The Java side shows that text in the web page / API
body, and an exception message can carry raw values.  Both paths now send an
opaque ``Internal error (ref:XXXXXXXX)`` with JSON-RPC code -32603; stderr gets
the exception TYPE and ref at WARNING (traceback at DEBUG only) and the audit
log gets ``bridge.internal_error`` with type + ref.

Synthetic names and digits only.
"""
from __future__ import annotations

import io
import json
import logging
import re

import pytest

import ledger_agent.bridge.jsonrpc_stdio as bridge

PARTNER = "Thaddeus Wrenfield"  # synthetic
DIGITS = "".join(str(i % 10) for i in range(1, 11))  # synthetic 10-digit run
SECRET_MSG = f"{PARTNER} {DIGITS}"
_REF_RE = re.compile(r"^Internal error \(ref:[0-9a-f]{8}\)$")


def _assert_opaque(line: str, resp: dict) -> str:
    """The frame is a -32603 opaque error carrying none of the exception text."""
    assert "result" not in resp
    err = resp["error"]
    assert err["code"] == -32603
    assert _REF_RE.match(err["message"]), err["message"]
    for needle in (SECRET_MSG, PARTNER, "Thaddeus", "Wrenfield", DIGITS, "Traceback"):
        assert needle not in line
    return err["message"][len("Internal error (ref:"):-1]


@pytest.fixture
def audit_events(monkeypatch):
    import ledger_agent.core.audit as audit_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(audit_mod, "audit", lambda event, **f: events.append((event, f)))
    monkeypatch.setattr(audit_mod, "shutdown_audit", lambda: None)
    return events


@pytest.fixture
def bridge_log():
    """Capture the bridge logger's records directly (robust to root reconfiguration)."""
    records: list[logging.LogRecord] = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger(bridge.__name__)
    handler = _H(level=logging.DEBUG)
    prev = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(prev)


def _raising_tool(monkeypatch, exc: BaseException):
    import ledger_agent.mcp.tools as tools

    def _boom(name, args, *, allow_pii=False):
        raise exc

    monkeypatch.setattr(tools, "call_tool", _boom)


def _dispatch(capsys, method="customer_summary", params=None):
    capsys.readouterr()
    bridge._dispatch({"jsonrpc": "2.0", "id": 7, "method": method,
                      "params": params if params is not None else {"fiscal_year": 2026}})
    out = capsys.readouterr().out
    lines = [ln for ln in out.split("\n") if ln]
    assert len(lines) == 1, "bridge must write exactly one JSON-RPC frame"
    return lines[0], json.loads(lines[0])


def _check_logs_and_audit(ref, error_type, records, events, where):
    rendered = [r.getMessage() for r in records]
    assert not any(n in m for m in rendered for n in (PARTNER, DIGITS))
    warn = [r for r in records if r.levelno == logging.WARNING and ref in r.getMessage()]
    assert warn and error_type in warn[0].getMessage()
    assert warn[0].exc_info is None, "no traceback above DEBUG"
    debug = [r for r in records if r.levelno == logging.DEBUG and ref in r.getMessage()]
    assert debug and debug[0].exc_info, "full traceback is kept at DEBUG only"
    hits = [f for e, f in events if e == "bridge.internal_error"]
    assert hits == [{"where": where, "error_type": error_type, "ref": ref}]
    assert PARTNER not in repr(events) and DIGITS not in repr(events)


# ── _dispatch generic except ─────────────────────────────────────────────────

def test_tool_value_error_is_opaque(capsys, monkeypatch, audit_events, bridge_log):
    _raising_tool(monkeypatch, ValueError(SECRET_MSG))
    line, resp = _dispatch(capsys)
    assert resp["id"] == 7
    ref = _assert_opaque(line, resp)
    _check_logs_and_audit(ref, "ValueError", bridge_log, audit_events,
                          "tool:customer_summary")
    # The pre-existing tool_error event still records the type only.
    assert ("bridge.tool_error",
            {"method": "customer_summary", "error_type": "ValueError"}) in audit_events


def test_non_value_error_is_opaque(capsys, monkeypatch, audit_events, bridge_log):
    _raising_tool(monkeypatch, KeyError(SECRET_MSG))
    line, resp = _dispatch(capsys, "reconcile_year")
    ref = _assert_opaque(line, resp)
    _check_logs_and_audit(ref, "KeyError", bridge_log, audit_events, "tool:reconcile_year")


def test_missing_argument_is_opaque_32603(capsys, audit_events):
    """Real call_tool: a missing fiscal_year (KeyError) keeps code -32603."""
    line, resp = _dispatch(capsys, params={})
    _assert_opaque(line, resp)
    assert "fiscal_year" not in line


def test_refs_are_unique(capsys, monkeypatch, audit_events):
    _raising_tool(monkeypatch, ValueError(SECRET_MSG))
    _l1, r1 = _dispatch(capsys)
    _l2, r2 = _dispatch(capsys)
    assert r1["error"]["message"] != r2["error"]["message"]


# ── serve() main-loop except ─────────────────────────────────────────────────

def _serve(monkeypatch, capsys, stdin_text: str) -> list[str]:
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)  # never read .env
    monkeypatch.setattr(bridge, "_background_init", lambda: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    capsys.readouterr()
    bridge.serve()
    return [ln for ln in capsys.readouterr().out.split("\n") if ln]


def test_main_loop_error_is_opaque(capsys, monkeypatch, audit_events, bridge_log):
    def _boom(msg):
        raise RuntimeError(SECRET_MSG)

    monkeypatch.setattr(bridge, "_dispatch", _boom)
    lines = _serve(monkeypatch, capsys, '{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n')
    assert len(lines) == 1
    resp = json.loads(lines[0])
    assert resp["id"] is None
    ref = _assert_opaque(lines[0], resp)
    _check_logs_and_audit(ref, "RuntimeError", bridge_log, audit_events, "main_loop")


def test_main_loop_non_object_request_is_opaque(capsys, monkeypatch, audit_events):
    """A JSON array makes the real ``_dispatch`` raise; the loop keeps serving."""
    lines = _serve(monkeypatch, capsys,
                   '[1, 2]\n{"jsonrpc": "2.0", "id": 2, "method": "ping"}\n')
    assert len(lines) == 2
    _assert_opaque(lines[0], json.loads(lines[0]))
    assert json.loads(lines[1]) == {"jsonrpc": "2.0", "id": 2, "result": {"pong": True}}


def test_no_traceback_module_in_bridge():
    assert not hasattr(bridge, "traceback")
