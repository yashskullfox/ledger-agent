"""
ledger_agent.mcp.transport_http  –  Streamable-HTTP MCP transport (ARCH-06, P-03)
==================================================================================

Implements the MCP 2024-11-05 streamable-HTTP transport:

  POST /mcp          – accepts a JSON-RPC request body, returns a JSON-RPC
                       response.
  GET  /mcp          – SSE endpoint for server-initiated notifications.
  GET  /healthz      – lightweight health probe used by the ARCH-10 fat jar.

Hardening (P-03 — the HTTP surface is reachable by every web page the user
opens, so it must not behave like a public API):

* Binds to loopback by default.  A non-loopback ``host`` is refused unless
  ``FI_MCP_HTTP_ALLOW_REMOTE=1`` (and then logged at WARNING).
* ``/mcp`` requires ``Authorization: Bearer <token>``, compared in constant
  time against ``FI_MCP_HTTP_TOKEN``.  The server refuses to start when the
  token is unset or shorter than 32 characters.
* No CORS headers are ever sent.  ``OPTIONS`` (CORS preflight) → 405.  Any
  request carrying an ``Origin`` header (i.e. sent by a browser) → 403.
* ``POST /mcp`` requires ``Content-Type: application/json`` (→ 415) and a
  body of at most 1 MiB (→ 413).
* ``_meta.allow_pii`` from the client is honoured only when the server was
  started with ``FI_MCP_ALLOW_PII=1``; otherwise it is forced to ``false``
  and the response goes through the normal redaction path.
* Every rejection emits the audit event ``mcp.http.rejected`` (reason and
  status only, never header or body values).

Requires ``uvicorn`` to serve (``pip install uvicorn``); the ASGI app itself
is stdlib-only.

Usage::

    from ledger_agent.mcp.transport_http import serve_http
    serve_http(host="127.0.0.1", port=7337, dispatch_fn=...)
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 1024 * 1024  # 1 MiB
MIN_TOKEN_LENGTH = 32

TOKEN_HINT = (
    "Generate one with:\n"
    "  python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
    "then export FI_MCP_HTTP_TOKEN=<token> (or put it in .env) and give the same "
    "token to your MCP client as 'Authorization: Bearer <token>'."
)


class HTTPTransportConfigError(RuntimeError):
    """Raised when the HTTP transport refuses to start (bad token / host)."""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _audit(event: str, **fields) -> None:
    """Emit an audit event; never let audit failure break a response."""
    try:
        from ledger_agent.core.audit import audit
        audit(event, **fields)
    except Exception as exc:  # audit is best-effort here; log type only
        log.warning("MCP HTTP: audit emit failed (%s)", type(exc).__name__)


def _env_flag(value: str | None) -> bool:
    return (value or "").strip() == "1"


def is_loopback_host(host: str) -> bool:
    """True for ``localhost`` and loopback IPv4/IPv6 literals."""
    h = (host or "").strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def validate_host(host: str) -> None:
    """Refuse a non-loopback bind address unless explicitly allowed."""
    if is_loopback_host(host):
        return
    if not _env_flag(os.environ.get("FI_MCP_HTTP_ALLOW_REMOTE")):
        _audit("mcp.http.start_refused", reason="non_loopback_host")
        raise HTTPTransportConfigError(
            f"Refusing to bind the MCP HTTP server to non-loopback host {host!r}. "
            "The server is local-only by default; set FI_MCP_HTTP_ALLOW_REMOTE=1 "
            "to override (not recommended)."
        )
    log.warning(
        "MCP HTTP server binding to non-loopback host %r because "
        "FI_MCP_HTTP_ALLOW_REMOTE=1 — it is reachable from other machines.",
        host,
    )
    _audit("mcp.http.remote_bind_allowed")


def load_token() -> str:
    """Return the configured bearer token or raise with setup instructions."""
    token = (os.environ.get("FI_MCP_HTTP_TOKEN") or "").strip()
    if not token:
        _audit("mcp.http.start_refused", reason="token_missing")
        raise HTTPTransportConfigError(
            "FI_MCP_HTTP_TOKEN is not set; the MCP HTTP transport requires a "
            "bearer token.\n" + TOKEN_HINT
        )
    if len(token) < MIN_TOKEN_LENGTH:
        _audit("mcp.http.start_refused", reason="token_too_short")
        raise HTTPTransportConfigError(
            f"FI_MCP_HTTP_TOKEN is shorter than {MIN_TOKEN_LENGTH} characters.\n"
            + TOKEN_HINT
        )
    return token


def _headers(scope) -> list[tuple[str, bytes]]:
    return [
        (k.decode("latin-1").lower(), v)
        for k, v in scope.get("headers") or []
    ]


def _header_values(headers, name: str) -> list[bytes]:
    return [v for k, v in headers if k == name]


def _is_json_content_type(values: list[bytes]) -> bool:
    if len(values) != 1:
        return False
    parts = values[0].decode("latin-1").split(";")
    if parts[0].strip().lower() != "application/json":
        return False
    for param in parts[1:]:
        key, _, val = param.partition("=")
        key = key.strip().lower()
        val = val.strip().strip('"').lower()
        if not key:
            continue
        if key != "charset" or val not in ("utf-8", "utf8"):
            return False
    return True


def _token_ok(values: list[bytes], token: str) -> bool:
    if len(values) != 1:
        return False
    scheme, _, supplied = values[0].strip().partition(b" ")
    if scheme.lower() != b"bearer":
        return False
    return hmac.compare_digest(supplied.strip(), token.encode("utf-8"))


_SECURITY_HEADERS = [
    [b"cache-control", b"no-store"],
    [b"x-content-type-options", b"nosniff"],
]


async def _respond(send, status: int, body: bytes = b"",
                   content_type: bytes | None = None,
                   extra: list | None = None) -> None:
    headers = list(_SECURITY_HEADERS)
    if content_type is not None:
        headers.append([b"content-type", content_type])
    if extra:
        headers.extend(extra)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _reject(send, status: int, reason: str, extra: list | None = None) -> None:
    """Uniform rejection: empty body, audit + INFO log with the reason only."""
    log.info("MCP HTTP request rejected: %s (%d)", reason, status)
    _audit("mcp.http.rejected", reason=reason, status=status)
    await _respond(send, status, extra=extra)


def _strip_client_allow_pii(request) -> bool:
    """Force ``_meta.allow_pii`` to False in-place.  Returns True if it was set."""
    if not isinstance(request, dict):
        return False
    params = request.get("params")
    if not isinstance(params, dict):
        return False
    meta = params.get("_meta")
    if not isinstance(meta, dict) or not meta.get("allow_pii"):
        return False
    meta["allow_pii"] = False
    return True


# ── ASGI app ──────────────────────────────────────────────────────────────────

def _build_app(dispatch_fn, *, token: str, allow_pii: bool = False):
    """
    Build the hardened ASGI app.

    Parameters
    ----------
    dispatch_fn:
        Callable taking a JSON-RPC dict and returning a JSON-RPC dict.
    token:
        Required bearer token (>= ``MIN_TOKEN_LENGTH`` characters).
    allow_pii:
        Whether a client's ``_meta.allow_pii=true`` is honoured.  Mirrors
        ``FI_MCP_ALLOW_PII=1`` at server start; default False.
    """
    if not isinstance(token, str) or len(token) < MIN_TOKEN_LENGTH:
        raise HTTPTransportConfigError(
            f"MCP HTTP app requires a bearer token of >= {MIN_TOKEN_LENGTH} characters."
        )

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return

        path = scope.get("path", "/")
        method = scope.get("method", "GET").upper()
        headers = _headers(scope)

        # ── CORS / browser lock-out ──────────────────────────────────────────
        if method == "OPTIONS":
            await _reject(send, 405, "options_preflight",
                          extra=[[b"allow", b"GET, POST"]])
            return
        # Any Origin header (including the literal "null" sent by sandboxed
        # iframes and file:// pages) means a browser sent this request.
        if any(v.strip() for v in _header_values(headers, "origin")):
            await _reject(send, 403, "origin_header")
            return

        # ── Health probe (no data, no auth) ──────────────────────────────────
        if path == "/healthz":
            if method not in ("GET", "HEAD"):
                await _reject(send, 405, "method_not_allowed", extra=[[b"allow", b"GET"]])
                return
            await _respond(send, 200, b'{"status":"ok","server":"ledger-agent-mcp"}',
                           content_type=b"application/json")
            return

        if path != "/mcp":
            await _reject(send, 404, "not_found")
            return

        # ── Authentication ───────────────────────────────────────────────────
        if not _token_ok(_header_values(headers, "authorization"), token):
            await _reject(send, 401, "unauthorized",
                          extra=[[b"www-authenticate", b"Bearer"]])
            return

        if method == "GET":
            # Minimal SSE stream — a production deployment would send server
            # notifications here.
            await send({"type": "http.response.start", "status": 200,
                        "headers": [
                            [b"content-type", b"text/event-stream"],
                            [b"cache-control", b"no-cache"],
                            [b"x-content-type-options", b"nosniff"],
                        ]})
            await send({"type": "http.response.body",
                        "body": b": ledger-agent-mcp SSE stream\n\n",
                        "more_body": False})
            return

        if method != "POST":
            await _reject(send, 405, "method_not_allowed", extra=[[b"allow", b"GET, POST"]])
            return

        # ── POST /mcp ────────────────────────────────────────────────────────
        if not _is_json_content_type(_header_values(headers, "content-type")):
            await _reject(send, 415, "unsupported_media_type")
            return

        declared = _header_values(headers, "content-length")
        if declared:
            try:
                too_big = any(int(v) > MAX_BODY_BYTES for v in declared)
            except ValueError:
                await _reject(send, 400, "bad_content_length")
                return
            if too_big:
                await _reject(send, 413, "body_too_large")
                return

        body_parts: list[bytes] = []
        size = 0
        while True:
            msg = await receive()
            if msg.get("type") == "http.disconnect":
                return
            chunk = msg.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                await _reject(send, 413, "body_too_large")
                return
            body_parts.append(chunk)
            if not msg.get("more_body", False):
                break
        raw = b"".join(body_parts)

        try:
            request = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Parse error"}}
        else:
            if not isinstance(request, dict):
                response = {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "Invalid Request"}}
            else:
                if not allow_pii and _strip_client_allow_pii(request):
                    log.info("MCP HTTP: client allow_pii ignored "
                             "(server not started with FI_MCP_ALLOW_PII=1)")
                    _audit("mcp.http.allow_pii_denied")
                try:
                    response = dispatch_fn(request)
                except Exception as exc:
                    log.error("MCP HTTP dispatch failed: %s", type(exc).__name__)
                    _audit("mcp.http.dispatch_failed", error_type=type(exc).__name__)
                    response = {"jsonrpc": "2.0", "id": request.get("id"),
                                "error": {"code": -32603, "message": "Internal error"}}

        await _respond(send, 200, json.dumps(response).encode(),
                       content_type=b"application/json")

    return app


def serve_http(host: str = "127.0.0.1", port: int = 7337, dispatch_fn=None) -> None:
    """Start the streamable-HTTP MCP server.

    Parameters
    ----------
    host:
        Bind address.  Default ``127.0.0.1`` (loopback only, per R-46).  A
        non-loopback address requires ``FI_MCP_HTTP_ALLOW_REMOTE=1``.
    port:
        TCP port.  Default ``7337``.
    dispatch_fn:
        Callable that accepts a JSON-RPC dict and returns a JSON-RPC dict.
        Injected by ``server.py`` so the transport stays logic-free.

    Raises
    ------
    HTTPTransportConfigError
        When ``FI_MCP_HTTP_TOKEN`` is missing/short or the host is refused.
    """
    if dispatch_fn is None:
        raise ValueError("serve_http requires a dispatch_fn")

    validate_host(host)
    token = load_token()
    allow_pii = _env_flag(os.environ.get("FI_MCP_ALLOW_PII"))
    if allow_pii:
        log.warning("MCP HTTP: FI_MCP_ALLOW_PII=1 — clients may request unredacted "
                    "responses with _meta.allow_pii=true.")

    try:
        import uvicorn  # type: ignore[import]
    except ImportError:
        raise RuntimeError(
            "HTTP transport requires 'uvicorn'.  Install with: pip install uvicorn"
        ) from None

    app = _build_app(dispatch_fn, token=token, allow_pii=allow_pii)
    _audit("mcp.http.started", loopback=is_loopback_host(host), allow_pii=allow_pii)
    log.info("ledger-agent MCP HTTP server starting on http://%s:%d/mcp", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
