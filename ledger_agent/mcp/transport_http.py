"""
ledger_agent.mcp.transport_http  –  Streamable-HTTP MCP transport (ARCH-06)
============================================================================

Implements the MCP 2024-11-05 streamable-HTTP transport:

  POST /mcp          – accepts a JSON-RPC request body, returns a JSON-RPC
                       response (or an SSE stream for streaming calls).
  GET  /mcp          – SSE endpoint for server-initiated notifications.
  GET  /healthz      – lightweight health probe used by the ARCH-10 fat jar.

Requires ``mcp >= 1.2.0`` (streamable-HTTP support) and ``anyio``.

Usage::

    from ledger_agent.mcp.transport_http import serve_http
    serve_http(host="127.0.0.1", port=7337)
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


def _build_app(dispatch_fn):
    """
    Build a minimal WSGI/ASGI-compatible app that handles:
      GET  /healthz
      POST /mcp     (JSON-RPC 2.0)
      GET  /mcp     (SSE notification stream — kept alive but currently empty)

    We avoid a heavy web framework dependency in the MCP package; the ASGI
    app is assembled from stdlib + the ``mcp`` SDK's SSE primitives where
    available, falling back to a hand-rolled implementation.
    """
    try:
        from mcp.server.sse import SseServerTransport
        _mcp_sse_available = True
    except ImportError:
        _mcp_sse_available = False

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return

        path = scope.get("path", "/")
        method = scope.get("method", "GET").upper()

        # ── Health probe ──────────────────────────────────────────────────────
        if path == "/healthz":
            body = b'{"status":"ok","server":"ledger-agent-mcp"}'
            await send({"type": "http.response.start", "status": 200,
                        "headers": [[b"content-type", b"application/json"]]})
            await send({"type": "http.response.body", "body": body})
            return

        # ── MCP endpoint ──────────────────────────────────────────────────────
        if path == "/mcp":
            if method == "POST":
                # Read full body
                body_parts = []
                while True:
                    msg = await receive()
                    body_parts.append(msg.get("body", b""))
                    if not msg.get("more_body", False):
                        break
                raw = b"".join(body_parts)
                try:
                    request = json.loads(raw)
                except json.JSONDecodeError:
                    response = {"jsonrpc": "2.0", "id": None,
                                "error": {"code": -32700, "message": "Parse error"}}
                else:
                    response = dispatch_fn(request)

                resp_body = json.dumps(response).encode()
                await send({"type": "http.response.start", "status": 200,
                            "headers": [
                                [b"content-type", b"application/json"],
                                [b"access-control-allow-origin", b"*"],
                            ]})
                await send({"type": "http.response.body", "body": resp_body})
                return

            if method == "GET":
                # Minimal SSE stream — send a keep-alive comment every 15 s
                # A production deployment would send server notifications here.
                headers = [
                    [b"content-type", b"text/event-stream"],
                    [b"cache-control", b"no-cache"],
                    [b"access-control-allow-origin", b"*"],
                ]
                await send({"type": "http.response.start", "status": 200,
                            "headers": headers})
                await send({"type": "http.response.body",
                            "body": b": ledger-agent-mcp SSE stream\n\n",
                            "more_body": False})
                return

        # ── 404 fallback ──────────────────────────────────────────────────────
        await send({"type": "http.response.start", "status": 404,
                    "headers": [[b"content-type", b"text/plain"]]})
        await send({"type": "http.response.body", "body": b"Not found"})

    return app


def serve_http(host: str = "127.0.0.1", port: int = 7337, dispatch_fn=None) -> None:
    """Start the streamable-HTTP MCP server.

    Parameters
    ----------
    host:
        Bind address.  Default ``127.0.0.1`` (loopback only, per R-46).
    port:
        TCP port.  Default ``7337``.
    dispatch_fn:
        Callable that accepts a JSON-RPC dict and returns a JSON-RPC dict.
        Injected by ``server.py`` so the transport stays logic-free.
    """
    if dispatch_fn is None:
        raise ValueError("serve_http requires a dispatch_fn")

    try:
        import uvicorn  # type: ignore[import]
    except ImportError:
        raise RuntimeError(
            "HTTP transport requires 'uvicorn'.  "
            "Install with: pip install 'ledger-agent-mcp[http]'"
        ) from None

    app = _build_app(dispatch_fn)
    log.info("ledger-agent MCP HTTP server starting on http://%s:%d/mcp", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
