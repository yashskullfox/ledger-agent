"""
ledger_agent.mcp  –  Spec-compliant MCP server (ARCH-06)
=========================================================

Form C of the four-form architecture.  Exposes the six ``ledger_agent.core.api``
functions as MCP tools over two transports:

* **stdio** — compatible with Claude Desktop, Cursor, Wibey, and any MCP-aware
  IDE.  Launch via ``python -m ledger_agent.mcp`` or ``ledger-agent-mcp``.
* **streamable-HTTP** — listen on ``http://127.0.0.1:7337/mcp`` (SSE-based
  request/response per the MCP 2024-11-05 spec).  Launch via
  ``python -m ledger_agent.mcp --transport http``.

Quick start
-----------
Install and run the stdio server::

    pip install ledger-agent-mcp
    ledger-agent-mcp

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
from ledger_agent.mcp.server import serve_stdio, serve_http, main  # noqa: F401

__all__ = ["serve_stdio", "serve_http", "main"]
