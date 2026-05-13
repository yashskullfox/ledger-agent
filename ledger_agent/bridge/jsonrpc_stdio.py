from __future__ import annotations

import json
import logging
import sys
import traceback
from pathlib import Path

# Add repo root to sys.path for source-checkout usage
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

log = logging.getLogger(__name__)

JSONRPC_VERSION = "2.0"

# Methods that map to ledger_agent.mcp.tools.call_tool
_TOOL_METHODS = frozenset({
    "import_statements",
    "generate_balance_sheet",
    "generate_form_1065",
    "generate_k1",
    "pte_estimate",
    "reconcile_year",
})


def _respond(msg_id, result):
    payload = {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "result": result}
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _error(msg_id, code: int, message: str):
    payload = {
        "jsonrpc": JSONRPC_VERSION,
        "id": msg_id,
        "error": {"code": code, "message": message},
    }
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _dispatch(msg: dict) -> None:
    msg_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    # ── Lifecycle ────────────────────────────────────────────────────────────
    if method == "ping":
        _respond(msg_id, {"pong": True})
        return

    if method == "server_info":
        _respond(msg_id, {
            "name": "ledger-agent-bridge",
            "version": "2.1.0",
            "methods": sorted(_TOOL_METHODS) + ["ping", "server_info"],
        })
        return

    # ── Tool calls ───────────────────────────────────────────────────────────
    if method in _TOOL_METHODS:
        try:
            from ledger_agent.mcp.tools import call_tool
            raw_json = call_tool(method, params, allow_pii=True)
            _respond(msg_id, json.loads(raw_json))
        except Exception:
            tb = traceback.format_exc()
            log.error("Bridge tool %r raised:\n%s", method, tb)
            _error(msg_id, -32603, f"Internal error: {tb}")
        return

    # ── Unknown ───────────────────────────────────────────────────────────────
    _error(msg_id, -32601, f"Method not found: {method!r}")


def serve() -> None:
    # Load .env if present
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    log.info("ledger-agent JSON-RPC bridge ready on stdio")

    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            _dispatch(msg)
        except EOFError:
            break
        except json.JSONDecodeError as exc:
            _error(None, -32700, f"Parse error: {exc}")
        except Exception:
            _error(None, -32603, traceback.format_exc())


if __name__ == "__main__":
    serve()
