"""
tests/unit/test_mcp_errors.py  –  P-13: Form C (``ledger_agent/mcp/server.py``) errors fail closed.

Before P-13 the MCP server sent ``traceback.format_exc()`` from its stdio main
loop, echoed any ``ValueError`` message and the full redaction-failure text to
the client, and logged tool tracebacks at ERROR.  Now:

* unexpected errors → ``Internal error (ref:XXXXXXXX)``; type + ref at WARNING,
  traceback at DEBUG only, ``mcp.internal_error`` audit event (type + ref);
* a ``ValueError`` is shown to the client only when ``core.api`` / ``mcp.tools``
  raised it themselves (fixed text + the caller's own arguments), and then only
  after redaction; any other ValueError is opaque;
* a redaction failure → ``redaction_failed (ref:…)`` without the error text.

Synthetic names and digits only.
"""
from __future__ import annotations

import io
import json
import logging
import re

import pytest

import ledger_agent.mcp.server as server
from ledger_agent.core import privacy

PARTNER = "Thaddeus Wrenfield"  # synthetic
DIGITS = "".join(str(i % 10) for i in range(1, 11))  # synthetic 10-digit run
SECRET_MSG = f"{PARTNER} {DIGITS}"
_NEEDLES = (PARTNER, "Thaddeus", "Wrenfield", DIGITS, "Traceback")


@pytest.fixture
def audit_events(monkeypatch):
    import ledger_agent.core.audit as audit_mod

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(audit_mod, "audit", lambda event, **f: events.append((event, f)))
    return events


@pytest.fixture
def mcp_log():
    records: list[logging.LogRecord] = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger(server.__name__)
    handler = _H(level=logging.DEBUG)
    prev = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(prev)


def _call(name="customer_summary", args=None) -> tuple[str, dict]:
    resp = server._dispatch({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": name, "arguments": args if args is not None else {"fiscal_year": 2026}},
    })
    assert resp is not None
    return json.dumps(resp, ensure_ascii=False), resp


def _raising_tool(monkeypatch, exc: BaseException):
    import ledger_agent.mcp.tools as tools

    def _boom(name, args, *, allow_pii=False):
        raise exc

    monkeypatch.setattr(tools, "call_tool", _boom)


def _opaque_ref(text: str, resp: dict) -> str:
    assert resp["result"]["isError"] is True
    msg = resp["result"]["content"][0]["text"]
    m = re.fullmatch(r"Internal error \(ref:([0-9a-f]{8})\) — see server logs", msg)
    assert m, msg
    for needle in _NEEDLES:
        assert needle not in text
    return m.group(1)


@pytest.mark.parametrize("exc", [RuntimeError(SECRET_MSG), KeyError(SECRET_MSG),
                                 ValueError(SECRET_MSG)])
def test_tool_error_is_opaque(exc, monkeypatch, audit_events, mcp_log):
    """A ValueError raised outside core.api / mcp.tools is opaque like any other."""
    _raising_tool(monkeypatch, exc)
    text, resp = _call()
    ref = _opaque_ref(text, resp)
    etype = type(exc).__name__
    assert [f for e, f in audit_events if e == "mcp.internal_error"] == [
        {"where": "tools/call", "error_type": etype, "ref": ref}]
    warn = [r for r in mcp_log if r.levelno == logging.WARNING and ref in r.getMessage()]
    assert warn and etype in warn[0].getMessage() and warn[0].exc_info is None
    assert any(r.levelno == logging.DEBUG and r.exc_info for r in mcp_log)
    assert not any(n in r.getMessage() for r in mcp_log for n in (PARTNER, DIGITS))


def test_builtin_conversion_value_error_is_opaque(audit_events):
    """``int('…')`` inside mcp.tools is not a ``raise`` statement → opaque."""
    text, resp = _call(args={"fiscal_year": f"{PARTNER} {DIGITS}"})
    _opaque_ref(text, resp)


def test_core_api_value_error_message_is_kept(db):
    """core.api's own messages (fixed text + the caller's year) stay useful."""
    _text, resp = _call("generate_balance_sheet", {"fiscal_year": 2031})
    assert resp["result"]["isError"] is True
    msg = resp["result"]["content"][0]["text"]
    assert "ref:" not in msg
    assert msg.startswith(("No entities found", "No statement data found"))


def test_unknown_tool_is_32601():
    _text, resp = _call("no_such_tool", {})
    assert resp["error"]["code"] == -32601
    assert resp["error"]["message"] == "Unknown tool: 'no_such_tool'"


def test_redaction_failure_text_not_sent(monkeypatch, audit_events, mcp_log):
    import ledger_agent.mcp.tools as tools

    monkeypatch.setattr(tools, "call_tool",
                        lambda name, args, *, allow_pii=False: json.dumps({"memo": "x"}))

    def _boom(text, scope="openai", entity_name=""):
        raise RuntimeError(f"detector crashed near {SECRET_MSG}")

    monkeypatch.setattr(privacy, "redact", _boom)
    text, resp = _call()
    assert "result" not in resp and resp["error"]["code"] == -32000
    assert re.fullmatch(r"redaction_failed \(ref:[0-9a-f]{8}\)", resp["error"]["message"])
    for needle in _NEEDLES + ("detector",):
        assert needle not in text
    assert not any(n in r.getMessage() for r in mcp_log for n in (PARTNER, DIGITS))


def test_stdio_main_loop_error_is_opaque(monkeypatch, capsys, audit_events):
    def _boom(msg):
        raise RuntimeError(SECRET_MSG)

    monkeypatch.setattr(server, "_dispatch", _boom)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n'))
    capsys.readouterr()
    server.serve_stdio()
    lines = [ln for ln in capsys.readouterr().out.split("\n") if ln]
    assert len(lines) == 1
    resp = json.loads(lines[0])
    assert resp["error"]["code"] == -32603
    assert re.fullmatch(r"Internal error \(ref:[0-9a-f]{8}\)", resp["error"]["message"])
    for needle in _NEEDLES:
        assert needle not in lines[0]
    assert [f["where"] for e, f in audit_events if e == "mcp.internal_error"] == ["main_loop"]


def test_no_traceback_module_in_mcp_server():
    assert not hasattr(server, "traceback")
