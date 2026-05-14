"""
ledger_agent.mcp.transport_stdio  –  Stdio MCP transport (ARCH-06)
===================================================================

Low-level stdio reader/writer that implements the MCP wire protocol:
newline-delimited JSON-RPC 2.0.  Compatible with Claude Desktop, Cursor,
Wibey, and the reference ``@modelcontextprotocol/inspector`` CLI.

This module is intentionally thin — all tool logic lives in ``tools.py``
and all privacy filtering in ``server.py``.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Optional


def read_message() -> Optional[dict]:
    """Read one JSON-RPC message from stdin.

    Returns ``None`` on EOF (client disconnected).
    Raises ``json.JSONDecodeError`` on a malformed line (caller should
    send a parse-error response).
    """
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    return json.loads(line)


def write_message(obj: Any) -> None:
    """Write one JSON object to stdout followed by a newline, then flush."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def respond(msg_id: Any, result: Any) -> None:
    """Send a successful JSON-RPC response."""
    write_message({"jsonrpc": "2.0", "id": msg_id, "result": result})


def error(msg_id: Any, code: int, message: str) -> None:
    """Send a JSON-RPC error response."""
    write_message({
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    })
