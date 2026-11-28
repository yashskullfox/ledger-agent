"""
tests/unit/test_mcp_http_transport.py  –  MCP HTTP transport hardening (P-03)
=============================================================================

Drives the ASGI app from ``ledger_agent.mcp.transport_http`` directly (no
sockets, no uvicorn) and checks the start-up guards of ``serve_http`` /
``ledger-agent-mcp --transport http`` with a fake ``uvicorn`` module.

Synthetic data only: the allow_pii cases use a tmp DB holding one
pseudonymous entity (``ENTITY_A ...``) and one account.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from ledger_agent.mcp import transport_http as th

TOKEN = "t" * 40
FY = 2026
ENTITY_ID = "ent-http"
ENTITY_NAME = "ENTITY_A HTTPTEST LLC"


# ── ASGI driver ───────────────────────────────────────────────────────────────

def _asgi(app, method: str = "POST", path: str = "/mcp",
          headers: dict[str, str] | list[tuple[str, str]] | None = None,
          body: bytes = b"", chunks: list[bytes] | None = None):
    """Run one request through the ASGI app; return (status, headers, body)."""
    if isinstance(headers, dict):
        headers = list(headers.items())
    scope = {
        "type": "http", "method": method, "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or [])],
    }
    parts = chunks if chunks is not None else [body]
    queue = [
        {"type": "http.request", "body": p, "more_body": i < len(parts) - 1}
        for i, p in enumerate(parts)
    ]
    sent: list[dict] = []

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    out_body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    out_headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], out_headers, out_body


def _auth(**extra: str) -> dict[str, str]:
    h = {"authorization": f"Bearer {TOKEN}", "content-type": "application/json"}
    h.update(extra)
    return h


def _rpc(method: str = "ping", params: dict | None = None) -> bytes:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg).encode()


class _Recorder:
    def __init__(self):
        self.requests: list[dict] = []

    def __call__(self, request: dict) -> dict:
        self.requests.append(json.loads(json.dumps(request)))
        return {"jsonrpc": "2.0", "id": request.get("id"), "result": {"ok": True}}


@pytest.fixture
def dispatch():
    return _Recorder()


@pytest.fixture
def app(dispatch):
    return th._build_app(dispatch, token=TOKEN)


@pytest.fixture
def audit_events(monkeypatch):
    events: list[tuple[str, dict]] = []
    import ledger_agent.core.audit as audit_mod

    monkeypatch.setattr(audit_mod, "audit", lambda ev, **kw: events.append((ev, kw)))
    return events


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("FI_MCP_HTTP_TOKEN", "FI_MCP_ALLOW_PII", "FI_MCP_HTTP_ALLOW_REMOTE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fake_uvicorn(monkeypatch):
    calls: list[dict] = []
    mod = types.ModuleType("uvicorn")
    mod.run = lambda app, **kw: calls.append({"app": app, **kw})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", mod)
    return calls


def _no_cors(headers: dict[str, str]) -> None:
    assert not [k for k in headers if k.startswith("access-control-")]


# ── Start-up guards ───────────────────────────────────────────────────────────

class TestStartup:
    def test_refused_without_token(self, fake_uvicorn):
        with pytest.raises(th.HTTPTransportConfigError) as ei:
            th.serve_http(dispatch_fn=_Recorder())
        assert "FI_MCP_HTTP_TOKEN" in str(ei.value)
        assert "secrets.token_urlsafe(32)" in str(ei.value)
        assert fake_uvicorn == []

    def test_refused_with_short_token(self, monkeypatch, fake_uvicorn):
        monkeypatch.setenv("FI_MCP_HTTP_TOKEN", "x" * 31)
        with pytest.raises(th.HTTPTransportConfigError) as ei:
            th.serve_http(dispatch_fn=_Recorder())
        assert "32" in str(ei.value)
        assert "x" * 31 not in str(ei.value)  # never echo the token
        assert fake_uvicorn == []

    def test_build_app_refuses_short_token(self):
        with pytest.raises(th.HTTPTransportConfigError):
            th._build_app(_Recorder(), token="short")

    def test_starts_on_loopback_with_token(self, monkeypatch, fake_uvicorn):
        monkeypatch.setenv("FI_MCP_HTTP_TOKEN", TOKEN)
        th.serve_http(dispatch_fn=_Recorder())
        assert len(fake_uvicorn) == 1
        assert fake_uvicorn[0]["host"] == "127.0.0.1"
        assert fake_uvicorn[0]["port"] == 7337

    def test_missing_uvicorn_hint_names_http_extra(self, monkeypatch):
        monkeypatch.setenv("FI_MCP_HTTP_TOKEN", TOKEN)
        monkeypatch.setitem(sys.modules, "uvicorn", None)  # import -> ImportError
        with pytest.raises(RuntimeError) as ei:
            th.serve_http(dispatch_fn=_Recorder())
        msg = str(ei.value)
        assert 'pip install "ledger-agent[http]"' in msg
        assert "pip install uvicorn" in msg
        assert TOKEN not in msg

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "example.com"])
    def test_non_loopback_refused_without_allow_remote(self, host, monkeypatch, fake_uvicorn):
        monkeypatch.setenv("FI_MCP_HTTP_TOKEN", TOKEN)
        with pytest.raises(th.HTTPTransportConfigError) as ei:
            th.serve_http(host=host, dispatch_fn=_Recorder())
        assert "FI_MCP_HTTP_ALLOW_REMOTE" in str(ei.value)
        assert fake_uvicorn == []

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
    def test_loopback_hosts_allowed(self, host):
        assert th.is_loopback_host(host)

    def test_non_loopback_allowed_with_flag_logs_warning(self, monkeypatch, fake_uvicorn,
                                                         caplog):
        monkeypatch.setenv("FI_MCP_HTTP_TOKEN", TOKEN)
        monkeypatch.setenv("FI_MCP_HTTP_ALLOW_REMOTE", "1")
        with caplog.at_level(logging.WARNING, logger=th.log.name):
            th.serve_http(host="0.0.0.0", dispatch_fn=_Recorder())
        assert fake_uvicorn[0]["host"] == "0.0.0.0"
        assert any(r.levelno == logging.WARNING and "FI_MCP_HTTP_ALLOW_REMOTE" in r.getMessage()
                   for r in caplog.records)

    def test_cli_exits_with_error_without_token(self, monkeypatch, fake_uvicorn, capsys):
        from ledger_agent.mcp import server

        monkeypatch.setattr(sys, "argv", ["ledger-agent-mcp", "--transport", "http"])
        with pytest.raises(SystemExit) as ei:
            server.main()
        assert ei.value.code == 2
        err = capsys.readouterr().err
        assert "FI_MCP_HTTP_TOKEN" in err
        assert fake_uvicorn == []

    def test_cli_default_host_is_loopback(self, monkeypatch, fake_uvicorn):
        from ledger_agent.mcp import server

        monkeypatch.setenv("FI_MCP_HTTP_TOKEN", TOKEN)
        monkeypatch.setattr(sys, "argv", ["ledger-agent-mcp", "--transport", "http"])
        server.main()
        assert fake_uvicorn[0]["host"] == "127.0.0.1"


# ── Request guards ────────────────────────────────────────────────────────────

class TestRequestGuards:
    def test_valid_request_dispatched(self, app, dispatch):
        status, headers, body = _asgi(app, headers=_auth(), body=_rpc())
        assert status == 200
        assert json.loads(body)["result"] == {"ok": True}
        assert headers["content-type"] == "application/json"
        assert len(dispatch.requests) == 1
        _no_cors(headers)

    def test_charset_utf8_accepted(self, app):
        status, _, _ = _asgi(app, headers=_auth(**{"content-type":
                                                   "application/json; charset=UTF-8"}),
                             body=_rpc())
        assert status == 200

    @pytest.mark.parametrize("headers", [
        {"content-type": "application/json"},
        _auth(authorization=f"Bearer {'x' * 40}"),
        _auth(authorization=f"Bearer {TOKEN}x"),
        _auth(authorization=f"Basic {TOKEN}"),
        _auth(authorization=TOKEN),
    ], ids=["missing", "wrong", "suffixed", "basic_scheme", "no_scheme"])
    def test_401_without_valid_token(self, app, dispatch, headers, audit_events):
        status, out_headers, body = _asgi(app, headers=headers, body=_rpc())
        assert status == 401
        assert body == b""
        assert dispatch.requests == []
        _no_cors(out_headers)
        assert audit_events == [("mcp.http.rejected", {"reason": "unauthorized", "status": 401})]

    def test_401_on_sse_get_without_token(self, app):
        status, _, _ = _asgi(app, method="GET", headers={})
        assert status == 401

    def test_sse_get_with_token(self, app):
        status, headers, _ = _asgi(app, method="GET", headers=_auth())
        assert status == 200
        assert headers["content-type"] == "text/event-stream"
        _no_cors(headers)

    @pytest.mark.parametrize("origin", ["http://evil.example", "http://127.0.0.1:7337", "null"])
    def test_403_with_origin_header(self, app, dispatch, origin, audit_events):
        status, headers, body = _asgi(app, headers=_auth(origin=origin), body=_rpc())
        assert status == 403
        assert body == b""
        assert dispatch.requests == []
        _no_cors(headers)
        assert audit_events[-1] == ("mcp.http.rejected",
                                    {"reason": "origin_header", "status": 403})

    @pytest.mark.parametrize("path", ["/mcp", "/healthz", "/other"])
    def test_options_preflight_405(self, app, dispatch, path, audit_events):
        status, headers, body = _asgi(app, method="OPTIONS", path=path, headers={
            "origin": "http://evil.example",
            "access-control-request-method": "POST",
            "access-control-request-headers": "authorization, content-type",
        })
        assert status == 405
        assert body == b""
        _no_cors(headers)
        assert dispatch.requests == []
        assert audit_events[-1][0] == "mcp.http.rejected"

    @pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded",
                                       "multipart/form-data; boundary=x",
                                       "application/json; charset=latin-1", None])
    def test_415_non_json_content_type(self, app, dispatch, ctype, audit_events):
        headers = {"authorization": f"Bearer {TOKEN}"}
        if ctype:
            headers["content-type"] = ctype
        status, out_headers, body = _asgi(app, headers=headers, body=_rpc())
        assert status == 415
        assert body == b""
        assert dispatch.requests == []
        _no_cors(out_headers)
        assert audit_events[-1] == ("mcp.http.rejected",
                                    {"reason": "unsupported_media_type", "status": 415})

    def test_413_declared_oversized_body(self, app, dispatch, audit_events):
        status, _, body = _asgi(app, headers=_auth(**{"content-length":
                                                      str(th.MAX_BODY_BYTES + 1)}),
                                body=b"")
        assert status == 413
        assert body == b""
        assert dispatch.requests == []
        assert audit_events[-1] == ("mcp.http.rejected",
                                    {"reason": "body_too_large", "status": 413})

    def test_413_streamed_oversized_body(self, app, dispatch):
        chunk = b" " * (th.MAX_BODY_BYTES // 2)
        status, _, _ = _asgi(app, headers=_auth(), chunks=[chunk, chunk, b"  "])
        assert status == 413
        assert dispatch.requests == []

    def test_body_at_limit_accepted(self, app):
        payload = _rpc()
        padded = payload + b" " * (th.MAX_BODY_BYTES - len(payload))
        status, _, _ = _asgi(app, headers=_auth(), body=padded)
        assert status == 200

    def test_healthz_open_without_token(self, app):
        status, headers, body = _asgi(app, method="GET", path="/healthz", headers={})
        assert status == 200
        assert json.loads(body)["status"] == "ok"
        _no_cors(headers)

    def test_unknown_path_404(self, app):
        status, _, _ = _asgi(app, headers=_auth(), path="/admin", body=_rpc())
        assert status == 404

    def test_put_405(self, app):
        status, _, _ = _asgi(app, method="PUT", headers=_auth(), body=_rpc())
        assert status == 405

    def test_parse_error_is_jsonrpc(self, app, dispatch):
        status, _, body = _asgi(app, headers=_auth(), body=b"{not json")
        assert status == 200
        assert json.loads(body)["error"]["code"] == -32700
        assert dispatch.requests == []

    def test_batch_array_rejected(self, app, dispatch):
        status, _, body = _asgi(app, headers=_auth(), body=b"[" + _rpc() + b"]")
        assert status == 200
        assert json.loads(body)["error"]["code"] == -32600
        assert dispatch.requests == []

    def test_no_cors_on_any_response(self, app):
        for method, path, headers in [
            ("POST", "/mcp", _auth()), ("GET", "/mcp", _auth()), ("GET", "/healthz", {}),
            ("POST", "/mcp", {}), ("POST", "/nope", _auth()),
        ]:
            _, out_headers, _ = _asgi(app, method=method, path=path, headers=headers,
                                      body=_rpc())
            _no_cors(out_headers)


# ── allow_pii over HTTP ───────────────────────────────────────────────────────

_PII_CALL = {"name": "x", "arguments": {}, "_meta": {"allow_pii": True}}


class TestAllowPiiFlag:
    def test_client_allow_pii_stripped_by_default(self, dispatch, audit_events, caplog):
        app = th._build_app(dispatch, token=TOKEN)
        with caplog.at_level(logging.INFO, logger=th.log.name):
            status, _, _ = _asgi(app, headers=_auth(), body=_rpc("tools/call", _PII_CALL))
        assert status == 200
        assert dispatch.requests[0]["params"]["_meta"]["allow_pii"] is False
        assert ("mcp.http.allow_pii_denied", {}) in audit_events
        assert any("allow_pii ignored" in r.getMessage() for r in caplog.records)

    def test_client_allow_pii_kept_when_server_allows(self, dispatch):
        app = th._build_app(dispatch, token=TOKEN, allow_pii=True)
        _asgi(app, headers=_auth(), body=_rpc("tools/call", _PII_CALL))
        assert dispatch.requests[0]["params"]["_meta"]["allow_pii"] is True

    def test_serve_http_reads_fi_mcp_allow_pii(self, monkeypatch, fake_uvicorn):
        rec = _Recorder()
        monkeypatch.setenv("FI_MCP_HTTP_TOKEN", TOKEN)
        monkeypatch.setenv("FI_MCP_ALLOW_PII", "1")
        th.serve_http(dispatch_fn=rec)
        _asgi(fake_uvicorn[0]["app"], headers=_auth(), body=_rpc("tools/call", _PII_CALL))
        assert rec.requests[0]["params"]["_meta"]["allow_pii"] is True

        monkeypatch.delenv("FI_MCP_ALLOW_PII")
        rec2 = _Recorder()
        th.serve_http(dispatch_fn=rec2)
        _asgi(fake_uvicorn[1]["app"], headers=_auth(), body=_rpc("tools/call", _PII_CALL))
        assert rec2.requests[0]["params"]["_meta"]["allow_pii"] is False


# ── End to end through the real MCP dispatcher (synthetic DB) ────────────────

@pytest.fixture
def entity_db(tmp_path, monkeypatch):
    from ledger_agent.core.database import (
        AccountRepo,
        EntityRepo,
        SnapshotRepo,
        TransactionRepo,
        init_db,
    )
    from ledger_agent.core.models import (
        Account,
        AccountSnapshot,
        AccountType,
        Entity,
        Transaction,
        TransactionType,
    )

    db_file = tmp_path / "http.db"
    monkeypatch.setenv("FI_DB_PATH", str(db_file))
    init_db(db_file)
    EntityRepo.upsert(
        Entity(id=ENTITY_ID, name=ENTITY_NAME, entity_type="LLC", state="MO"), db_file
    )
    AccountRepo.upsert(Account(
        id="acct-http-chk", entity_id=ENTITY_ID, name="BANK_X",
        institution="BANK_X", account_type=AccountType.CHECKING,
        account_number_masked="0000",
    ), db_file)
    for month in range(1, 13):
        SnapshotRepo.upsert(AccountSnapshot(
            account_id="acct-http-chk", statement_period=f"{FY}-{month:02d}",
            ending_balance=Decimal("100"),
        ), db_file)
    TransactionRepo.bulk_insert([
        Transaction(
            id=f"txn-http-{month}", account_id="acct-http-chk",
            date=date(FY, month, 5), description="Office supplies",
            raw_description="Office supplies", amount=Decimal("-10.00"),
            transaction_type=TransactionType.DEBIT,
            statement_period=f"{FY}-{month:02d}",
            coa_code="5080", coa_name="Other Operating Expenses",
        )
        for month in range(1, 13)
    ], db_file)
    return db_file


def _server_app(allow_pii: bool):
    from ledger_agent.mcp.server import _dispatch

    def _dispatch_sync(req):
        return _dispatch(req) or {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}

    return th._build_app(_dispatch_sync, token=TOKEN, allow_pii=allow_pii)


def _balance_sheet_text(app) -> str:
    params = {"name": "generate_balance_sheet", "arguments": {"fiscal_year": FY},
              "_meta": {"allow_pii": True}}
    status, _, body = _asgi(app, headers=_auth(), body=_rpc("tools/call", params))
    assert status == 200
    resp = json.loads(body)
    assert "error" not in resp, resp
    assert not resp["result"].get("isError"), resp
    return resp["result"]["content"][0]["text"]


def test_allow_pii_ignored_over_http_without_server_flag(entity_db):
    text = _balance_sheet_text(_server_app(allow_pii=False))
    assert ENTITY_NAME not in text
    assert "ENTITY_A" not in text
    assert '"entity_name"' in text  # field present, value redacted


def test_allow_pii_honoured_over_http_with_server_flag(entity_db):
    """Proves the assertion above is not vacuous: with the server-side opt-in
    the same request returns the entity name."""
    assert ENTITY_NAME in _balance_sheet_text(_server_app(allow_pii=True))
