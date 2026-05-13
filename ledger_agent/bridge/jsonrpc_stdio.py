from __future__ import annotations

import json
import logging
import sys
import traceback
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

log = logging.getLogger(__name__)

JSONRPC_VERSION = "2.0"

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

    if method in _TOOL_METHODS:
        try:
            from ledger_agent.mcp.tools import call_tool
            # Read allow_pii from _meta — default deny (CRIT-01 fix)
            allow_pii = bool((params.get("_meta") or {}).get("allow_pii", False))
            # Strip _meta so it is not forwarded as a tool argument
            clean_params = {k: v for k, v in params.items() if k != "_meta"}
            try:
                from core.audit import audit
                from core.cleanup import run_cycle
                audit("bridge.tool_call", method=method, allow_pii=allow_pii)
                with run_cycle(f"bridge:{method}"):
                    raw_json = call_tool(method, clean_params, allow_pii=allow_pii)
            except ImportError:
                # Audit/cleanup unavailable — degrade gracefully, never silently
                # bypass PII checks.
                raw_json = call_tool(method, clean_params, allow_pii=allow_pii)
            _respond(msg_id, json.loads(raw_json))
        except Exception:
            tb = traceback.format_exc()
            log.error("Bridge tool %r raised:\n%s", method, tb)
            try:
                from core.audit import audit
                audit("bridge.tool_error", method=method,
                      error_type=type(sys.exc_info()[1]).__name__ if sys.exc_info()[1] else "Unknown")
            except Exception:
                pass
            _error(msg_id, -32603, f"Internal error: {tb}")
        return

    _error(msg_id, -32601, f"Method not found: {method!r}")


def serve() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    # Boot-time cleanup: purge anything a crashed previous bridge run left
    # behind, then open a fresh audit log for this session.
    try:
        from core.cleanup import boot_cleanup
        from core.audit import audit, current_run_id
        boot_cleanup()
        audit("bridge.session.start", transport="jsonrpc_stdio")
        log.info("ledger-agent JSON-RPC bridge ready on stdio (run_id=%s)",
                 current_run_id())
    except Exception:
        # Audit/cleanup is best-effort here — the bridge must boot even if
        # the audit module can't (e.g. missing data/ dir on first run).
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
