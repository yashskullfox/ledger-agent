"""
ledger_agent.mcp  –  Spec-compliant MCP server (ARCH-06)
=========================================================

Form C of the four-form architecture.  Exposes the ``ledger_agent.core.api``
functions as MCP tools (see ``ledger_agent.mcp.tools.TOOL_SCHEMAS``) over two
transports:

* **stdio** — compatible with Claude Desktop, Cursor, Wibey, and any MCP-aware
  IDE.  Launch via ``python -m ledger_agent.mcp`` or ``ledger-agent-mcp``.
* **streamable-HTTP** — listen on ``http://127.0.0.1:7337/mcp`` (SSE-based
  request/response per the MCP 2024-11-05 spec).  Launch via
  ``python -m ledger_agent.mcp --transport http``.

Quick start
-----------
Install and run the stdio server::

    pip install ledger-agent
    ledger-agent-mcp

HTTP transport (needs the ``http`` extra for ``uvicorn``).  The server refuses
to start without a bearer token of at least 32 characters in
``FI_MCP_HTTP_TOKEN`` and binds ``127.0.0.1`` by default (a non-loopback
``--host`` also needs ``FI_MCP_HTTP_ALLOW_REMOTE=1``)::

    pip install "ledger-agent[http]"
    export FI_MCP_HTTP_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
    ledger-agent-mcp --transport http
    # client: POST http://127.0.0.1:7337/mcp
    #         with "Authorization: Bearer $FI_MCP_HTTP_TOKEN"

Claude Desktop config entry::

    {
      "mcpServers": {
        "ledger-agent": {
          "command": "ledger-agent-mcp",
          "args": []
        }
      }
    }
"""
from ledger_agent.mcp.server import main, serve_http, serve_stdio  # noqa: F401

__all__ = ["serve_stdio", "serve_http", "main"]
